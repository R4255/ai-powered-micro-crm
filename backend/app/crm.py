"""Domain logic: account context, AI analysis (cached, rules fallback), history summaries,
drafts, Ask (the AI picks a tool, code runs it) and plain search.

Every AI call returns JSON checked against a schema; code does everything else.
"""
import hashlib
import json
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta

from . import db, llm, retrieval
from .config import AS_OF_DATE, ASK_TOP_CHUNKS, BUSINESS_CONTEXT, RECENT_NOTES, SUMMARY_BATCH
from .schemas import Analysis, HistorySummary, ToolCall

# ---------------------------------------------------------------- data access


def get_customers() -> list[dict]:
    return db.rows("SELECT * FROM customers ORDER BY id")


def get_account(customer_id: str) -> dict | None:
    customer = db.row("SELECT * FROM customers WHERE id = ?", customer_id)
    if not customer:
        return None
    contacts = db.rows("SELECT * FROM contacts WHERE customer_id = ? ORDER BY id", customer_id)
    interactions = db.rows(
        "SELECT * FROM interactions WHERE customer_id = ? ORDER BY occurred_at, id", customer_id
    )
    return {**customer, "contacts": contacts, "interactions": interactions, "facts": _facts(customer, interactions)}


def _days_ago(iso: str) -> int:
    return (AS_OF_DATE - date.fromisoformat(iso)).days


def _facts(customer: dict, interactions: list[dict]) -> dict:
    """Deterministic signals computed in code. The LLM interprets them; it never has to do date math."""
    last = interactions[-1] if interactions else None
    return {
        "today": AS_OF_DATE.isoformat(),
        "interaction_count": len(interactions),
        "last_interaction_at": last["occurred_at"] if last else None,
        "days_since_last_interaction": _days_ago(last["occurred_at"]) if last else None,
        "last_interaction_type": last["type"] if last else None,
        "days_as_account": _days_ago(customer["created_at"]),
    }


def is_updating(customer_id: str) -> bool:
    """True while the worker has queued or running work for this client."""
    return bool(db.row("SELECT 1 AS x FROM jobs WHERE customer_id = ? AND status IN ('queued', 'running')", customer_id))


# ------------------------------------------------------- history and context


def get_history(customer_id: str) -> dict | None:
    h = db.row("SELECT * FROM history_summaries WHERE customer_id = ?", customer_id)
    return {**h, "key_facts": json.loads(h["key_facts"])} if h else None


def _shown_notes(account: dict, history: dict | None) -> list[dict]:
    """Notes that go into prompts word for word: everything not yet folded into the summary.

    That is the latest RECENT_NOTES plus fewer than SUMMARY_BATCH older ones, so at
    most RECENT_NOTES + SUMMARY_BATCH - 1 notes however long the history is.
    """
    ints = account["interactions"]
    covered = history["notes_covered"] if history else 0
    return ints[min(covered, max(0, len(ints) - RECENT_NOTES)):]


def account_context(account: dict, history: dict | None = None) -> str:
    """Render one account as text for prompts: profile, facts, rolling summary, recent notes."""
    contacts = {c["id"]: c for c in account["contacts"]}
    shown = _shown_notes(account, history)
    lines = [
        f"ACCOUNT {account['id']}: {account['name']} (status: {account['status']}, "
        f"account created {account['created_at']})",
        "Contacts:",
        *[f"  - {c['id']}: {c['name']}, {c['role']} <{c['email']}>" for c in account["contacts"]],
        "Facts: " + json.dumps(account["facts"]),
    ]
    if history:
        lines += [
            f"Earlier history (summary of the oldest {history['notes_covered']} notes): {history['summary']}",
            "Key facts from earlier history:",
            *[f"  - {f}" for f in history["key_facts"]],
        ]
    lines.append(f"Interaction timeline (latest {len(shown)} of {len(account['interactions'])}, oldest first):")
    for i in shown:
        lines.append(_note_line(i, contacts))
    return "\n".join(lines)


def _note_line(i: dict, contacts: dict) -> str:
    who = contacts.get(i["contact_id"], {}).get("name", "unknown")
    return (f"  - [{i['id']}] {i['occurred_at']} ({_days_ago(i['occurred_at'])} days ago) "
            f"{i['type']} with {who}: {i['notes']}")


