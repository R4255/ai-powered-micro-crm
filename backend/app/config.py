import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "crm.db"))

# The sample data ends on 2026-08-31, so "today" is pinned to the day after.
# Using the wall clock would make every account look months stale.
AS_OF_DATE = date.fromisoformat(os.getenv("AS_OF_DATE", "2026-09-01"))

# "ollama" (tested) | "gemini" (supported, untested) | "rules" (no LLM, deterministic fallback)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")

# Short business context the model needs to give sensible advice.
BUSINESS_CONTEXT = os.getenv(
    "BUSINESS_CONTEXT",
    "You sell an AI phone receptionist to dental practices: it answers calls "
    "(incl. after-hours), books appointments and writes call summaries. "
    "The user is the founder/sole salesperson managing prospects and customers.",
)

# Email sending. "outbox" (default) only stores emails locally, nothing is sent.
# "smtp" is a placeholder for real sending; see app/email_sender.py.
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "outbox").lower()
EMAIL_FROM = os.getenv("EMAIL_FROM", "you@yourbusiness.example")

# Embeddings for meaning-based search (Ollama, runs locally).
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# Prompt size limits, so prompts stay the same size however much data there is.
RECENT_NOTES = 15      # analysis and drafts read the latest notes word for word...
SUMMARY_BATCH = 10     # ...plus a rolling summary, updated in batches of 10 older notes
ASK_TOP_CHUNKS = 20    # Ask reads only the 20 best search results
