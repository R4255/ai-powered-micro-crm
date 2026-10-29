"""Search over chunks (notes, client profiles, AI views): keyword + meaning, merged.

  - keyword: SQLite FTS5, ranked by BM25. Exact terms like "Dentrix" or a name.
  - meaning: cosine similarity between embedding vectors. Finds "talking to an AI"
    for the question "worried about robots?".
The two rankings are merged with reciprocal rank fusion (RRF): every list gives a
chunk 1/(60 + rank), and the scores add up. No tuning, and a chunk ranked high by
either method ends up near the top.

Ask uses this across all clients; drafts use it inside one client. Either way the
prompt gets the top k chunks, so its size stays flat as data grows. At larger scale
the in-Python cosine loop would move to a vector index (sqlite-vec, pgvector); the
functions here would keep the same signatures.
"""
import logging
import math
import re
from array import array

from . import db, llm

log = logging.getLogger("crm.retrieval")

STOPWORDS = set(
    "a an and any are as at be by can did do does for from has have how i in is it its me my of on or "
    "our than that the their them they this to us was we were what when where which who whom why will "
    "with you your about anyone anything client clients customer customers prospect prospects tell show "
    "list find give write email".split()
)
RRF_K = 60


def _pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def _unpack(blob: bytes) -> array:
    v = array("f")
    v.frombytes(blob)
    return v


def _scope(customer_id: str | None, kinds: tuple[str, ...] | None, alias: str = "") -> tuple[str, list]:
    """Extra WHERE clauses limiting a search to one client and/or some chunk kinds."""
    sql, params = "", []
    if customer_id:
        sql += f" AND {alias}customer_id = ?"
        params.append(customer_id)
    if kinds:
        sql += f" AND {alias}kind IN ({','.join('?' * len(kinds))})"
        params.extend(kinds)
    return sql, params


def keyword_search(query: str, customer_id: str | None = None, kinds=None, limit: int = 50) -> list[str]:
    """Chunk ids ordered by BM25."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOPWORDS and len(w) > 1]
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words)  # quoted: user text is never parsed as FTS syntax
    where, params = _scope(customer_id, kinds)
    hits = db.rows(f"SELECT chunk_id FROM chunks WHERE chunks MATCH ?{where} ORDER BY rank LIMIT ?",
                   match, *params, limit)
    return [h["chunk_id"] for h in hits]


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def vector_search(query: str, customer_id: str | None = None, kinds=None, limit: int = 50) -> list[str]:
    """Chunk ids ordered by meaning similarity to the query."""
    model = llm.embed_model_label()
    if not model:
        return []
    q = llm.embed([query], query=True)[0]
    where, params = _scope(customer_id, kinds, alias="c.")
    rows = db.rows(
        "SELECT v.chunk_id, v.vector FROM chunk_vectors v JOIN chunks c ON c.chunk_id = v.chunk_id "
        f"WHERE v.model = ?{where}", model, *params,
    )
    scored = sorted(((_cosine(q, _unpack(r["vector"])), r["chunk_id"]) for r in rows), reverse=True)
    return [cid for _, cid in scored[:limit]]


def search(query: str, k: int, customer_id: str | None = None, kinds=None, exclude=()) -> list[dict]:
    """Top k chunks for a query: keyword and meaning rankings merged with RRF (each with its RRF score)."""
    rankings = [keyword_search(query, customer_id, kinds)]
    try:
        rankings.append(vector_search(query, customer_id, kinds))
    except llm.LLMError as e:
        log.warning("vector search unavailable, using keywords only: %s", e)
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(c for c in ranking if c not in exclude):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    top = sorted(scores, key=scores.get, reverse=True)[:k]
    if not top:
        return []
    found = {r["chunk_id"]: r for r in db.rows(
        f"SELECT chunk_id, customer_id, kind, text FROM chunks WHERE chunk_id IN ({','.join('?' * len(top))})", *top)}
    return [{**found[c], "score": scores[c]} for c in top if c in found]


# ------------------------------------------------------------------ indexing


def embed_missing(customer_id: str | None = None, batch: int = 64) -> int:
    """Embed chunks that have no vector yet (new notes, new clients, refreshed AI views)."""
    model = llm.embed_model_label()
    if not model:
        return 0
    where, params = _scope(customer_id, None, alias="c.")
    missing = db.rows(
        "SELECT c.chunk_id, c.customer_id, c.text FROM chunks c "
        "LEFT JOIN chunk_vectors v ON v.chunk_id = c.chunk_id AND v.model = ? "
        f"WHERE v.chunk_id IS NULL{where}", model, *params,
    )
    for start in range(0, len(missing), batch):
        part = missing[start:start + batch]
        vectors = llm.embed([m["text"] for m in part])
        with db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO chunk_vectors (chunk_id, model, customer_id, vector) VALUES (?, ?, ?, ?)",
                [(m["chunk_id"], model, m["customer_id"], _pack(v)) for m, v in zip(part, vectors)],
            )
    return len(missing)


def index_ai_view(customer_id: str, text: str) -> None:
    """Replace a client's "AI view" chunk (summary + key facts). Its vector is re-made by embed_missing."""
    chunk_id = f"ai:{customer_id}"
    with db.connect() as conn:
        old = conn.execute("SELECT text FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
        if old and old["text"] == text:
            return
        conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (chunk_id,))
        conn.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,))
        conn.execute("INSERT INTO chunks (chunk_id, customer_id, kind, text) VALUES (?, ?, 'ai_view', ?)",
                     (chunk_id, customer_id, text))
