"""Thin provider layer: every AI feature asks for JSON matching a Pydantic model.

Swapping providers is a config change (LLM_PROVIDER). Output is always
validated; one retry with the validation error is attempted before giving up.
"""
import json
import logging
import time
from collections.abc import Iterator
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import config

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger("crm.llm")


class LLMError(RuntimeError):
    pass


# Every Ollama call must use the same num_ctx: a different value makes Ollama
# reload the whole model (~30s). Prompts are capped (24 notes, 20 chunks), so 8k
# is plenty; Ollama's default (~4k) could silently truncate a long one.
OLLAMA_CTX = 8192


def provider_name() -> str:
    if config.LLM_PROVIDER == "gemini" and not config.GEMINI_API_KEY:
        return "rules"
    return config.LLM_PROVIDER


def model_label() -> str:
    p = provider_name()
    if p == "gemini":
        return f"gemini · {config.GEMINI_MODEL}"
    if p == "ollama":
        return f"ollama · {config.OLLAMA_MODEL}"
    return "rules (no LLM configured)"


def _call_gemini(system: str, prompt: str, schema: dict) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent"
    )
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        },
    }
    r = httpx.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=90)
    if r.status_code != 200:
        raise LLMError(f"Gemini error {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected Gemini response: {r.text[:300]}") from e


def _call_ollama(system: str, prompt: str, schema: dict) -> str:
    body = {
        "model": config.OLLAMA_MODEL,
        "stream": False,
        "format": schema,  # Ollama constrains decoding to this JSON schema
        # Keep the model in memory between requests; reloading 4.7GB adds seconds to the next call.
        "keep_alive": "30m",
        "options": {"temperature": 0.2, "num_ctx": OLLAMA_CTX},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        r = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=300)
    except httpx.ConnectError as e:
        raise LLMError(f"Cannot reach Ollama at {config.OLLAMA_URL}. Is `ollama serve` running?") from e
    if r.status_code != 200:
        raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
    data = r.json()
    # Token counts and timings, to see whether time goes to reading the prompt or writing the answer.
    log.info("ollama read %s tokens in %.1fs, wrote %s tokens in %.1fs",
             data.get("prompt_eval_count"), data.get("prompt_eval_duration", 0) / 1e9,
             data.get("eval_count"), data.get("eval_duration", 0) / 1e9)
    return data["message"]["content"]


def warm_up() -> None:
    """Load the local model into memory at startup so the first real request isn't a cold start."""
    if provider_name() != "ollama":
        return
    try:
        # An empty prompt just loads the model (Ollama's documented preload call),
        # with the same num_ctx as real calls so they don't trigger a reload.
        httpx.post(f"{config.OLLAMA_URL}/api/generate", timeout=120, json={
            "model": config.OLLAMA_MODEL, "keep_alive": "30m", "options": {"num_ctx": OLLAMA_CTX},
        })
    except httpx.HTTPError:
        pass  # Ollama not running; the real call will report a clear error later.


def generate(system: str, prompt: str, model: type[T], schema_in_prompt: bool = True) -> T:
    """Get JSON matching `model`. Both providers enforce the schema while decoding; pasting it into the
    prompt as well helps the model with field descriptions but makes it slower and wordier, so
    latency-sensitive features (Ask, drafts) turn it off and explain the fields in the system prompt."""
    provider = provider_name()
    if provider == "rules":
        raise LLMError("No LLM configured")
    call = _call_gemini if provider == "gemini" else _call_ollama
    schema = model.model_json_schema()
    full_prompt = f"{prompt}\n\nRespond ONLY with JSON matching this schema:\n{json.dumps(schema)}" \
        if schema_in_prompt else f"{prompt}\n\nRespond ONLY with JSON."

    last_error = ""
    for attempt in range(2):
        start = time.monotonic()
        text = call(system, full_prompt + last_error, schema)
        # One log line per call: which feature, how long, whether the output was valid.
        try:
            result = model.model_validate_json(text)
            log.info("llm %s %.1fs ok (prompt %d chars)", model.__name__, time.monotonic() - start, len(full_prompt))
            return result
        except ValidationError as e:
            log.warning("llm %s %.1fs invalid JSON (attempt %d)", model.__name__, time.monotonic() - start, attempt + 1)
            last_error = f"\n\nYour previous answer was invalid ({e.errors()[:3]}). Fix it."
    raise LLMError(f"Model returned invalid JSON twice: {last_error}")


