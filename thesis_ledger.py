#!/usr/bin/env python3
"""The thesis ledger CLI — module 2 of the collective agent.

Pre-registration applied to your own judgment. Write the falsifiers before
the outcome is known, and keep a record of what was claimed that cannot be
quietly rewritten afterwards.

    python thesis_ledger.py open --id grpn-2026q4 --ticker GRPN \\
        --claim "trading below net cash with the ad business stabilising" \\
        --falsifier "two consecutive quarters of declining active customers" \\
        --falsifier "net cash per share falls below the share price" \\
        --review 2026-02-15 --stance balanced

    python thesis_ledger.py status
    python thesis_ledger.py due
    python thesis_ledger.py review --id grpn-2026q4 --note "Q4 read" \\
        --fired "net cash per share falls below the share price"
    python thesis_ledger.py override --id grpn-2026q4 \\
        --falsifier "net cash per share falls below the share price" \\
        --reasoning "the fall is a one-off buyback, cash generation intact"
    python thesis_ledger.py close --id grpn-2026q4 --reason falsifier_fired \\
        --falsifier "two consecutive quarters of declining active customers"
    python thesis_ledger.py verify

🔑 THERE IS NO EDIT COMMAND AND NO DELETE COMMAND. Every change is an
append, and each entry carries the hash of the one before it, so altering
history breaks the chain and `verify` says exactly where.

This tool does not place orders, read prices, or evaluate anything. It
records what you decided and when you decided it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import thesis as th

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time, any
# broker SDK, and anything that reads a price. A ledger that could see a
# price could be tempted to decide something.

LEDGER = os.environ.get("QT_THESIS_LEDGER", "data/theses/ledger.jsonl")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return dt.date.today().isoformat()


def _load():
    entries = th.load(LEDGER)
    chain = th.verify(entries)
    if not chain["ok"]:
        # Fail CLOSED. A ledger whose history has been altered is not a
        # ledger, and appending to it would bless the alteration.
        print(f"[thesis] ⚠️  LEDGER CHAIN BROKEN — {chain['why']}")
        print("[thesis] refusing to append. Restore the file from git "
              "history; do not 'fix' it in place.")
        sys.exit(4)
    return entries


def _write(entry: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER) or ".", exist_ok=True)
    th.append(LEDGER, entry)
    print(f"[thesis] appended {entry['kind']} for {entry['thesis_id']}")
    print(f"         hash {entry['hash'][:16]}…  prev {entry['prev'][:16]}…")


def cmd_open(a):
    e = th.open_thesis(_load(), a.id, a.ticker, a.claim, a.falsifier,
                       a.review, _now(), a.stance or "", a.sizing or "")
    _write(e)
    print(f"         {len(e['falsifiers'])} falsifier(s), review {e['review_date']}")


def cmd_review(a):
    _write(th.review(_load(), a.id, _now(), a.note, a.fired, a.next or ""))
    if a.fired:
        print(f"         ⚠️  {len(a.fired)} falsifier(s) fired. Record an "
              f"`override` if you are keeping the position, or `close` it. "
              f"Doing neither is drift.")


def cmd_override(a):
    _write(th.override(_load(), a.id, _now(), a.falsifier, a.reasoning))
    print("         recorded as an OVERRIDE and counted in the summary.")


def cmd_close(a):
    _write(th.close(_load(), a.id, _now(), a.reason, a.note or "",
                    a.falsifier or ""))


def cmd_note(a):
    _write(th.note(_load(), a.id, _now(), a.text))


def cmd_status(a):
    entries = _load()
    st = th.state(entries)
    s = th.summary(entries, _today())
    print(f"[thesis] {LEDGER}")
    print(f"  theses {s['theses']} · open {s['open']} · closed {s['closed']}")
    print(f"  closed ON A FALSIFIER {s['closed_on_falsifier']} · "
          f"overrides {s['overrides']}")
    print(f"  reviews due {s['reviews_due']} · drifting {s['drifting']}")
    print(f"  chain {s['chain']['why']} ({s['chain']['n']} entries)\n")

    for t in sorted(st.values(), key=lambda x: (x["status"], x["review_date"])):
        mark = "·" if t["status"] == "open" else "×"
        print(f"  {mark} {t['thesis_id']:<22} {t['ticker']:<6} "
              f"{t['status']:<7} review {t['review_date'] or '—'}")
        print(f"      {t['claim'][:88]}")
        for f in t["falsifiers"]:
            hit = "🔴 FIRED" if f in t["fired"] else "      "
            print(f"      {hit}  {f[:76]}")
        if t["status"] == "open" and t["fired"] and not t["overrides"]:
            print("      ⚠️  a falsifier fired and nothing was recorded. "
                  "Override it or close it.")

    drift = th.unreviewed_fired(entries)
    if drift:
        print(f"\n[thesis] 🔑 {len(drift)} thesis/theses are DRIFTING — a "
              f"falsifier fired, the position is open, and no reasoning was "
              f"written down. That is the thing this ledger exists to catch.")


def cmd_due(a):
    entries = _load()
    d = th.due(entries, a.on or _today())
    if not d:
        print(f"[thesis] nothing due as of {a.on or _today()}")
        return
    print(f"[thesis] {len(d)} review(s) due as of {a.on or _today()}")
    for t in d:
        print(f"  {t['review_date']}  {t['thesis_id']:<22} {t['ticker']:<6} "
              f"{t['claim'][:60]}")


def cmd_verify(a):
    entries = th.load(LEDGER)
    v = th.verify(entries)
    print(f"[thesis] {LEDGER}: {v['n']} entries — {v['why']}")
    if not v["ok"]:
        print(f"[thesis] broken at entry {v['broken_at']}")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="open a thesis (needs falsifiers + review date)")
    o.add_argument("--id", required=True)
    o.add_argument("--ticker", default="")
    o.add_argument("--claim", required=True)
    o.add_argument("--falsifier", action="append", required=True,
                   help="repeatable. What would make this WRONG?")
    o.add_argument("--review", required=True, help="YYYY-MM-DD")
    o.add_argument("--stance", default="")
    o.add_argument("--sizing", default="")
    o.set_defaults(fn=cmd_open)

    r = sub.add_parser("review", help="record a review")
    r.add_argument("--id", required=True)
    r.add_argument("--note", required=True)
    r.add_argument("--fired", action="append", default=[])
    r.add_argument("--next", default="", help="next review date")
    r.set_defaults(fn=cmd_review)

    ov = sub.add_parser("override",
                        help="a falsifier fired and you are KEEPING it")
    ov.add_argument("--id", required=True)
    ov.add_argument("--falsifier", required=True)
    ov.add_argument("--reasoning", required=True)
    ov.set_defaults(fn=cmd_override)

    c = sub.add_parser("close", help="close a thesis")
    c.add_argument("--id", required=True)
    c.add_argument("--reason", required=True, choices=th.CLOSE_REASONS)
    c.add_argument("--falsifier", default="")
    c.add_argument("--note", default="")
    c.set_defaults(fn=cmd_close)

    n = sub.add_parser("note", help="append a note")
    n.add_argument("--id", required=True)
    n.add_argument("--text", required=True)
    n.set_defaults(fn=cmd_note)

    s = sub.add_parser("status", help="every thesis and its falsifiers")
    s.set_defaults(fn=cmd_status)

    d = sub.add_parser("due", help="reviews due")
    d.add_argument("--on", default="")
    d.set_defaults(fn=cmd_due)

    v = sub.add_parser("verify", help="check the hash chain")
    v.set_defaults(fn=cmd_verify)

    a = p.parse_args()
    try:
        a.fn(a)
    except ValueError as e:
        print(f"[thesis] refused: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
