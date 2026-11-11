#!/usr/bin/env python3
"""Pure-logic validation for the thesis ledger. No network, no clock, no files.

The class of wrongness this file exists to prevent: **the record of what you
claimed quietly becoming the record of what turned out to be true.**

That is not a hypothetical. It is the single failure mode the whole
pre-registration apparatus in this repository exists to stop, and a ledger
that can be edited is strictly worse than no ledger, because it carries the
authority of a record without the properties of one.

Four things must hold, and each has a test that fails loudly:

  1. a thesis cannot be opened without falsifiers and a review date
  2. nothing can be edited — only appended
  3. an edit to history is DETECTED, and located
  4. a falsifier that fired is either overridden in writing or closed on,
     and anything else is reported as drift
"""

from __future__ import annotations

import ast
import inspect
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import thesis as th
import thesis_ledger as cli

PASS, FAIL = [], []


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def refuses(fn, *a, **k):
    """-> (did_it_refuse, message)."""
    try:
        fn(*a, **k)
        return False, "accepted"
    except ValueError as e:
        return True, str(e)


def fresh():
    e = []
    e.append(th.open_thesis(e, "t1", "grpn", "below net cash",
                            ["active customers decline two quarters",
                             "net cash per share < price"],
                            "2026-12-01", "2026-11-11", stance="balanced"))
    return e


# ── 1. a thesis must be falsifiable ──────────────────────────────────────────

def test_open_requires_falsifiers_and_review():
    print("\na thesis cannot be opened without falsifiers and a review date")

    ok, msg = refuses(th.open_thesis, [], "t", "X", "claim", [], "2026-12-01", "now")
    check("no-falsifier-refused", ok, msg[:96])
    check("refusal-explains-why", "wrong" in msg.lower() or "falsifier" in msg.lower(),
          "a thesis with no stated falsifier cannot be wrong, which means it "
          "cannot be right either")

    ok, _ = refuses(th.open_thesis, [], "t", "X", "claim", ["   ", ""],
                    "2026-12-01", "now")
    check("blank-falsifiers-refused", ok, "whitespace is not a falsifier")

    ok, msg = refuses(th.open_thesis, [], "t", "X", "claim", ["f"], "", "now")
    check("no-review-date-refused", ok, msg[:96])

    ok, _ = refuses(th.open_thesis, [], "t", "X", "", ["f"], "2026-12-01", "now")
    check("empty-claim-refused", ok)

    e = fresh()
    ok, _ = refuses(th.open_thesis, e, "t1", "X", "again", ["f"],
                    "2026-12-01", "now")
    check("duplicate-open-refused", ok,
          "a second open for the same id makes the record ambiguous")

    check("valid-open-accepted", e[0]["kind"] == "open"
          and len(e[0]["falsifiers"]) == 2)


# ── 2. append only ───────────────────────────────────────────────────────────

def test_no_mutation_exists():
    print("\nnothing can be edited — there is no code to call")
    fns = {n for n, o in vars(th).items() if callable(o)
           and getattr(o, "__module__", "") == th.__name__}
    for banned in ("update", "edit", "delete", "remove", "amend", "set_",
                   "rewrite", "fix"):
        hit = sorted(f for f in fns if banned in f.lower())
        check(f"no-{banned.strip('_')}-function", not hit,
              "a correction is a new entry that references the old, so the "
              "original claim survives beside it"
              + (f" — found {hit}" if hit else ""))

    src = inspect.getsource(th.append)
    check("append-opens-in-a-mode", '"a"' in src,
          "the only write in the module, and it cannot truncate")

    writes = [n.func.id for n in ast.walk(ast.parse(inspect.getsource(th)))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "open"]
    check("single-open-call", len(writes) <= 2,
          f"{len(writes)} open() calls — one to read, one to append")

    # Closed is closed. A second thought is a NEW thesis that references it.
    e = fresh()
    e.append(th.close(e, "t1", "now", "thesis_played_out"))
    ok, msg = refuses(th.review, e, "t1", "now", "actually...")
    check("closed-thesis-cannot-be-reopened", ok, msg[:100])


# ── 3. 🔑 tampering is detected AND located ──────────────────────────────────

