# Micro-CRM: an AI follow-up assistant

A small CRM for a solo founder selling an AI phone receptionist to dental practices.
It answers one question every morning: **"Who do I need to talk to today, why, and what should I say?"**

<!-- TODO: add a screenshot of the Today view here -->

## Run it

**Requirements:** Python 3.11+, Node 18+, [Ollama](https://ollama.com) running locally.

```bash
ollama pull qwen2.5:7b-instruct      # the LLM
ollama pull nomic-embed-text         # embeddings for meaning-based search

# 1. Backend (http://127.0.0.1:8000)
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --port 8000

# 2. Frontend (http://localhost:5173), in a second terminal
cd frontend
npm install && npm run dev
```

The SQLite DB is created and seeded from `backend/data/*.csv` on first start (delete `backend/crm.db` to reset).
On a fresh start the list shows a simple rules-based ranking right away, and the background worker replaces it
with AI analyses one client at a time (about 20s each on a laptop). The top bar shows "AI is updating N clients…".

`LLM_PROVIDER=rules` runs with no AI at all. `LLM_PROVIDER=gemini` is supported in `app/llm.py` but untested.

**Checks** (run them on a fresh copy of the data, e.g. `DB_PATH=/tmp/eval.db`, so your own notes don't skew them):

| Command | What it checks | Result |
|---|---|---|
| `.venv/bin/python eval.py --refresh` | priorities for the cases that matter (Evergreen *on hold*, Central Ave *urgent*, …) | 8/8 |
| `.venv/bin/python eval.py --retrieval` | search finds the right client for 6 questions, including one with no shared keyword | 6/6 |
| `.venv/bin/python eval.py --ask` | Ask picks the right tool and client (and how fast) | 6/6, ~1s each |
| `.venv/bin/python scale_test.py` | 313 clients and a 60-note client: search, prompt size, rolling summary | all pass |

## What I built and why

The user is a busy owner, not a CRM admin. CRMs usually fail them by making them do two jobs: data entry and
remembering what to do with it. I focused on the **daily decision loop**:

1. **Today list (home).** Every client is sorted into *To do today / This week / On hold / All good*, with a one-line
   *why*, a concrete next step, the right contact, and a due or revisit date.
2. **Client page.** An AI summary, a short "good to know" list, and the history. The notes the AI **cited** as evidence
   are highlighted, so you can check instead of trusting.
3. **Write the email for me.** A draft that refers to the real history, streamed as it's written. It flags any
   number or date that isn't in the notes. You edit it and click **Send**. Send only stores it in a local outbox.
4. **Add a note / New client.** Saving is instant. The AI work happens in the background, and the page refreshes when it's done.
5. **Two search bars.** 🔍 **Simple search** filters the list as you type (plain SQL text match, no AI).
   ✨ **Ask AI** answers questions like "who uses Dentrix?", "who is on hold?" or "email Julia about the call".

### Why AI here, and not just rules

The sample data is full of signals that only make sense when you read the notes. The rules baseline (`LLM_PROVIDER=rules`)
makes exactly the mistakes you'd expect:

| Client | Rules (days since contact plus keywords) | AI reading the notes |
|---|---|---|
| Northstar: 3-location proposal, no reply | *All good* (contacted 3 days ago) | Follow up on the proposal |
| Evergreen: "Do not push before September planning meeting" | relies on an exact keyword match | *On hold*, revisit after the meeting |
| Riverbend: opening a 2nd location in October, stalled since July | generic "quiet prospect" | Re-engage now; the October opening is the hook |
| Oak & Pine: issue resolved, "no follow-up required" | *This week* (91 days silent) | *All good* |

**Code computes the facts** (days since the last touch, counts, dates), and **the LLM interprets intent and timing**
(deadlines, "don't push", unanswered questions). The LLM never does date math, which small models get wrong.

## How the AI works

**The rule everywhere: the AI either makes a decision, as JSON checked against a schema (analysis, tool choice), or writes text for a person to read (answers, drafts). Code does everything else.**
The AI never sends an email, never writes to the database directly, and can only name clients and notes it was shown.

```
WRITE (note / sent email / new client)
  → saved to SQLite instantly; an SQLite trigger adds the note to the keyword index (BM25)
  → one job queued for that client → the worker runs:
       embed new chunks → rolling summary (every 10 old notes) → re-analyze if the notes changed
       → update the client's "AI view" search chunk

READ (Today list)   → saved analyses, one SQL query. Never waits for the AI.
DRAFT email         → this client's context (+ relevant older notes) → AI writes (streamed) → code checks → you edit and send
ASK AI              → AI picks ONE tool from the question alone (~1s) → code runs it (SQL or BM25 + vector + RRF)
                      → only "answer" and "draft_email" call the AI again, streamed
```

### 1. Client analysis (runs in the background)
- **Prompt:** one client's contacts, the computed facts, the rolling summary of older notes, and the latest notes.
- **Output (`Analysis` in `app/schemas.py`):** priority, reason, next step, contact, due / on-hold date, summary,
  good-to-know, evidence note ids. Code drops evidence ids that don't exist and contact ids from another client.
- **Saved** in the `analyses` table with a hash of the client's notes. The worker re-analyzes only when that hash changes.

### 2. Rolling history summary (keeps prompts the same size)
- The AI always reads the **latest 15 notes word for word**. Older notes are folded, **10 at a time**, into one
  summary plus a list of **key facts** (systems they use, who decides, promises, deadlines).
- The prompt says to keep every key fact unless a newer note contradicts it, so early details survive.
  The scale test checks that facts from notes #3 and #7 are still there after 60 notes.
- A prompt never holds more than 24 notes, however long the history is. Original notes are never deleted:
  the history view and search still use them.

### 3. Search: keyword + meaning (`app/retrieval.py`)
- **Chunks:** one per note, one "profile" per client (name, people, roles), and one "AI view" per client (summary + key
  facts, often closer to how people phrase questions).
- **Keyword (BM25):** SQLite FTS5 with stemming. Strong on exact terms: "Dentrix", names.
- **Meaning (vectors):** `nomic-embed-text` through Ollama, computed once by the worker. Finds "talking to an AI" for
  "worried about robots?".
- **RRF:** each list gives a chunk `1/(60 + rank)`, and the scores add up. No weights to tune.

### 4. Ask AI: the AI picks a tool first, code runs it
The first AI call sees **only a fixed system prompt (the tools and a few examples) plus the question**. It returns
a tool and its arguments as JSON, about 20 tokens:

| Tool | Example | What code does | Second AI call? |
|---|---|---|---|
| `list_by_status(priority)` | "who is on hold?" | SQL on the saved analyses | no |
| `find_clients(query)` | "who uses Dentrix?" | BM25 + vector + RRF; the card's "why" is **the matching note** from the DB | no |
| `answer(query)` | "what are clients worried about?" | the same search; the AI writes a short answer from the top 20 chunks | yes, streamed |
| `draft_email(client, instructions)` | "email Julia about the call" | code resolves "Julia" to a client and contact, then the draft runs | yes, streamed |

**Why it's fast: the KV cache.** The system prompt is identical for every question, so Ollama keeps its reading of it in
the KV cache and only reads the few new tokens of the question: 354 tokens in 0.2s instead of about 2.6s.
The prefix is warmed at startup, and again whenever the worker's queue empties, because the worker's own prompts push
it out of the cache. Lookups finish in about 1s. Answers and drafts show their first words after about 5–8s.

Client cards come from the database. The AI never writes client lists, and it can't invent a client.

### 5. Draft email
Context is the same bounded client view, plus, for long histories, the 5 older notes most relevant to the next step
(the same search, limited to this client). The AI writes plain text (`Subject: …` then the body), streamed to the
screen. **Code** picks the contact (the one named in Ask, else the analysis's suggestion) and, when the text is finished,
flags numbers that don't appear in the notes.

## Production readiness

**Built and tested:**
- **Durable job queue** (`app/jobs.py`, `jobs` table). Jobs survive restarts, there's at most one queued job per client,
  failures retry with growing waits (10s, 20s, 40s, 80s), and the job is marked failed after 5 tries. A sweep every
  5 minutes queues clients that were never analyzed or have unembedded chunks (e.g. after an import). Locally the
  worker is a thread. In production the same loop runs as its own process: `python -m app.jobs`.
- **Bounded prompts.** Analysis and drafts hold at most 24 notes plus a summary. Ask's router holds only the question,
  and its answer step holds 20 chunks. With 313 clients the answer prompt was the same size as with 12.
- **Cache-friendly prompts.** Fixed text first, changing text last, so Ollama's KV cache reuses the fixed part.
- **Reads never call the AI.** The Today list is one SQL query with `limit`/`offset`.
- **Logging.** Each AI call logs its feature, duration, and Ollama's token counts and timings.
- **Prompt versioning.** `ANALYSIS_VERSION` is part of the cache key, so a prompt change re-analyzes everyone once, through the queue.
- **SQLite in WAL mode**, so the API keeps reading while the worker writes.

**Would change for a real deployment (not built):**
- Postgres instead of SQLite, and a vector index (pgvector or sqlite-vec) instead of the Python cosine loop.
  The loop takes about 180 ms at 1,600 chunks; it would be too slow at hundreds of thousands.
- Login, with each user seeing only their own clients.
- A real email provider behind `app/email_sender.py`, still only on an explicit click.
- A daily job that re-checks clients whose due date or on-hold date has arrived, and the real clock instead of `AS_OF_DATE`.
- Running the eval on every prompt or model change.

**Measured on this laptop (Apple M2, qwen2.5 7B on Ollama):**

| What | Time |
|---|---|
| Ask: status or "who…" question | **~1s** (router 1.0s + tool ~0.1s) |
| Ask: written answer | first words ~7s, done ~10s |
| Draft (panel or "email X" in Ask) | first words ~5s, done ~10s |
| AI analysis | ~15–25s per client, in the background |

What's left is mostly the model *writing* (7–16 tokens/s on this chip) and reading the search results for answers and
drafts, which change every time and can't be cached. A faster machine or a hosted model would cut both.

## Assumptions and simplifications

- Single user, no auth. The "business" is inferred from the data and set in `BUSINESS_CONTEXT`.
- **Everything is local.** With Ollama, no data leaves the machine. No email is ever sent: Send stores it in the
  local `outbox` table and adds a note to the history.
- "Today" is pinned to **2026-09-01** (`AS_OF_DATE`), because the data ends on Aug 31 and the real clock would make every client look stale.
- Interactions have no direction (inbound vs. outbound), so the model infers it from the wording.
- Notes are append-only (no editing or deleting), which keeps the rolling summary simple.
- `find_clients` doesn't use the AI to judge relevance: it shows clients whose best search hit is at least half as
  strong as the top hit (at most 5), so a weak match can appear next to the right one. The matching note is shown as
  the reason, so it's easy to spot. A re-ranking step would tighten this, at the cost of another AI call.
- With meaning-only questions, RRF can rank a chunk found only by vectors fairly low: rank 17 of 20 in the scale test.
  It still reaches the model, but a larger `ASK_TOP_CHUNKS` or a re-ranking step would make this more robust.

## What I'd build next

1. **Real inbox and calendar sync** (Gmail/Outlook), so interactions are logged automatically. That's the biggest cut to manual effort.
2. **The daily due-date job** described above, plus a morning digest of the "To do today" list.
3. **Snooze and override.** Let the user change a priority, and feed that back into the prompt as examples.
4. **Deal value fields**, so ranking can weigh revenue (5-location Evergreen vs. a single office).
5. **Structured extraction on save:** pull out dates, commitments and feature requests into fields, enabling
   product rollups ("3 customers asked for SMS").