HISTORY_SYSTEM = f"""You keep a running summary of one client relationship in a CRM. {BUSINESS_CONTEXT}
You get the current summary and key facts (empty at first) and the next batch of older notes.
Return an updated summary and key facts that cover everything so far.
- Keep every existing key fact unless a newer note clearly contradicts or resolves it.
- Add durable facts: systems they use, who decides, prices or terms discussed, promises we made,
  deadlines, feature requests, concerns.
- Never invent anything that is not in the notes. Each key fact under 15 words, at most 15 facts."""


def update_history_summary(account: dict) -> dict | None:
    """Fold older notes into the rolling summary, SUMMARY_BATCH notes at a time.

    Only notes that have dropped out of the latest RECENT_NOTES are folded, and each
    note is folded once, so this costs one small LLM call per SUMMARY_BATCH notes.
    """
    history = get_history(account["id"])
    covered = history["notes_covered"] if history else 0
    older = account["interactions"][:max(0, len(account["interactions"]) - RECENT_NOTES)]
    contacts = {c["id"]: c for c in account["contacts"]}
    while len(older) - covered >= SUMMARY_BATCH:
        batch = older[covered:covered + SUMMARY_BATCH]
        prompt = (
            f"CLIENT: {account['name']} ({account['status']})\n"
            f"CURRENT SUMMARY: {history['summary'] if history else '(none yet)'}\n"
            "CURRENT KEY FACTS:\n" + "\n".join(f"  - {f}" for f in (history["key_facts"] if history else [])) +
            "\nNEXT NOTES (oldest first):\n" + "\n".join(_note_line(i, contacts) for i in batch)
        )
        result = llm.generate(HISTORY_SYSTEM, prompt, HistorySummary)
        covered += len(batch)
        db.execute(
            "INSERT OR REPLACE INTO history_summaries VALUES (?, ?, ?, ?, ?)",
            account["id"], covered, result.summary, json.dumps(result.key_facts),
            datetime.now().isoformat(timespec="seconds"),
        )
        history = get_history(account["id"])
    return history


# ------------------------------------------------------------------ analysis

ANALYSIS_SYSTEM = f"""You are the relationship assistant inside a micro-CRM. {BUSINESS_CONTEXT}

Your job: read one account's history and decide what the user should do next and how urgently.
Judge from the *meaning* of the notes, not only from how long ago the last touch was:
- Explicit instructions in notes (e.g. "do not push", "decision in September") override staleness.
  If the right move is to wait, use priority=waiting and set snooze_until to when to re-engage.
- Deadlines or time-sensitive events the contact mentioned (onboarding dates, new locations
  opening, "this week") create urgency. Count the days left relative to today.
- A question the contact asked that we have not answered, or a meeting they requested, is urgent.
- Use priority=urgent when the contact asked for something "this week"/by a specific near date, or an
  unconfirmed deadline is within ~14 days. A warm buyer waiting on us is urgent, not soon.
- A proposal/pricing sent with no reply for 5+ days deserves a follow-up.
- Prospects silent for 30+ days: one concise re-engagement attempt, not daily chasing.
- Customers: look for expansion signals (new locations, more providers), unanswered feature
  requests, and long silence (90+ days = churn-risk check-in). If nothing is owed and things are
  going well, priority=healthy, though you may still note an opportunity in good_to_know.
- Never invent facts, prices, dates or product capabilities not present in the notes.
- Cite the interaction ids your judgement relies on in `evidence`.
Today's date is {AS_OF_DATE.isoformat()}."""

# Bump when the Analysis schema or prompt changes, so the worker re-analyzes everyone once.
ANALYSIS_VERSION = 3


