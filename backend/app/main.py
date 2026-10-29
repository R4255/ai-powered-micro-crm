import json
import logging
import threading
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import crm, db, email_sender, jobs, llm
from .config import AS_OF_DATE
from .schemas import DraftRequest, NewClient, NewInteraction, Question, SendEmail

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per HTTP call is noise

app = FastAPI(title="Micro-CRM")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
db.init_db()



def _warm_up() -> None:
    # Load the model, then put Ask's fixed router prompt in Ollama's KV cache.
    llm.warm_up()
    crm.prime_router()


# Warm up at startup so the first request isn't a cold start, then start the worker
# that keeps analyses, summaries and search vectors up to date.
threading.Thread(target=_warm_up, daemon=True).start()
jobs.start_in_background()


def _ndjson(events: Iterator[dict]) -> StreamingResponse:
    """Stream events as one JSON object per line, so the UI can show results as they arrive."""
    def body():
        try:
            for event in events:
                yield json.dumps(event) + "\n"
        except llm.LLMError as e:
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"
    return StreamingResponse(body(), media_type="application/x-ndjson")


def _account_or_404(customer_id: str) -> dict:
    account = crm.get_account(customer_id)
    if not account:
        raise HTTPException(404, "Account not found")
    return account


@app.get("/api/meta")
def meta():
    return {
        "today": AS_OF_DATE.isoformat(), "provider": llm.provider_name(), "model": llm.model_label(),
        "email_provider": email_sender.provider_name(),
    }


@app.get("/api/today")
def today(limit: int = 200, offset: int = 0):
    """The prioritized list, from saved analyses in one query. Never waits for the AI."""
    return crm.today(min(limit, 500), offset)


@app.get("/api/accounts/{customer_id}")
def account(customer_id: str):
    a = _account_or_404(customer_id)
    return {**a, "analysis": crm.get_analysis(a), "history_summary": crm.get_history(customer_id)}


@app.post("/api/accounts/{customer_id}/reanalyze")
def reanalyze(customer_id: str):
    """Force a fresh analysis: forget the saved one's hash and queue the client."""
    _account_or_404(customer_id)
    db.execute("UPDATE analyses SET input_hash = '' WHERE customer_id = ?", customer_id)
    jobs.enqueue(customer_id)
    return {"queued": True}


@app.post("/api/accounts/{customer_id}/draft")
def draft(customer_id: str, req: DraftRequest):
    a = _account_or_404(customer_id)
    return _ndjson(crm.draft_stream(a, crm.get_analysis(a), req.instructions))


@app.post("/api/accounts/{customer_id}/interactions")
def log_interaction(customer_id: str, body: NewInteraction):
    if not body.notes.strip():
        raise HTTPException(400, "Notes are required")
    a = _account_or_404(customer_id)
    created = crm.add_interaction(a, body.contact_id, body.type, body.occurred_at, body.notes)
    jobs.enqueue(customer_id)  # saved now; the AI work happens in the worker
    return created


@app.post("/api/accounts/{customer_id}/send")
def send_email(customer_id: str, body: SendEmail):
    a = _account_or_404(customer_id)
    contact = next((c for c in a["contacts"] if c["id"] == body.contact_id), None) or (a["contacts"] or [None])[0]
    if not contact:
        raise HTTPException(400, "This account has no contact to email")
    try:
        result = email_sender.send(customer_id, contact["email"], body.subject, body.body)
    except email_sender.EmailError as e:
        raise HTTPException(503, str(e))
    first_line = next((l for l in body.body.splitlines() if l.strip() and not l.lower().startswith(("hi", "hello", "dear"))), "")
    crm.add_interaction(a, contact["id"], "email", None, f'Sent email "{body.subject}": {first_line}')
    jobs.enqueue(customer_id)
    return result


@app.post("/api/clients")
def create_client(body: NewClient):
    if not body.name.strip() or not body.contact_name.strip():
        raise HTTPException(400, "Client name and contact name are required")
    created = crm.add_client(body.name, body.status, body.contact_name, body.contact_email, body.contact_role)
    jobs.enqueue(created["id"])  # makes the new client findable by meaning, too
    return created


@app.get("/api/outbox")
def outbox():
    return db.rows("SELECT * FROM outbox ORDER BY id DESC")


@app.get("/api/jobs")
def job_counts():
    """Queue status, e.g. {"done": 40, "queued": 2}. Handy to watch the worker."""
    return jobs.counts()


@app.get("/api/search")
def search(q: str = ""):
    return crm.search(q)


@app.post("/api/ask")
def ask(q: Question):
    if not q.question.strip():
        raise HTTPException(400, "Question is required")
    if llm.provider_name() == "rules":
        raise HTTPException(503, "Ask needs an LLM. Set LLM_PROVIDER in backend/.env.")
    return _ndjson(crm.ask_events(q.question))
