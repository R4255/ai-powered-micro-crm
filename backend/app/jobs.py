"""A small job queue stored in SQLite, and the worker that processes it.

Any change to a client (new note, sent email, new client) queues one "refresh"
job for that client. The worker then runs, in order:
  1. embed the client's chunks that have no vector yet (new notes, profile)
  2. fold older notes into the rolling history summary (every SUMMARY_BATCH notes)
  3. re-analyze, if the notes changed since the last analysis
  4. update the client's "AI view" search chunk (summary + key facts) and embed it

Jobs live in the database, so they survive restarts, and a failed job is retried
with growing waits. Locally the worker is a thread inside the API process; in
production the same loop runs as its own process (`python -m app.jobs`).
"""
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from . import crm, db, llm, retrieval

log = logging.getLogger("crm.jobs")

MAX_ATTEMPTS = 5
SWEEP_EVERY_S = 300


def _now(delay_s: float = 0) -> str:
    return (datetime.now() + timedelta(seconds=delay_s)).isoformat(timespec="seconds")


def enqueue(customer_id: str) -> None:
    """Queue a refresh. A client already waiting in the queue is not queued twice."""
    db.execute(
        "INSERT OR IGNORE INTO jobs (customer_id, status, run_after, created_at) VALUES (?, 'queued', ?, ?)",
        customer_id, _now(), _now(),
    )


def counts() -> dict[str, int]:
    return {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


def refresh_client(customer_id: str) -> None:
    account = crm.get_account(customer_id)
    if not account:
        return
    try:
        retrieval.embed_missing(customer_id)
    except llm.LLMError as e:
        log.warning("embedding skipped for %s: %s", customer_id, e)  # keyword search still works
    history = crm.update_history_summary(account)
    if account["interactions"] and crm.needs_analysis(account):
        crm.analyze(account)
    analysis = crm.get_analysis(account)
    if analysis["source"] != "rules":
        retrieval.index_ai_view(customer_id, crm.ai_view_text(account, analysis, history))
        try:
            retrieval.embed_missing(customer_id)
        except llm.LLMError as e:
            log.warning("embedding skipped for %s: %s", customer_id, e)


def _claim() -> dict | None:
    """Take the next due job and mark it running, in one statement so two workers never take the same job."""
    with db.connect() as conn:
        r = conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1 WHERE id = ("
            "  SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY run_after, id LIMIT 1"
            ") RETURNING *", (_now(),),
        ).fetchone()
        return dict(r) if r else None


def _finish(job: dict, error: str | None) -> None:
    if error is None:
        db.execute("UPDATE jobs SET status = 'done', last_error = NULL WHERE id = ?", job["id"])
        return
    if job["attempts"] >= MAX_ATTEMPTS:
        log.error("job %s for %s failed for good: %s", job["id"], job["customer_id"], error)
        db.execute("UPDATE jobs SET status = 'failed', last_error = ? WHERE id = ?", error, job["id"])
        return
    delay = 10 * 2 ** (job["attempts"] - 1)  # 10s, 20s, 40s, 80s
    log.warning("job %s for %s failed (attempt %d), retrying in %ss: %s",
                job["id"], job["customer_id"], job["attempts"], delay, error)
    try:
        db.execute("UPDATE jobs SET status = 'queued', run_after = ?, last_error = ? WHERE id = ?",
                   _now(delay), error, job["id"])
    except sqlite3.IntegrityError:
        # A newer job for this client is already queued; it will redo the work.
        db.execute("UPDATE jobs SET status = 'done', last_error = ? WHERE id = ?", error, job["id"])


def run_once() -> bool:
    """Process one job. Returns False when there was nothing to do."""
    job = _claim()
    if not job:
        return False
    try:
        refresh_client(job["customer_id"])
        _finish(job, None)
    except Exception as e:  # noqa: BLE001 - any failure is recorded and retried
        _finish(job, str(e)[:500])
    return True


def recover() -> None:
    """After a crash or restart, put jobs that were mid-run back in the queue."""
    for job in db.rows("SELECT * FROM jobs WHERE status = 'running'"):
        try:
            db.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", job["id"])
        except sqlite3.IntegrityError:
            db.execute("UPDATE jobs SET status = 'done' WHERE id = ?", job["id"])


def sweep() -> int:
    """Queue clients that need work but have no job: never analyzed, or chunks without vectors.

    Catches data added outside the app (an import) and anything a crash left behind.
    """
    if llm.provider_name() == "rules":
        return 0
    ids = db.rows(
        "SELECT DISTINCT customer_id FROM interactions WHERE customer_id NOT IN "
        "  (SELECT customer_id FROM analyses WHERE source != 'rules') "
        "UNION SELECT c.customer_id FROM chunks c LEFT JOIN chunk_vectors v "
        "  ON v.chunk_id = c.chunk_id AND v.model = ? WHERE v.chunk_id IS NULL",
        llm.embed_model_label() or "",
    )
    pending = {r["customer_id"] for r in db.rows("SELECT customer_id FROM jobs WHERE status IN ('queued', 'running')")}
    todo = [r["customer_id"] for r in ids if r["customer_id"] not in pending]
    for cid in todo:
        enqueue(cid)
    if todo:
        log.info("sweep queued %d clients", len(todo))
    return len(todo)


def run_forever(stop: threading.Event | None = None) -> None:
    recover()
    last_sweep, busy = 0.0, False
    while not (stop and stop.is_set()):
        if time.monotonic() - last_sweep > SWEEP_EVERY_S:
            sweep()
            last_sweep = time.monotonic()
        if run_once():
            busy = True
            continue
        if busy:
            # Queue just emptied. The worker's prompts pushed Ask's router prompt out of
            # Ollama's KV cache; put it back so the next question is fast again.
            crm.prime_router()
            busy = False
        time.sleep(1)


def start_in_background() -> threading.Thread:
    thread = threading.Thread(target=run_forever, name="crm-worker", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    # Production: run the worker as its own process, next to one or more API processes.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    db.init_db()
    run_forever()
