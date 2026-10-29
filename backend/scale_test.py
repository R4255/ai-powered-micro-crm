"""Scale check on generated data, in a throwaway database (your crm.db is not touched).

  1. 300 clients: does search still find one planted fact, and does the Ask prompt stay the same size?
  2. One client with 60 notes: does the rolling summary keep early facts while the prompt stays small?

Run: .venv/bin/python scale_test.py      (needs Ollama; takes a few minutes)
"""
import os
import random
import tempfile
import time

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "scale.db")

from app import crm, db, retrieval  # noqa: E402  (DB_PATH must be set first)
from app.config import ASK_TOP_CHUNKS, RECENT_NOTES, SUMMARY_BATCH  # noqa: E402

NOTES = [
    "Intro call; interested in after-hours call coverage.", "Sent pricing for one location.",
    "Demo scheduled for next Tuesday.", "Asked about online booking integration.",
    "No response after the follow-up email.", "Front desk is overwhelmed during lunch hours.",
    "Wants call summaries emailed to the office manager daily.", "Concerned about setup time for staff.",
    "Asked for a customer reference.", "Happy with the trial so far; reviewing with the team.",
]


def _insert_client(conn, n: int, notes: list[str]) -> str:
    cid, contact = f"cust_{n:04d}", f"contact_{n:04d}"
    conn.execute("INSERT INTO customers VALUES (?, ?, 'prospect', '2026-01-01')", (cid, f"Practice {n:04d} Dental"))
    conn.execute("INSERT INTO contacts VALUES (?, ?, ?, ?, 'Owner')", (contact, cid, f"Owner {n}", f"o{n}@x.example"))
    for j, text in enumerate(notes):
        day = f"2026-{1 + j // 28 % 8:02d}-{1 + j % 28:02d}"
        conn.execute("INSERT INTO interactions VALUES (?, ?, ?, 'note', ?, ?)", (f"int_{n:04d}_{j:03d}", cid, contact, day, text))
    return cid


def main() -> int:
    db.init_db()  # seeds the 12 sample clients too
    rng = random.Random(7)
    planted = "Asked whether the receptionist can answer calls from Mandarin-speaking patients."
    with db.connect() as conn:
        for n in range(1000, 1300):
            notes = rng.sample(NOTES, 4)
            if n == 1217:
                notes[2] = planted
            _insert_client(conn, n, notes)
        long_notes = [rng.choice(NOTES) for _ in range(60)]
        long_notes[2] = "Office uses Eaglesoft for scheduling and billing."
        long_notes[6] = "Dr. Patel makes the final purchasing decision."
        long_id = _insert_client(conn, 2000, long_notes)
    for n in [*range(1000, 1300), 2000]:
        db.index_profile(f"cust_{n:04d}")  # profile chunks for the generated clients

    clients = db.row("SELECT COUNT(*) AS n FROM customers")["n"]
    t = time.time()
    embedded = retrieval.embed_missing()
    print(f"{clients} clients, {embedded} chunks embedded in {time.time() - t:.0f}s\n")

    ok_all = True
    for q in ("Who asked about Mandarin?", "Which practices need Chinese language support?"):
        t = time.time()
        hits = retrieval.search(q, k=ASK_TOP_CHUNKS)
        ms = (time.time() - t) * 1000
        ranks = [i for i, h in enumerate(hits) if h["customer_id"] == "cust_1217"]
        prompt = crm._ask_prompt(q, hits)
        ok_all &= bool(ranks)
        print(f"{'PASS' if ranks else 'FAIL'}  {q!r:52} planted note in the {ASK_TOP_CHUNKS} chunks Ask reads: "
              f"{'rank ' + str(ranks[0] + 1) if ranks else 'no'} | search {ms:.0f}ms | Ask prompt {len(prompt)} chars")
    print("      (the Ask prompt holds 20 chunks whether there are 12 clients or 300)\n")

    account = crm.get_account(long_id)
    t = time.time()
    history = crm.update_history_summary(account)
    shown = crm._shown_notes(account, history)
    context = crm.account_context(account, history)
    remembered = " ".join([history["summary"], *history["key_facts"]]).lower()
    checks = {
        f"folded {history['notes_covered']} older notes into the summary": history["notes_covered"] == 40,
        f"prompt shows {len(shown)} notes word for word (max {RECENT_NOTES + SUMMARY_BATCH - 1})":
            len(shown) <= RECENT_NOTES + SUMMARY_BATCH - 1,
        "summary still knows note #3: Eaglesoft": "eaglesoft" in remembered,
        "summary still knows note #7: Dr. Patel decides": "patel" in remembered,
    }
    print(f"60-note client: {SUMMARY_BATCH}-note batches summarized in {time.time() - t:.0f}s, "
          f"analysis prompt {len(context)} chars")
    for label, ok in checks.items():
        ok_all &= ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print("\nKey facts:", *[f"  - {f}" for f in history["key_facts"]], sep="\n")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