def _input_hash(account: dict) -> str:
    payload = json.dumps([ANALYSIS_VERSION, account["status"], account["interactions"]], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def rules_analysis(account: dict) -> Analysis:
    """Deterministic fallback, shown until the AI has analyzed a client (or when no LLM is configured).

    Intentionally simple: it shows what a non-AI CRM would conclude, which is a
    useful baseline to compare the AI against.
    """
    ints = account["interactions"]
    last = ints[-1] if ints else None
    days = account["facts"]["days_since_last_interaction"] or 0
    text = " ".join(i["notes"].lower() for i in ints[-3:])
    primary = account["contacts"][0]["id"] if account["contacts"] else None
    is_customer = account["status"] == "customer"

    if not ints:
        priority, reason = "soon", "New client with no interactions yet."
    elif "do not push" in text or "not push" in text:
        priority, reason = "waiting", "Notes say not to push right now."
    elif "this week" in text or re.search(r"before (september|october|\w+ \d+)", text):
        priority, reason = "urgent", "Contact mentioned a near-term timeline."
    elif not is_customer and days >= 5 and any(k in text for k in ("proposal", "pricing", "no response")):
        priority, reason = "soon", f"No reply {days} days after proposal/pricing."
    elif not is_customer and days >= 21:
        priority, reason = "soon", f"Prospect quiet for {days} days."
    elif is_customer and days >= 90:
        priority, reason = "soon", f"Customer not contacted in {days} days."
    else:
        priority, reason = "healthy", f"Last touch {days} days ago; nothing flagged."

    return Analysis(
        priority=priority,
        reason=reason,
        next_action=("Send an introduction email" if not ints else "Send a short check-in email")
        if priority != "healthy" else "No action needed",
        contact_id=last["contact_id"] if last else primary,
        due_date=(AS_OF_DATE + timedelta(days=1 if priority == "urgent" else 5)).isoformat()
        if priority in ("urgent", "soon") else None,
        snooze_until=None,
        summary=f"{len(ints)} interactions; last was a {last['type']} on {last['occurred_at']}: {last['notes']}"
        if last else "No interactions yet.",
        good_to_know=[],
        evidence=[last["id"]] if last else [],
    )


def get_analysis(account: dict) -> dict:
    """The saved AI analysis, or the rules baseline if there is none yet. Never calls the LLM.

    Keeping the AI out of reads is what makes the list and panels instant; the
    worker (app/jobs.py) keeps the saved analyses up to date in the background.
    """
    cached = db.row("SELECT * FROM analyses WHERE customer_id = ? AND source != 'rules'", account["id"])
    updating = is_updating(account["id"])
    if cached:
        return {**json.loads(cached["payload"]), "source": cached["source"], "updating": updating}
    return {**rules_analysis(account).model_dump(), "source": "rules", "updating": updating}


def needs_analysis(account: dict) -> bool:
    cached = db.row("SELECT input_hash FROM analyses WHERE customer_id = ? AND source != 'rules'", account["id"])
    return not cached or cached["input_hash"] != _input_hash(account)


def analyze(account: dict) -> dict:
    """Run the AI analysis for one client and save it. Raises llm.LLMError on failure."""
    history = get_history(account["id"])
    result = llm.generate(ANALYSIS_SYSTEM, account_context(account, history), Analysis)
    known = {i["id"] for i in account["interactions"]}
    result.evidence = [e for e in result.evidence if e in known]  # drop ids the model made up
    if result.contact_id not in {c["id"] for c in account["contacts"]}:
        result.contact_id = account["contacts"][0]["id"] if account["contacts"] else None
    db.execute(
        "INSERT OR REPLACE INTO analyses VALUES (?, ?, ?, ?, ?)",
        account["id"], _input_hash(account), llm.model_label(), result.model_dump_json(),
        datetime.now().isoformat(timespec="seconds"),
    )
    return {**result.model_dump(), "source": llm.model_label()}


def ai_view_text(account: dict, analysis: dict, history: dict | None) -> str:
    """Text of the client's "AI view" search chunk: the AI's plain-language take, which
    often matches a question's wording better than the raw notes do."""
    facts = (history["key_facts"] if history else []) + analysis.get("good_to_know", [])
    return f"{account['name']}: {analysis['summary']} " + " ".join(facts)


# --------------------------------------------------------------- today list

PRIORITY_RANK = "CASE json_extract(a.payload, '$.priority') " \
                "WHEN 'urgent' THEN 0 WHEN 'soon' THEN 1 WHEN 'waiting' THEN 2 WHEN 'healthy' THEN 3 ELSE 4 END"


def today(limit: int, offset: int) -> list[dict]:
    """The prioritized list in one query: client + saved analysis + contact + last touch + updating."""
    items = []
    for r in db.rows(f"""
        SELECT c.id, c.name, c.status, a.payload, a.source,
               ct.id AS contact_id, ct.name AS contact_name, ct.email AS contact_email, ct.role AS contact_role,
               (SELECT MAX(occurred_at) FROM interactions i WHERE i.customer_id = c.id) AS last_at,
               EXISTS(SELECT 1 FROM jobs j WHERE j.customer_id = c.id AND j.status IN ('queued', 'running')) AS updating
        FROM customers c
        LEFT JOIN analyses a ON a.customer_id = c.id AND a.source != 'rules'
        LEFT JOIN contacts ct ON ct.id = json_extract(a.payload, '$.contact_id')
        ORDER BY {PRIORITY_RANK}, COALESCE(json_extract(a.payload, '$.due_date'), '9999'), c.name
        LIMIT ? OFFSET ?""", limit, offset):
        if r["payload"]:
            analysis = {**json.loads(r["payload"]), "source": r["source"], "updating": bool(r["updating"])}
            contact = {"id": r["contact_id"], "name": r["contact_name"], "email": r["contact_email"],
                       "role": r["contact_role"]} if r["contact_id"] else None
        else:
            # Not analyzed yet (new client): the rules baseline, until the worker gets to it.
            account = get_account(r["id"])
            analysis = get_analysis(account)
            contact = next((x for x in account["contacts"] if x["id"] == analysis["contact_id"]), None)
        items.append({
            "id": r["id"], "name": r["name"], "status": r["status"], "contact": contact, "analysis": analysis,
            "facts": {"last_interaction_at": r["last_at"],
                      "days_since_last_interaction": _days_ago(r["last_at"]) if r["last_at"] else None},
        })
    return items


# ------------------------------------------------------------------ drafting

DRAFT_SYSTEM = f"""You write short follow-up emails on behalf of the user. {BUSINESS_CONTEXT}
Style: warm, direct, plain text, under 120 words, no buzzwords, one clear ask.
Reference specific details from the history so it is obviously personal.
Never promise features, prices, dates or discounts not present in the notes; if something is
unknown (e.g. a roadmap date), offer to find out or discuss on a call instead.
Sign off as "Best," followed by a blank line and "[Your name]".
Output exactly this format and nothing else:
Subject: <subject line>

<email body>"""

NUMBER = re.compile(r"\$?\d[\d,.:%]*")


def _unverified_numbers(text: str, context: str) -> list[str]:
    """Numbers in the draft (prices, dates, counts) that appear nowhere in what the AI was shown."""
    plain = context.replace(",", "").replace("$", "")
    found = []
    for token in NUMBER.findall(text):
        value = token.rstrip(".,:").replace(",", "").replace("$", "")
        if value and value not in plain and token not in found:
            found.append(token.rstrip(".,:"))
    return found


def _split_draft(text: str) -> tuple[str, str]:
    """'Subject: ...' on the first line, the body after it."""
    m = re.match(r"\s*subject:\s*(.*?)\s*\n(.*)", text, re.IGNORECASE | re.DOTALL)
    return (m.group(1).strip(), m.group(2).strip()) if m else ("Following up", text.strip())


def draft_stream(account: dict, analysis: dict, instructions: str | None,
                 contact_id: str | None = None) -> Iterator[dict]:
    """Write a follow-up email, streamed. Code picks the contact and checks the result; nothing is sent.

    Yields {"type": "text"} events as words arrive, then one {"type": "draft"} with the finished draft.
    """
    history = get_history(account["id"])
    shown = _shown_notes(account, history)
    context = account_context(account, history)
    # Long histories: also pull the older notes most relevant to this email (BM25 + vector, this client only).
    if len(shown) < len(account["interactions"]):
        query = " ".join([analysis["next_action"], analysis["reason"], instructions or ""])
        older = retrieval.search(query, k=5, customer_id=account["id"], kinds=("note",),
                                 exclude={i["id"] for i in shown})
        by_id = {i["id"]: i for i in account["interactions"]}
        contacts = {c["id"]: c for c in account["contacts"]}
        if older:
            context += "\nRelevant older notes:\n" + "\n".join(_note_line(by_id[c["chunk_id"]], contacts) for c in older)

    by_contact = {c["id"]: c for c in account["contacts"]}
    contact = by_contact.get(contact_id) or by_contact.get(analysis.get("contact_id")) or \
        (account["contacts"][0] if account["contacts"] else None)
    prompt = (
        f"{context}\n\nRecommended next action: {analysis['next_action']} (reason: {analysis['reason']})\n"
        f"Write the email to: {contact['name'] + ', ' + contact['role'] if contact else 'the main contact'}."
    )
    if instructions:
        prompt += f"\nExtra instructions from the user: {instructions}"

    text = ""
    for chunk in llm.stream_text(DRAFT_SYSTEM, prompt, max_tokens=350):
        text += chunk
        yield {"type": "text", "text": chunk}
    subject, body = _split_draft(text)
    yield {"type": "draft", "draft": {
        "subject": subject, "body": body, "contact_id": contact["id"] if contact else None,
        "warnings": _unverified_numbers(f"{subject}\n{body}", context),
    }}


# ------------------------------------------------- Ask: AI picks a tool first

# Fixed text, identical for every question, so Ollama keeps it in its KV cache and only
# has to read the question itself. Do not put anything that changes in here.
ROUTER_SYSTEM = f"""You are the router inside a CRM. {BUSINESS_CONTEXT}
Read the user's question and pick exactly ONE tool. Reply with JSON only, with just that tool's arguments.
Tools:
- list_by_status(priority): the question is only about a status.
  urgent = "to do today"/urgent, soon = "this week", waiting = "on hold", healthy = "all good".
- find_clients(query): "which/who" questions asking for clients with some trait, need or note.
  query = that trait in a few words.
- answer(query): any other question about the clients that needs a written answer.
  query = what to search the notes for.
- draft_email(client, instructions): the user wants an email written. client = the practice or
  person named in the question; instructions = topic or tone, if any.
Examples:
"who is urgent?" -> {{"tool": "list_by_status", "priority": "urgent"}}
"what's on hold?" -> {{"tool": "list_by_status", "priority": "waiting"}}
"who uses Dentrix?" -> {{"tool": "find_clients", "query": "uses Dentrix"}}
"what are clients worried about?" -> {{"tool": "answer", "query": "concerns worries problems"}}
"email Julia about the call" -> {{"tool": "draft_email", "client": "Julia", "instructions": "about the call"}}"""

ANSWER_SYSTEM = f"""You answer questions about the user's CRM. {BUSINESS_CONTEXT}
You get search results (notes grouped by client) and a question. Answer only from these results;
if they do not contain the answer, say so.
Keep it short: at most 5 sentences, or "- " bullets, one per client, each starting with the
client name exactly as given. No preamble.
Today's date is {AS_OF_DATE.isoformat()}."""


def route(question: str) -> ToolCall:
    """The only AI call every question needs: a fixed prompt plus the question, ~20 tokens out."""
    return llm.generate(ROUTER_SYSTEM, f"QUESTION: {question}", ToolCall, schema_in_prompt=False)


def prime_router() -> None:
    """Put the router prompt in Ollama's KV cache, so the next question only costs its own tokens."""
    llm.prime(ROUTER_SYSTEM, "QUESTION: (warm-up)\n\nRespond ONLY with JSON.")


def _client_cards(picks: list[tuple[str, str | None]]) -> list[dict]:
    """Client cards for the UI from the saved analyses, in the given order."""
    ids = [cid for cid, _ in picks]
    found = {r["id"]: r for r in db.rows(
        "SELECT c.id, c.name, c.status, a.payload FROM customers c "
        "LEFT JOIN analyses a ON a.customer_id = c.id AND a.source != 'rules' "
        f"WHERE c.id IN ({','.join('?' * len(ids))})", *ids)} if ids else {}
    cards = []
    for cid, why in picks:
        if cid not in found:
            continue
        r = found[cid]
        a = json.loads(r["payload"]) if r["payload"] else get_analysis(get_account(cid))
        cards.append({"id": cid, "name": r["name"], "status": r["status"], "priority": a["priority"],
                      "next_action": a["next_action"], "why": why})
    return cards


def _hit_lines(hits: list[dict]) -> dict[str, str]:
    """Readable line per search hit: notes get their date and contact."""
    note_ids = [h["chunk_id"] for h in hits if h["kind"] == "note"]
    notes = {i["id"]: i for i in db.rows(
        "SELECT i.*, ct.name AS contact_name FROM interactions i LEFT JOIN contacts ct ON ct.id = i.contact_id "
        f"WHERE i.id IN ({','.join('?' * len(note_ids))})", *note_ids)} if note_ids else {}
    lines = {}
    for h in hits:
        n = notes.get(h["chunk_id"])
        if n:
            lines[h["chunk_id"]] = f"{n['occurred_at']} {n['type']} with {n['contact_name'] or 'unknown'}: {n['notes']}"
        elif h["kind"] == "profile":
            lines[h["chunk_id"]] = f"Profile: {h['text']}"
        else:  # ai_view text starts with "<client name>: "; the card already shows the name
            lines[h["chunk_id"]] = f"AI summary: {h['text'].split(': ', 1)[-1]}"
    return lines


def _matching_clients(hits: list[dict], max_clients: int = 5) -> list[dict]:
    """Clients whose best hit scores at least half of the top hit, with that hit as the "why".

    The "why" is a real note from the database, not text written by the AI.
    """
    if not hits:
        return []
    lines = _hit_lines(hits)
    best: dict[str, dict] = {}
    for h in hits:  # hits are sorted best first
        best.setdefault(h["customer_id"], h)
    cutoff = hits[0]["score"] * 0.5
    picks = [(cid, lines[h["chunk_id"]]) for cid, h in best.items() if h["score"] >= cutoff]
    return _client_cards(picks[:max_clients])


def _answer_prompt(question: str, hits: list[dict]) -> str:
    lines = _hit_lines(hits)
    grouped: dict[str, list[str]] = {}
    for h in hits:
        grouped.setdefault(h["customer_id"], []).append(f"- {lines[h['chunk_id']]}")
    cards = {c["id"]: c for c in _client_cards([(cid, None) for cid in grouped])}
    blocks = [f"## {cards[cid]['name']} ({cards[cid]['status']}, {cards[cid]['priority']})\n" + "\n".join(ls)
              for cid, ls in grouped.items() if cid in cards]
    return "SEARCH RESULTS:\n\n" + ("\n\n".join(blocks) or "(nothing found)") + f"\n\nQUESTION: {question}"


def _resolve_client(name: str) -> tuple[str, str | None] | None:
    """Map "Julia" / "Northstar" / "Sarah Mitchell" to (customer_id, contact_id or None).

    Plain lookups first (practice name, then person name), then a profile search.
    """
    n = name.strip().lower()
    if not n:
        return None
    practices = db.rows("SELECT id FROM customers WHERE lower(name) LIKE ?", f"%{n}%")
    if len(practices) == 1:
        return practices[0]["id"], None
    people = db.rows("SELECT id, customer_id FROM contacts WHERE lower(name) LIKE ? OR lower(name) LIKE ?",
                     f"{n}%", f"% {n}%")
    if people and len({p["customer_id"] for p in people}) == 1:
        return people[0]["customer_id"], people[0]["id"] if len(people) == 1 else None
    hits = retrieval.search(name, k=1, kinds=("profile",))
    return (hits[0]["customer_id"], None) if hits else None


def ask_events(question: str) -> Iterator[dict]:
    """Answer a question as a stream of events for the UI.

    1. The AI picks a tool (fast: fixed cached prompt + the question, ~20 tokens out).
    2. Code runs it: SQL, search (BM25 + vector + RRF), or client lookup.
    3. Only `answer` and `draft_email` call the AI again, and that text is streamed.
    Events: {"type": "tool", ...} first, then {"type": "text"} chunks, then {"type": "draft"} for emails.
    """
    call = route(question)

    if call.tool == "list_by_status" and call.priority:
        rows = db.rows("SELECT customer_id FROM analyses WHERE source != 'rules' AND json_extract(payload, '$.priority') = ? "
                       "ORDER BY COALESCE(json_extract(payload, '$.due_date'), '9999')", call.priority)
        yield {"type": "tool", "tool": call.tool, "args": {"priority": call.priority},
               "clients": _client_cards([(r["customer_id"], None) for r in rows])}
        if not rows:
            yield {"type": "text", "text": "No clients have that status right now."}
        return

    if call.tool == "draft_email":
        resolved = _resolve_client(call.client or "")
        if not resolved:
            yield {"type": "tool", "tool": "answer", "args": {"client": call.client}, "clients": []}
            yield {"type": "text", "text": f"I couldn't tell which client to write to{' (' + call.client + ')' if call.client else ''}."}
            return
        customer_id, contact_id = resolved
        account = get_account(customer_id)
        yield {"type": "tool", "tool": call.tool, "args": {"client": call.client, "instructions": call.instructions},
               "clients": _client_cards([(customer_id, None)])}
        yield from draft_stream(account, get_analysis(account), call.instructions, contact_id)
        return

    # find_clients / answer (and list_by_status without a priority)
    query = call.query or question
    hits = retrieval.search(query, k=ASK_TOP_CHUNKS)
    searched = {"chunks": len(hits), "clients": len({h["customer_id"] for h in hits})}
    if call.tool == "find_clients":
        clients = _matching_clients(hits)
        yield {"type": "tool", "tool": call.tool, "args": {"query": query}, "clients": clients, "searched": searched}
        if not clients:
            yield {"type": "text", "text": "I couldn't find any matching clients."}
        return
    yield {"type": "tool", "tool": "answer", "args": {"query": query}, "clients": _matching_clients(hits), "searched": searched}
    for chunk in llm.stream_text(ANSWER_SYSTEM, _answer_prompt(question, hits)):
        yield {"type": "text", "text": chunk}


# -------------------------------------------------------------- plain search


def search(query: str) -> dict[str, list[str]]:
    """Keyword search (no AI): client names, people, roles, emails and note text.

    Returns {customer_id: [what matched, ...]} so the UI can filter the list and
    show why each client matched. SQLite LIKE is case-insensitive for ASCII.
    """
    q = query.strip()
    if not q:
        return {}
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    hits = db.rows(
        """
        SELECT id AS customer_id, 'Client: ' || name AS text FROM customers
            WHERE name LIKE ?1 ESCAPE '\\'
        UNION ALL
        SELECT customer_id, 'Person: ' || name || ', ' || role || ' <' || email || '>' FROM contacts
            WHERE name LIKE ?1 ESCAPE '\\' OR role LIKE ?1 ESCAPE '\\' OR email LIKE ?1 ESCAPE '\\'
        UNION ALL
        SELECT customer_id, occurred_at || ': ' || notes FROM interactions
            WHERE notes LIKE ?1 ESCAPE '\\'
        """,
        like,
    )
    results: dict[str, list[str]] = {}
    for h in hits:
        results.setdefault(h["customer_id"], []).append(h["text"])
    return results


# ------------------------------------------------------------------- writes


def _next_id(table: str, prefix: str) -> str:
    n = db.row(f"SELECT COUNT(*) AS n FROM {table}")["n"] + 1
    while db.row(f"SELECT id FROM {table} WHERE id = ?", f"{prefix}_{n:03d}"):
        n += 1
    return f"{prefix}_{n:03d}"


def add_interaction(account: dict, contact_id: str | None, type_: str, occurred_at: str | None, notes: str) -> dict:
    if not contact_id:
        # Infer the contact from a first name mentioned in the note, else the most recent contact.
        lowered = notes.lower()
        match = next((c for c in account["contacts"] if c["name"].split()[0].lower() in lowered), None)
        contact_id = match["id"] if match else (
            account["interactions"][-1]["contact_id"] if account["interactions"] else
            (account["contacts"][0]["id"] if account["contacts"] else None)
        )
    new_id = _next_id("interactions", "int")
    # The index_new_note trigger makes it keyword-searchable immediately.
    db.execute(
        "INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?)",
        new_id, account["id"], contact_id, type_, occurred_at or AS_OF_DATE.isoformat(), notes.strip(),
    )
    return {"id": new_id}


def add_client(name: str, status: str, contact_name: str, contact_email: str, contact_role: str) -> dict:
    customer_id = _next_id("customers", "cust")
    contact_id = _next_id("contacts", "contact")
    with db.connect() as conn:
        conn.execute("INSERT INTO customers VALUES (?, ?, ?, ?)", (customer_id, name.strip(), status, AS_OF_DATE.isoformat()))
        conn.execute("INSERT INTO contacts VALUES (?, ?, ?, ?, ?)",
                     (contact_id, customer_id, contact_name.strip(), contact_email.strip(), contact_role.strip()))
    db.index_profile(customer_id)
    return {"id": customer_id}
