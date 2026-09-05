#!/usr/bin/env python3
"""Pure-logic validation for extract_8k_car.py — no network, no secrets.

The extractor's difficulty is entirely in three places, and each is tested
here against a hand-computed answer rather than against itself:

  1. WHICH SESSION REACTS. A UTC timestamp, a DST boundary, a weekend and a
     holiday all change the answer. The DST case is the one worth staring
     at: 20:30Z is intraday in January and after the close in July.
  2. THE CAR ARITHMETIC, on a series whose answer can be done in the head.
  3. THE ENTRY BAR. event_ts must be A+1 such that the ENGINE'S OWN
     searchsorted(side="right") lands on A+2 -- the rule is verified by
     replicating the engine's call, not by trusting the comment above it.

Plus the one that matters most for the pre-registration: the +5.0% constant
still matches the threshold declared in the registry on 2026-09-05. If
someone tunes it toward a nicer N, this suite fails.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

import extract_8k_car as ex

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


SESSIONS = pd.DatetimeIndex([
    "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
    "2026-01-09", "2026-01-12", "2026-01-13",
    "2026-07-06", "2026-07-07", "2026-07-08",
])


def test_announcement_session():
    print("\n--- which session can react to the news ---")
    A = ex.announcement_session

    check("after-close-next-session",
          A("2026-01-06T21:01:47Z", SESSIONS) == pd.Timestamp("2026-01-07"),
          "16:01 ET Tuesday -> Wednesday. 66.5% of real announcements are this case")
    check("pre-market-same-session",
          A("2026-01-06T12:00:00Z", SESSIONS) == pd.Timestamp("2026-01-06"),
          "07:00 ET -> that same session, which opens after the news")
    check("intraday-same-session",
          A("2026-01-06T18:30:00Z", SESSIONS) == pd.Timestamp("2026-01-06"),
          "13:30 ET -> the session already under way")
    check("close-boundary-is-exclusive",
          A("2026-01-06T21:00:00Z", SESSIONS) == pd.Timestamp("2026-01-07")
          and A("2026-01-06T20:59:00Z", SESSIONS) == pd.Timestamp("2026-01-06"),
          "exactly 16:00 ET is NOT before the close; 15:59 is")

    jan = A("2026-01-06T20:30:00Z", SESSIONS)
    jul = A("2026-07-06T20:30:00Z", SESSIONS)
    check("dst-flips-the-answer",
          jan == pd.Timestamp("2026-01-06") and jul == pd.Timestamp("2026-07-07"),
          f"THE SAME UTC CLOCK TIME: 20:30Z is 15:30 EST (intraday -> {jan.date()}) "
          f"in January and 16:30 EDT (after close -> {jul.date()}) in July")

    check("weekend-rolls-forward",
          A("2026-01-10T15:00:00Z", SESSIONS) == pd.Timestamp("2026-01-12"),
          "Saturday -> Monday")
    check("holiday-rolls-forward",
          A("2026-01-01T14:00:00Z", SESSIONS) == pd.Timestamp("2026-01-02"),
          "a non-session date rolls to the first session strictly after it")
    check("naive-timestamp-treated-as-utc",
          A("2026-01-06T21:01:47", SESSIONS) == pd.Timestamp("2026-01-07"),
          "a timestamp without a zone is read as UTC, not as local time")
    check("missing-acceptance-is-none",
          A(None, SESSIONS) is None and A(float("nan"), SESSIONS) is None,
          "no timestamp -> no event, rather than a guessed session")
    check("past-the-last-session-is-none",
          A("2026-12-31T21:00:00Z", SESSIONS) is None,
          "an announcement after the price history ends is not a measurement")


def test_car_arithmetic():
    print("\n--- the two-session market-adjusted return ---")
    idx = SESSIONS[:6]                       # 01-02 .. 01-09
    stock = pd.Series([100.0, 100.0, 110.0, 121.0, 121.0, 121.0], index=idx)
    bench = pd.Series([100.0, 100.0, 102.0, 105.0, 105.0, 105.0], index=idx)
    A = pd.Timestamp("2026-01-06")
    car = ex.car_two_session(stock, bench, A)
    check("car-hand-computed",
          car is not None and abs(car - 0.16) < 1e-12,
          f"stock 100->121 over [A-1, A+1] is +21%, SPY 100->105 is +5%, "
          f"CAR = +16%; got {car:+.6f}")
    check("car-uses-close-before-the-news",
          ex.car_two_session(stock, bench, pd.Timestamp("2026-01-07")) is not None,
          "A-1 is the last close BEFORE the news under all three arrival patterns")
    check("car-none-at-history-start",
          ex.car_two_session(stock, bench, idx[0]) is None,
          "no A-1 available -> None, not a truncated window")
    check("car-none-at-history-end",
          ex.car_two_session(stock, bench, idx[-1]) is None,
          "no A+1 available -> None")
    check("car-none-when-unlisted-that-day",
          ex.car_two_session(stock.drop(A), bench, A) is None,
          "a name with no bar on A yields None rather than a shifted window")
    neg = pd.Series([100.0, 100.0, 90.0, 80.0, 80.0, 80.0], index=idx)
    check("car-signs-negative",
          ex.car_two_session(neg, bench, A) < 0,
          "a bad announcement produces a negative CAR and will not qualify")


def test_threshold():
    print("\n--- the declared +5.0% bar ---")
    check("threshold-inclusive", ex.qualifies(0.05) and not ex.qualifies(0.0499),
          ">= +5.0% qualifies; +4.99% does not")
    check("threshold-rejects-nan-and-none",
          not ex.qualifies(None) and not ex.qualifies(float("nan")),
          "a missing CAR never qualifies")

    reg = json.loads(Path("data/registry/specifications.json").read_text())
    spec = reg["specifications"]["pead_8k_car_v3"]
    declared = str(spec["params"]["selection_threshold"])
    check("threshold-matches-registry",
          ex.CAR_THRESHOLD == 0.05 and "+5.0%" in declared,
          "the constant in the extractor still equals the threshold DECLARED "
          "2026-09-05 — tuning it toward a nicer N fails here")
    check("registry-still-unread",
          spec["read_at"] == "",
          "and spec #3 is still unread: this extractor computes SELECTION only")
    check("window-matches-registry",
          ex.WINDOW_START == "2023-09-01" and ex.WINDOW_END == "2026-09-05"
          and "1095" in str(spec["params"]["lookback_days"]),
          "the event window matches the declared 1,095-day lookback")
    check("output-path-matches-registry",
          "events_8k_car_1095d.csv" in str(spec["params"]["events_file"]),
          "and the output filename is the one the registry names, which the "
          "read job's INPUT GUARD will check")


def test_entry_bar():
    print("\n--- entry is A+2, verified against the ENGINE's own call ---")
    A = pd.Timestamp("2026-01-06")
    ts = ex.entry_ts(A, SESSIONS)
    check("event-ts-is-a-plus-1", ts == pd.Timestamp("2026-01-07"),
          "event_ts is the close of A+1")
    # replicate event_study.py::_entry_index rather than trusting a comment
    i = int(SESSIONS.searchsorted(pd.Timestamp(ts), side="right"))
    check("engine-enters-at-a-plus-2",
          i < len(SESSIONS) and SESSIONS[i] == pd.Timestamp("2026-01-08"),
          f"searchsorted(event_ts, side='right') -> {SESSIONS[i].date()} = A+2, "
          f"strictly after BOTH sessions the selection used")
    check("entry-none-at-history-end",
          ex.entry_ts(SESSIONS[-1], SESSIONS) is None,
          "an A with no following session yields no event")
    src = Path("event_study.py").read_text()
    check("engine-contract-unchanged",
          'side="right"' in src and '"event_id", "ticker", "event_ts", "event_type"' in src,
          "event_study.py still enters with side='right' and still requires "
          "exactly the four columns this extractor writes")


def test_build_rows():
    print("\n--- assembling rows ---")
    idx = SESSIONS[:8]
    up = pd.Series([100, 100, 100, 100, 100, 100, 100, 130.0], index=idx)
    flat = pd.Series([100.0] * 8, index=idx)
    prices = {"AAA": up, "SPY": flat}
    # Friday after close and a Saturday amendment both react on Monday 01-12
    anns = [
        {"ticker": "AAA", "filing_date": "2026-01-09", "acceptance": "2026-01-09T22:00:00Z"},
        {"ticker": "AAA", "filing_date": "2026-01-10", "acceptance": "2026-01-10T15:00:00Z"},
    ]
    rows, stats, cars = ex.build_rows(anns, prices, idx)
    check("dedupes-to-one-reacting-session",
          stats["dup_same_session"] == 1 and len(cars) == 1,
          "two filings that react on the same session are ONE announcement")
    check("qualifying-row-written",
          len(rows) == 1 and rows[0]["ticker"] == "AAA",
          "a +30% two-session move against a flat SPY clears +5.0%")
    check("row-has-engine-required-columns",
          set(rows[0]) >= {"event_id", "ticker", "event_ts", "event_type"},
          "event_id / ticker / event_ts / event_type all present")
    check("row-event-ts-is-a-plus-1",
          rows[0]["day_A"] == "2026-01-12" and rows[0]["event_ts"] == "2026-01-13",
          f"A={rows[0]['day_A']}, event_ts={rows[0]['event_ts']} = A+1")

    down = pd.Series([100.0] * 8, index=idx)
    rows2, stats2, _ = ex.build_rows(
        [{"ticker": "BBB", "filing_date": "2026-01-09", "acceptance": "2026-01-09T22:00:00Z"}],
        {"BBB": down, "SPY": flat}, idx)
    check("flat-move-does-not-qualify",
          rows2 == [] and stats2["below_threshold"] == 1,
          "a name that did not move produces no event")
    rows3, stats3, _ = ex.build_rows(
        [{"ticker": "ZZZ", "filing_date": "2026-01-09", "acceptance": "2026-01-09T22:00:00Z"}],
        {"SPY": flat}, idx)
    check("unpriced-name-skipped",
          rows3 == [] and stats3["no_prices"] == 1,
          "a name with no price history is counted and skipped, not crashed on")


def main():
    print("=" * 70)
    print("extract_8k_car — pure logic (no network)")
    print("=" * 70)
    test_announcement_session()
    test_car_arithmetic()
    test_threshold()
    test_entry_bar()
    test_build_rows()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("The reacting session is right across DST, weekends and holidays;")
    print("CAR matches a hand computation; entry lands on A+2 under the")
    print("engine's own searchsorted; the +5.0% bar still matches the registry.")


if __name__ == "__main__":
    main()