def stream_text(system: str, prompt: str, max_tokens: int = 400) -> Iterator[str]:
    """Stream plain text chunk by chunk, so the UI shows words as soon as they are written.

    For Ollama, keep the start of `system` + `prompt` identical across calls where
    possible: Ollama reuses its cached reading (KV cache) of a matching prefix.
    """
    provider = provider_name()
    if provider == "rules":
        raise LLMError("No LLM configured")
    if provider == "gemini":
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.GEMINI_MODEL}:streamGenerateContent?alt=sse"
        )
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens},
        }
        with httpx.stream("POST", url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=90) as r:
            if r.status_code != 200:
                raise LLMError(f"Gemini error {r.status_code}: {r.read()[:300]!r}")
            for line in r.iter_lines():
                if line.startswith("data: "):
                    parts = json.loads(line[6:]).get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    yield "".join(p.get("text", "") for p in parts)
        return

    body = {
        "model": config.OLLAMA_MODEL,
        "stream": True,
        "keep_alive": "30m",
        "options": {"temperature": 0.3, "num_ctx": OLLAMA_CTX, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }
    start = time.monotonic()
    try:
        with httpx.stream("POST", f"{config.OLLAMA_URL}/api/chat", json=body, timeout=300) as r:
            if r.status_code != 200:
                raise LLMError(f"Ollama error {r.status_code}: {r.read()[:300]!r}")
            for line in r.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get("done"):
                    log.info("ollama stream read %s tokens in %.1fs, wrote %s tokens in %.1fs (total %.1fs)",
                             data.get("prompt_eval_count"), data.get("prompt_eval_duration", 0) / 1e9,
                             data.get("eval_count"), data.get("eval_duration", 0) / 1e9, time.monotonic() - start)
                yield data.get("message", {}).get("content", "")
    except httpx.ConnectError as e:
        raise LLMError(f"Cannot reach Ollama at {config.OLLAMA_URL}. Is `ollama serve` running?") from e


def prime(system: str, prompt: str) -> None:
    """Have Ollama read a prompt now (writing 1 token), so its KV cache holds that prefix for the next call."""
    if provider_name() != "ollama":
        return
    try:
        for _ in stream_text(system, prompt, max_tokens=1):
            pass
    except LLMError:
        pass


def embed_model_label() -> str | None:
    """Embedding model for meaning-based search, or None when it is unavailable (keyword search still works)."""
    return config.OLLAMA_EMBED_MODEL if provider_name() == "ollama" else None


def embed(texts: list[str], query: bool = False) -> list[list[float]]:
    """Turn texts into meaning vectors with the local embedding model."""
    if not embed_model_label():
        raise LLMError("Embeddings need LLM_PROVIDER=ollama")
    # nomic-embed-text expects these task prefixes.
    prefix = "search_query: " if query else "search_document: "
    try:
        r = httpx.post(f"{config.OLLAMA_URL}/api/embed", timeout=120, json={
            "model": config.OLLAMA_EMBED_MODEL, "input": [prefix + t for t in texts], "keep_alive": "30m",
        })
    except httpx.ConnectError as e:
        raise LLMError(f"Cannot reach Ollama at {config.OLLAMA_URL}") from e
    if r.status_code != 200:
        raise LLMError(f"Ollama embed error {r.status_code}: {r.text[:300]}")
    return r.json()["embeddings"]
