"""SQLite storage, seeded from the sample CSVs on first run."""
import csv
import sqlite3
from pathlib import Path

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
    name TEXT NOT NULL, email TEXT, role TEXT
);
CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
    contact_id TEXT REFERENCES contacts(id), type TEXT NOT NULL,
    occurred_at TEXT NOT NULL, notes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS interactions_by_customer ON interactions(customer_id, occurred_at);
-- One cached AI analysis per account. input_hash changes whenever the
-- account's interactions change, which tells the worker to re-analyze.
CREATE TABLE IF NOT EXISTS analyses (
    customer_id TEXT PRIMARY KEY REFERENCES customers(id),
    input_hash TEXT NOT NULL, source TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL
);
-- Emails "sent" from the app. With EMAIL_PROVIDER=outbox they only live here.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL REFERENCES customers(id),
    to_email TEXT NOT NULL, from_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
    provider TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);

-- Search chunks: one per note, plus one "profile" (name, people) and one "ai_view"
-- (AI summary + key facts) per client. FTS5 gives keyword search ranked by BM25;
-- porter stemming lets "calls" match "call".
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    chunk_id UNINDEXED, customer_id UNINDEXED, kind UNINDEXED, text, tokenize = 'porter unicode61'
);
-- New notes become searchable by keyword the moment they are saved.
CREATE TRIGGER IF NOT EXISTS index_new_note AFTER INSERT ON interactions BEGIN
    INSERT INTO chunks (chunk_id, customer_id, kind, text) VALUES (new.id, new.customer_id, 'note', new.notes);
END;
-- One meaning vector per chunk, added by the worker.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id TEXT NOT NULL, model TEXT NOT NULL, customer_id TEXT NOT NULL, vector BLOB NOT NULL,
    PRIMARY KEY (chunk_id, model)
);

-- Rolling summary of a client's older notes, so prompts stay small for long histories.
CREATE TABLE IF NOT EXISTS history_summaries (
    customer_id TEXT PRIMARY KEY REFERENCES customers(id),
    notes_covered INTEGER NOT NULL, summary TEXT NOT NULL, key_facts TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- Background work queue: one "refresh this client" job per change.
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, run_after TEXT NOT NULL, last_error TEXT, created_at TEXT NOT NULL
);
-- At most one waiting job per client: five quick notes still mean one refresh.
CREATE UNIQUE INDEX IF NOT EXISTS one_queued_job_per_client ON jobs(customer_id) WHERE status = 'queued';
"""

# Profile chunk: client name, status and people, so search finds a client by who works there.
PROFILE_SQL = """
INSERT INTO chunks (chunk_id, customer_id, kind, text)
SELECT 'profile:' || c.id, c.id, 'profile',
       c.name || ' (' || c.status || '). People: ' || COALESCE(GROUP_CONCAT(ct.name || ', ' || ct.role, '; '), 'none')
FROM customers c LEFT JOIN contacts ct ON ct.customer_id = c.id
WHERE ?1 IS NULL OR c.id = ?1
GROUP BY c.id
"""


def connect() -> sqlite3.Connection:
    # The worker and the API share one file: wait briefly for a lock instead of failing.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def init_db() -> None:
    with connect() as conn:
        # WAL lets the API keep reading while the worker writes.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        if not conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]:
            # The note trigger indexes interactions as they are seeded.
            for table in ("customers", "contacts", "interactions"):
                rows = _load_csv(DATA_DIR / f"{table}.csv")
                cols = list(rows[0].keys())
                conn.executemany(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [tuple(r[c] for c in cols) for r in rows],
                )
        if not conn.execute("SELECT COUNT(*) FROM chunks WHERE kind = 'note'").fetchone()[0]:
            # A DB created before search existed: index its notes once.
            conn.execute("INSERT INTO chunks (chunk_id, customer_id, kind, text) "
                         "SELECT id, customer_id, 'note', notes FROM interactions")
        if not conn.execute("SELECT COUNT(*) FROM chunks WHERE kind = 'profile'").fetchone()[0]:
            conn.execute(PROFILE_SQL, (None,))


def index_profile(customer_id: str) -> None:
    """(Re)build one client's profile chunk, e.g. after the client or a contact is added."""
    with connect() as conn:
        conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (f"profile:{customer_id}",))
        conn.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (f"profile:{customer_id}",))
        conn.execute(PROFILE_SQL, (customer_id,))


def rows(sql: str, *params) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(sql: str, *params) -> dict | None:
    result = rows(sql, *params)
    return result[0] if result else None


def execute(sql: str, *params) -> None:
    with connect() as conn:
        conn.execute(sql, params)
