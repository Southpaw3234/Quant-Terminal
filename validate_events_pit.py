#!/usr/bin/env python3
"""Pure-logic validation for extract_events_pit.py — no network, no secrets.

Step 3 adds exactly one thing to a tested extractor: an eligibility filter.
So this suite attacks that filter and the boundary it turns on, and then
checks the two ways step 3 could quietly stop being safe — by drifting from
spec #3's event definition, or by writing into spec #3's frozen input.

The boundary is the whole filter. Membership must come from the last
month-end STRICTLY BEFORE the event. Allow equality and an event occurring on
a month-end would be screened using its own month.
"""
from __future__ import annotations

import sys

import pandas as pd

import extract_events_pit as p

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


GRID = ["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"]
PAIRS = {("ALIVE", "2024-01-31"), ("ALIVE", "2024-02-29"), ("ALIVE", "2024-03-31"),
         ("LATER", "2024-03-31"), ("LATER", "2024-04-30")}


def test_governing_month_end():
    print("\n--- which month-end governs an event ---")
    check("mid-month-uses-previous",
          p.governing_month_end("2024-03-15", GRID) == "2024-02-29",
          "an event on 15 March is screened by the 29 February membership")
    check("strictly-before-on-a-month-end",
          p.governing_month_end("2024-03-31", GRID) == "2024-02-29",
          "an event ON 31 March uses FEBRUARY — allowing equality would let the "
          "event's own month decide its eligibility")
    check("day-after-month-end",
          p.governing_month_end("2024-04-01", GRID) == "2024-03-31",
          "the day after a month-end picks that month-end up")
    check("before-the-grid-has-none",
          p.governing_month_end("2023-12-01", GRID) is None,
          "an event before the panel starts has no governing month-end")
    check("accepts-timestamps",
          p.governing_month_end(pd.Timestamp("2024-03-15"), GRID) == "2024-02-29",
          "a Timestamp works as well as a string")


def test_eligible_at():
    print("\n--- the eligibility filter ---")
    check("eligible-when-listed",
          p.eligible_at(PAIRS, GRID, "ALIVE", "2024-03-15"),
          "ALIVE was eligible at 29 February, so a 15 March event counts")
    check("not-eligible-before-it-qualified",
          not p.eligible_at(PAIRS, GRID, "LATER", "2024-03-15"),
          "LATER only qualifies from 31 March — a 15 March event is REFUSED. "
          "This is the look-ahead the static universe was committing")
    check("eligible-once-it-qualifies",
          p.eligible_at(PAIRS, GRID, "LATER", "2024-04-15"),
          "and it counts from April, once the qualification is real")
    check("unknown-ticker-refused",
          not p.eligible_at(PAIRS, GRID, "NEVER", "2024-03-15"),
          "a name absent from the panel is never eligible")
    check("case-insensitive",
          p.eligible_at(PAIRS, GRID, "alive", "2024-03-15"),
          "ticker case does not change membership")


def test_filter_events():
    print("\n--- filtering a batch ---")
    rows = [
        {"ticker": "ALIVE", "day_A": "2024-03-15", "event_ts": "2024-03-18",
         "event_type": "8k_item202_car_ge5", "car": 0.2},
        {"ticker": "LATER", "day_A": "2024-03-15", "event_ts": "2024-03-18",
         "event_type": "8k_item202_car_ge5", "car": 0.3},
        {"ticker": "NEVER", "day_A": "2024-03-15", "event_ts": "2024-03-18",
         "event_type": "8k_item202_car_ge5", "car": 0.4},
    ]
    kept, stats = p.filter_events(rows, PAIRS, GRID)
    check("keeps-only-eligible",
          len(kept) == 1 and kept[0]["ticker"] == "ALIVE",
          f"1 of 3 survives; {stats['dropped_not_eligible_then']} dropped as not "
          f"eligible at the time")
    check("counts-are-complete",
          stats["in"] == 3 and stats["kept"] + stats["dropped_not_eligible_then"] == 3,
          "every input event lands in exactly one bucket")
    check("event-type-relabelled",
          kept[0]["event_type"] == p.EVENT_TYPE and p.EVENT_TYPE.endswith("_pit"),
          f"surviving rows are relabelled {p.EVENT_TYPE} so a PIT event set can "
          f"never be mistaken for spec #3's in a ledger")
    check("input-not-mutated",
          rows[0]["event_type"] == "8k_item202_car_ge5",
          "the caller's rows are copied, not edited in place")


def test_comparison():
    print("\n--- the set difference against the frozen file ---")
    import tempfile, os as _os
    fz = pd.DataFrame([{"ticker": "ALIVE", "day_A": "2024-03-15"},
                       {"ticker": "GONENOW", "day_A": "2024-02-15"}])
    fd, path = tempfile.mkstemp(suffix=".csv"); _os.close(fd)
    fz.to_csv(path, index=False)
    try:
        cmp = p.compare_to_frozen(
            [{"ticker": "ALIVE", "day_A": "2024-03-15"},
             {"ticker": "DEADCO", "day_A": "2024-01-10"}], path)
        check("common-counted", cmp["common"] == 1, "one event is in both sets")
        check("only-frozen-counted", cmp["only_frozen"] == 1,
              "GONENOW is in the frozen set only — not eligible when it happened")
        check("only-pit-counted", cmp["only_pit"] == 1,
              "DEADCO is point-in-time only — an event the static universe could "
              "not have seen")
        check("missing-frozen-file-is-safe",
              p.compare_to_frozen([{"ticker": "A", "day_A": "2024-01-01"}],
                                  "does/not/exist.csv")["frozen_rows"] == 0,
              "an absent frozen file yields zeros rather than an exception")
    finally:
        _os.unlink(path)


def test_does_not_touch_spec3():
    print("\n--- step 3 must not become spec #3 ---")
    src = open("extract_events_pit.py", encoding="utf-8").read()
    check("writes-its-own-file",
          src.count(".to_csv(") == 1 and "df.to_csv(OUT_CSV" in src,
          "exactly one write, to its own output path")
    check("output-is-not-the-declared-input",
          p.OUT_CSV != "data/events/events_8k_car_1095d.csv"
          and "_pit_" in p.OUT_CSV,
          f"output is {p.OUT_CSV} — NOT spec #3's declared, frozen input")
    check("reuses-tested-logic",
          "import extract_8k_car as base" in src
          and "def car_two_session" not in src
          and "def announcement_session" not in src,
          "the reacting session, the CAR and the entry rule are IMPORTED from "
          "the tested extractor, not copied")
    check("threshold-not-redefined",
          "CAR_THRESHOLD =" not in src and "base.CAR_THRESHOLD" in src,
          "the +5.0% bar is read from the spec #3 module, so it cannot drift here")
    check("no-return-computed",
          "forward_return" not in src and "event_study" not in src,
          "no forward return and no engine import — selection only")


def main():
    print("=" * 70)
    print("extract_events_pit — pure logic (no network)")
    print("=" * 70)
    test_governing_month_end()
    test_eligible_at()
    test_filter_events()
    test_comparison()
    test_does_not_touch_spec3()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Membership comes from the month-end STRICTLY BEFORE the event; the")
    print("event definition is imported rather than copied; and the output can")
    print("never be mistaken for spec #3's frozen input.")


if __name__ == "__main__":
    main()
