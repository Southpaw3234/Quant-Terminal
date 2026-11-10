#!/usr/bin/env python3
"""Pure-logic validation for extract_8k_deficiency.py — no network, no secrets.

The hard parts of this extractor are not the same as spec #3's, because the
selection rule is different in kind. There is no CAR arithmetic to check and
no threshold to defend. What has to be right instead:

  1. WHICH SESSION REACTS. Unchanged from spec #3 and tested the same way,
     including the DST case: 20:30Z is intraday in January and after the
     close in July.
  2. THE ENTRY BAR IS A+1, verified by replicating the MEASUREMENT'S OWN
     searchsorted(side="right") call rather than by trusting a comment.
  3. POINT-IN-TIME MEMBERSHIP. A company that becomes universe-eligible
     AFTER its deficiency notice must not be selected on eligibility it did
     not yet have. This is the look-ahead guard and it gets a dedicated case.
  4. 🔑 THERE IS NO PRICE CONDITION ANYWHERE. Spec #3 died at t = -4.04
     because selecting on a +5% move bought mean reversion. A test asserts
     the absence of any price threshold in this module, so the failure mode
     cannot be reintroduced quietly later.

Plus the tripwire that matters before a slot is spent: specification #4 is
NOT in the registry and K is unchanged. That check is MEANT to go red the day
it is declared, and flipping it is how the declaration gets noticed.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pandas as pd

import extract_8k_deficiency as ex

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

MONTH_ENDS = pd.DatetimeIndex(["2025-12-31", "2026-01-30", "2026-06-30"])
PANEL = {
    pd.Timestamp("2025-12-31"): frozenset({"AAA", "BBB"}),
    pd.Timestamp("2026-01-30"): frozenset({"AAA", "BBB", "LATE"}),
    pd.Timestamp("2026-06-30"): frozenset({"AAA"}),
}


# ═══════════════════════════════════════════════════ 1. the reacting session

def test_announcement_session():
    print("\n--- which session can react to the filing ---")
    A = ex.announcement_session

    check("after-close-next-session",
          A("2026-01-06T21:01:47Z", SESSIONS) == pd.Timestamp("2026-01-07"),
          "21:01Z = 16:01 ET in January — after the close, so A is the next session")
    check("before-close-same-session",
          A("2026-01-06T14:30:00Z", SESSIONS) == pd.Timestamp("2026-01-06"),
          "14:30Z = 09:30 ET — intraday, so the same session reacts")
    check("dst-jan-2030z-is-intraday",
          A("2026-01-06T20:30:00Z", SESSIONS) == pd.Timestamp("2026-01-06"),
          "20:30Z = 15:30 EST — still intraday in January")
    check("dst-jul-2030z-is-after-close",
          A("2026-07-06T20:30:00Z", SESSIONS) == pd.Timestamp("2026-07-07"),
          "the SAME 20:30Z = 16:30 EDT in July — after the close. One "
          "timestamp, two answers, and only the zone decides")
    check("weekend-rolls-forward",
          A("2026-01-10T15:00:00Z", SESSIONS) == pd.Timestamp("2026-01-12"),
          "Saturday -> Monday")
    check("holiday-rolls-forward",
          A("2026-01-11T12:00:00Z", SESSIONS) == pd.Timestamp("2026-01-12"),
          "a non-session date rolls to the first session strictly after it")
    check("naive-timestamp-treated-as-utc",
          A("2026-01-06T21:01:47", SESSIONS) == pd.Timestamp("2026-01-07"),
          "a timestamp without a zone is read as UTC, not as local time")
    check("missing-acceptance-is-none",
          A(None, SESSIONS) is None,
          "no timestamp -> no event, rather than a guessed session")
    check("past-the-last-session-is-none",
          A("2027-01-01T12:00:00Z", SESSIONS) is None,
          "a filing after the price history ends is not a measurement")


# ═══════════════════════════════════════════════════════ 2. entry is A+1

def test_entry_bar():
    print("\n--- entry is A+1, verified against the measurement's own call ---")
    a = pd.Timestamp("2026-01-07")
    ets = ex.event_ts_for(a, SESSIONS)
    check("event-ts-is-a",
          ets == pd.Timestamp("2026-01-07"),
          "event_ts is the close of A itself — the selection used no price, "
          "so nothing has to be stepped over")

    # Replicate the measurement's entry rule rather than trusting the comment.
    i = SESSIONS.searchsorted(ets, side="right")
    check("entry-is-a-plus-1",
          SESSIONS[i] == pd.Timestamp("2026-01-08"),
          f"searchsorted(event_ts, side='right') -> {SESSIONS[i].date()} = A+1, "
          f"the first session strictly after the news was actionable")
    check("entry-strictly-after-A",
          SESSIONS[i] > a,
          "and strictly after A, so the announcement session itself is never "
          "traded — no part of the day the news broke is assumed available")
    check("entry-none-at-history-end",
          ex.event_ts_for(SESSIONS[-1], SESSIONS) is None,
          "an A with no following session yields no event rather than an "
          "entry that could not have happened")
    check("event-ts-none-when-A-not-a-session",
          ex.event_ts_for(pd.Timestamp("2026-01-10"), SESSIONS) is None,
          "a non-session A is refused rather than snapped to a neighbour")


# ═══════════════════════════════════ 3. point-in-time membership (look-ahead)

def test_pit_membership():
    print("\n--- PIT membership: eligibility is as of the month-end BEFORE A ---")
    E = ex.pit_eligible
    check("eligible-at-prior-month-end",
          E(PANEL, MONTH_ENDS, "AAA", pd.Timestamp("2026-01-07")),
          "AAA was in the panel at 2025-12-31, the last month-end before A")
    check("look-ahead-refused",
          not E(PANEL, MONTH_ENDS, "LATE", pd.Timestamp("2026-01-07")),
          "🔑 LATE only joins the panel at 2026-01-30, AFTER this A. Selecting "
          "it would use eligibility the company did not yet have")
    check("eligible-once-the-month-end-passes",
          E(PANEL, MONTH_ENDS, "LATE", pd.Timestamp("2026-02-02")),
          "and the same name IS eligible once its month-end is actually past")
    check("dropped-name-not-eligible-later",
          not E(PANEL, MONTH_ENDS, "BBB", pd.Timestamp("2026-07-07")),
          "BBB left the panel by 2026-06-30 and is not selected after that")
    check("before-first-month-end-is-none",
          not E(PANEL, MONTH_ENDS, "AAA", pd.Timestamp("2015-01-01")),
          "an A before the panel starts has no membership to read, so the "
          "event is refused rather than assumed in")


# ═════════════════════════════════════ 4. the selection rule is item-only

def test_selection_is_not_price_conditioned():
    print("\n--- 🔑 the selection conditions on a FILING, never on a price ---")
    check("item-301-qualifies", ex.qualifies("3.01"), "a bare 3.01 qualifies")
    check("item-301-among-others", ex.qualifies("5.02,3.01,8.01"),
          "and 3.01 alongside other items still qualifies")
    check("item-302-does-not", not ex.qualifies("3.02"),
          "3.02 is unregistered sales, a different event — it does not qualify")
    check("item-202-does-not", not ex.qualifies("2.02"),
          "and spec #3's earnings item does not leak into this sample")
    check("empty-items-does-not", not ex.qualifies("") and not ex.qualifies(None),
          "a filing with no items recorded is not an event")

    # The property that matters most, asserted structurally rather than by
    # reading the code: nothing in this module thresholds a return.
    src = inspect.getsource(ex)
    banned = ("CAR_THRESHOLD", "car_two_session", "SURPRISE_SESSIONS")
    present = [b for b in banned if b in src]
    check("no-price-threshold-in-the-module", not present,
          "no CAR, no surprise window, no return threshold anywhere. Spec #3 "
          "died at t = -4.04 because selecting on a +5% move bought mean "
          "reversion; this specification has no price in its selection rule, "
          "so that failure mode cannot return quietly"
          if not present else f"price-selection machinery reappeared: {present}")

    sig = inspect.signature(ex.qualifies)
    check("qualifies-takes-only-the-items-field",
          list(sig.parameters) == ["items_field"],
          "the selection function cannot even see a price — it takes the "
          "filing's items field and nothing else")


# ═══════════════════════════════════════════════════════ 5. assembling rows

def test_build_rows():
    print("\n--- assembling rows ---")
    f = [
        {"ticker": "AAA", "filing_date": "2026-01-06",
         "acceptance": "2026-01-06T21:30:00Z"},          # -> A = 01-07
        {"ticker": "AAA", "filing_date": "2026-01-07",
         "acceptance": "2026-01-07T12:00:00Z"},          # -> A = 01-07 too
        {"ticker": "LATE", "filing_date": "2026-01-06",
         "acceptance": "2026-01-06T21:30:00Z"},          # not yet eligible
    ]
    rows, c = ex.build_rows(f, SESSIONS, PANEL, MONTH_ENDS)
    check("dedupes-to-one-reacting-session",
          len(rows) == 1 and c["duplicate_session"] == 1,
          "two 3.01 filings that become actionable on the same session are "
          "ONE announcement, not two observations of one")
    check("look-ahead-row-excluded",
          c["not_pit_eligible"] == 1 and all(r["ticker"] != "LATE" for r in rows),
          "and the not-yet-eligible name never reaches the file")
    check("row-has-required-columns",
          all(k in rows[0] for k in
              ("event_id", "ticker", "event_ts", "event_type")),
          "event_id / ticker / event_ts / event_type all present")
    check("row-event-ts-is-A",
          rows[0]["event_ts"] == "2026-01-07" and rows[0]["day_A"] == "2026-01-07",
          "event_ts is A, so the measurement enters at A+1")
    check("event-type-is-declared",
          rows[0]["event_type"] == ex.EVENT_TYPE == "8k_item301_listing_deficiency",
          "the event_type is the declared one")
    check("counters-account-for-every-filing",
          c["filings"] == 3
          and c["kept"] + c["no_session"] + c["no_next_session"]
              + c["not_pit_eligible"] + c["duplicate_session"] == c["filings"],
          "every filing is either kept or counted in exactly one drop reason "
          "— a funnel that does not add up is a funnel hiding something")

    rows2, c2 = ex.build_rows(
        [{"ticker": "AAA", "filing_date": "2026-07-08",
          "acceptance": "2026-07-08T21:00:00Z"}], SESSIONS, PANEL, MONTH_ENDS)
    check("no-session-after-A-is-dropped",
          rows2 == [] and (c2["no_session"] + c2["no_next_session"]) == 1,
          "a filing with no tradeable session after it is dropped and counted")


# ═════════════════════════════════════════ 6. the window and the tripwire

def test_window_and_registry():
    print("\n--- the window matches the frozen panel, and K is untouched ---")
    panel = Path("data/universe/v29_universe_pit.csv")
    if panel.exists():
        d = pd.read_csv(panel, usecols=["date"])["date"]
        check("window-matches-panel",
              ex.WINDOW_START == str(d.min())[:10]
              and ex.WINDOW_END == str(d.max())[:10],
              f"extractor window {ex.WINDOW_START}..{ex.WINDOW_END} equals the "
              f"panel's own span — the sample is the frozen panel's, not a "
              f"range chosen by hand")
    else:
        check("window-matches-panel", False, f"{panel} missing")

    check("benchmark-is-borrowable",
          ex.BENCHMARK == "IWM",
          "IWM, not SPY: the hedge this specification proposes has to be one "
          "that can actually be shorted at $50k-$5M ADV")

    reg = Path("data/registry/specifications.json")
    specs = json.loads(reg.read_text(encoding="utf-8")).get("specifications", {})
    # ⚠️ TRIPWIRE. This is MEANT to go red the day spec #4 is declared. Until
    # then it asserts that building the extractor cost nothing: the design can
    # be finished, reviewed and rehearsed without committing a slot.
    check("spec4-not-declared",
          not any(s.startswith("listing_deficiency") for s in specs),
          f"no listing_deficiency specification in the registry — the "
          f"extractor exists, the slot does not. {len(specs)} specs declared, "
          f"and a declaration is what spends K, not this file")


def main():
    print("=" * 70)
    print("extract_8k_deficiency — pure logic (no network)")
    print("=" * 70)
    test_announcement_session()
    test_entry_bar()
    test_pit_membership()
    test_selection_is_not_price_conditioned()
    test_build_rows()
    test_window_and_registry()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("The reacting session is right across DST, weekends and holidays;")
    print("entry lands on A+1 under the measurement's own searchsorted;")
    print("membership is read as of the month-end BEFORE the filing, so a")
    print("company cannot be selected on eligibility it did not yet have;")
    print("and there is no price anywhere in the selection rule.")


if __name__ == "__main__":
    main()
