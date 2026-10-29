"""Tiny behavioural eval: do the AI features get the cases that matter right?

Run: .venv/bin/python eval.py              priorities (uses saved analyses; add --refresh to re-run the LLM)
     .venv/bin/python eval.py --retrieval  does search find the right client for a question?
     .venv/bin/python eval.py --ask        does Ask pick the right tool and client?
     LLM_PROVIDER=rules .venv/bin/python eval.py   (baseline without AI)

Tip: point it at a fresh copy of the sample data so your own test notes don't skew it:
     DB_PATH=/tmp/eval.db .venv/bin/python eval.py --refresh
"""
import sys
import time

from app import crm, db, llm, retrieval

# (customer_id, allowed priorities, why this case matters)
EXPECTATIONS = [
    ("cust_012", {"urgent"}, "Julia asked for a follow-up *this week*"),
    ("cust_009", {"urgent", "soon"}, "High intent; onboarding deadline Sep 15 unconfirmed"),
    ("cust_001", {"urgent", "soon"}, "3-location proposal unanswered for a week"),
    ("cust_003", {"urgent", "soon"}, "Stalled, but opening 2nd location in October"),
    ("cust_011", {"waiting"}, "Explicit 'do not push before September planning meeting'"),
    ("cust_004", {"healthy"}, "Issue resolved; note says no follow-up needed"),
    ("cust_007", {"soon", "waiting"}, "Cold after pricing; one re-engagement, not urgent"),
]
TOP_N = {"cust_012", "cust_009"}  # must appear in the top 4 of the queue


def eval_priorities(refresh: bool) -> int:
    print(f"Provider: {llm.model_label()}\n")
    results = {}
    for c in crm.get_customers():
        account = crm.get_account(c["id"])
        use_llm = llm.provider_name() != "rules" and (refresh or crm.needs_analysis(account))
        results[c["id"]] = crm.analyze(account) if use_llm else crm.get_analysis(account)

    passed = 0
    for cid, allowed, why in EXPECTATIONS:
        got = results[cid]["priority"]
        ok = got in allowed
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {cid:9} got={got:10} want={'/'.join(sorted(allowed)):18} {why}")

    order = {"urgent": 0, "soon": 1, "waiting": 2, "healthy": 3}
    ranked = sorted(results, key=lambda k: (order[results[k]["priority"]], results[k]["due_date"] or "9999"))
    ok = TOP_N <= set(ranked[:4])
    passed += ok
    print(f"{'PASS' if ok else 'FAIL'}  top-4 contains {sorted(TOP_N)}: {ranked[:4]}")
    return _summary(passed, len(EXPECTATIONS) + 1)


# question -> client whose chunks must be in the top results
RETRIEVAL_CASES = {
    "Who uses Dentrix?": "cust_005",
    "Which clients want to expand to more locations?": "cust_003",
    "Who speaks Spanish?": "cust_008",
    "Anyone worried patients won't like talking to a robot?": "cust_007",  # no shared keyword
    "Who asked about texting patients?": "cust_002",
    "Who cares about contract cancellation terms?": "cust_012",
}


def eval_retrieval(k: int = 6) -> int:
    """k is deliberately small (6 chunks out of ~80) so the test means something on the sample data."""
    retrieval.embed_missing()
    print(f"Retrieval: is the right client in the top {k} chunks? (vectors: {llm.embed_model_label() or 'off'})\n")
    passed = 0
    for q, want in RETRIEVAL_CASES.items():
        top = list(dict.fromkeys(h["customer_id"] for h in retrieval.search(q, k=k)))
        ok = want in top
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {q!r:58} clients={top}")
    return _summary(passed, len(RETRIEVAL_CASES))


# question -> (expected tool, client that must be in the result, or None)
ASK_CASES = {
    "who is urgent?": ("list_by_status", "cust_012"),
    "what's on hold?": ("list_by_status", "cust_011"),
    "Who uses Dentrix?": ("find_clients", "cust_005"),
    "Who speaks Spanish?": ("find_clients", "cust_008"),
    "what are clients worried about?": ("answer", None),
    "write an email to Julia about the call": ("draft_email", "cust_012"),
}


def eval_ask() -> int:
    """Checks the tool the AI picked and the clients the tool returned. Timings show the router's speed."""
    retrieval.embed_missing()
    crm.prime_router()
    print(f"Ask: right tool and client? ({llm.model_label()})\n")
    passed = 0
    for q, (tool, client) in ASK_CASES.items():
        start = time.monotonic()
        events = crm.ask_events(q)
        first = next(events)  # the tool event: router call + tool run, before any streamed text
        took = time.monotonic() - start
        for _ in events:  # let answers and drafts finish
            pass
        ids = [c["id"] for c in first["clients"]]
        ok = first["tool"] == tool and (client is None or client in ids)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {q!r:44} tool={first['tool']:15} {took:4.1f}s  clients={ids}")
    return _summary(passed, len(ASK_CASES))


def _summary(passed: int, total: int) -> int:
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    db.init_db()
    if "--retrieval" in sys.argv:
        sys.exit(eval_retrieval())
    if "--ask" in sys.argv:
        sys.exit(eval_ask())
    sys.exit(eval_priorities(refresh="--refresh" in sys.argv))