def test_chain_detects_edits():
    print("\nan edit to history is detected, and located")
    e = fresh()
    e.append(th.review(e, "t1", "2026-11-20", "Q3 read"))
    e.append(th.note(e, "t1", "2026-11-21", "added to the position"))

    v = th.verify(e)
    check("intact-chain-verifies", v["ok"] and v["n"] == 3, v["why"])
    check("genesis-links-to-zero", e[0]["prev"] == th.GENESIS)
    check("each-links-to-previous",
          e[1]["prev"] == e[0]["hash"] and e[2]["prev"] == e[1]["hash"])

    # 🔑 The attack this exists to stop: quietly soften a claim that aged
    # badly, long after the outcome is known.
    tampered = [dict(x) for x in e]
    tampered[0]["claim"] = "I always said it was speculative"
    v = th.verify(tampered)
    check("edited-claim-detected", not v["ok"], v["why"][:88])
    check("edit-is-located", v["broken_at"] == 0,
          f"broken_at {v['broken_at']} — 'the ledger was edited' and 'the "
          f"ledger was edited at the entry you are about to cite' are "
          f"different facts")

    # Softening a falsifier after it fired.
    t2 = [dict(x) for x in e]
    t2[0] = dict(t2[0], falsifiers=["something that could never happen"])
    check("edited-falsifier-detected", not th.verify(t2)["ok"])

    # Deleting an inconvenient middle entry.
    check("deleted-entry-detected", not th.verify([e[0], e[2]])["ok"],
          "removing a review breaks the link to everything after it")

    # Re-sealing the edit does not help: the chain is anchored at genesis and
    # every later entry would have to be re-sealed too, which is a rewrite of
    # the whole file and visible in git.
    resealed = [th.seal({k: v for k, v in e[0].items()
                         if k not in ("prev", "hash")}, th.GENESIS)]
    resealed[0] = dict(resealed[0], claim="rewritten")
    check("reseal-without-rehash-detected", not th.verify(resealed)["ok"])

    check("empty-ledger-is-valid", th.verify([])["ok"],
          "no entries is a valid chain, not a broken one")


# ── 4. 🔑 drift ──────────────────────────────────────────────────────────────

def test_override_and_drift():
    print("\na fired falsifier is overridden in writing, closed on, or DRIFT")
    e = fresh()
    e.append(th.review(e, "t1", "2026-11-20", "Q3",
                       falsifiers_fired=["net cash per share < price"]))

    drift = th.unreviewed_fired(e)
    check("fired-and-silent-is-drift", [s["thesis_id"] for s in drift] == ["t1"],
          "a falsifier fired, the position is open, and no reasoning was "
          "written down. That is the thing this ledger exists to catch")
    check("summary-counts-drift", th.summary(e)["drifting"] == 1)

    ok, msg = refuses(th.override, e, "t1", "now", "net cash per share < price", "")
    check("override-needs-reasoning", ok, msg[:92])
    ok, _ = refuses(th.override, e, "t1", "now", "", "because")
    check("override-needs-the-falsifier-named", ok)

    e.append(th.override(e, "t1", "now", "net cash per share < price",
                         "one-off buyback; cash generation intact"))
    check("override-clears-drift", th.summary(e)["drifting"] == 0,
          "a decision was recorded — that is the ledger working, not failing")
    check("override-is-counted", th.summary(e)["overrides"] == 1,
          "🔑 reported beside open and closed, because a ledger meant to "
          "catch somebody renegotiating with themselves must make this "
          "number impossible to look away from")

    ok, _ = refuses(th.close, e, "t1", "now", "falsifier_fired")
    check("closing-on-a-falsifier-must-name-it", ok)
    ok, _ = refuses(th.close, e, "t1", "now", "because-i-felt-like-it")
    check("close-reason-is-from-a-fixed-list", ok,
          "free text would let 'it stopped working' and 'I panicked' look "
          "the same in six months")

    e.append(th.close(e, "t1", "now", "falsifier_fired",
                      falsifier="net cash per share < price"))
    s = th.summary(e)
    check("closed-on-falsifier-counted", s["closed_on_falsifier"] == 1)
    check("honoured-stop-is-visible", s["closed"] == 1 and s["open"] == 0,
          "honouring a pre-registered stop is the behaviour worth counting")


def test_due_and_state():
    print("\nreviews due, and state folded from the log")
    e = fresh()
    check("not-due-before", th.due(e, "2026-11-30") == [])
    check("due-on-the-date", len(th.due(e, "2026-12-01")) == 1)
    check("still-due-after", len(th.due(e, "2027-03-01")) == 1,
          "an overdue review does not quietly stop being due")

    e.append(th.review(e, "t1", "now", "rolled", next_review="2027-01-15"))
    check("next-review-moves-the-date",
          th.state(e)["t1"]["review_date"] == "2027-01-15")
    check("rolled-review-not-due", th.due(e, "2026-12-02") == [])

    e.append(th.close(e, "t1", "now", "position_exited"))
    check("closed-is-never-due", th.due(e, "2027-06-01") == [])

    ok, _ = refuses(th.review, [], "nope", "now", "x")
    check("unknown-thesis-refused", ok)


def test_cli_cannot_decide():
    print("\nthe CLI records; it does not decide")
    tree = ast.parse(inspect.getsource(cli))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported.add(a.name)
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module)
    for m in ("qt.referee", "qt.wrc", "qt.calendar_time", "qt.prices",
              "yfinance", "alpaca", "requests"):
        check(f"cli-does-not-import-{m}", m not in imported,
              "a ledger that could see a price could be tempted to decide "
              "something")
    check("cli-fails-closed-on-broken-chain",
          "sys.exit(4)" in inspect.getsource(cli._load),
          "appending to an altered ledger would bless the alteration")


def main():
    print("=" * 70)
    print("thesis ledger — pure logic (no network, no clock, no files)")
    print("=" * 70)
    test_open_requires_falsifiers_and_review()
    test_no_mutation_exists()
    test_chain_detects_edits()
    test_override_and_drift()
    test_due_and_state()
    test_cli_cannot_decide()

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing decided.")


if __name__ == "__main__":
    main()
