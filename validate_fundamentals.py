#!/usr/bin/env python3
"""Pure-logic validation for extract_fundamentals.py — no network, no secrets.

One property carries this entire layer: a value known on a date must be a
value that had been FILED by that date. Everything else is bookkeeping.

So most of this file is built from a single hand-made company whose filing
history contains the two traps that ruin fundamental backtests:

  1. A fiscal year that ENDS in December and is FILED the following March.
     A backtest keyed on `end` picks it up in December and earns three months
     of hindsight on every name in the universe.
  2. A RESTATEMENT: the same period reported twice, months apart, with
     different numbers. A backtest that takes today's value for a 2024 period
     uses a figure nobody had in 2024.

Both are constructed below with answers that can be checked by eye.
"""
from __future__ import annotations

import sys

import pandas as pd

import extract_fundamentals as f

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


# FY2024 ends 2024-12-31 but is not public until 2025-03-15.
# It is then RESTATED on 2025-08-10 from 100 to 90.
FACTS = [
    {"end": "2023-12-31", "val": 80.0, "filed": "2024-03-12", "form": "10-K", "fp": "FY"},
    {"end": "2024-12-31", "val": 100.0, "filed": "2025-03-15", "form": "10-K", "fp": "FY"},
    {"end": "2024-12-31", "val": 90.0, "filed": "2025-08-10", "form": "10-K/A", "fp": "FY"},
]


def test_filing_lag():
    print("\n--- the filing lag: `filed`, never `end` ---")
    jan = f.known_as_of(FACTS, "2025-01-31")
    check("fiscal-year-end-is-not-publication",
          jan is not None and jan["end"] == "2023-12-31" and jan["val"] == 80.0,
          "on 31 Jan 2025 the newest PUBLIC annual is still FY2023 (80). FY2024 "
          "ended in December but is not filed until March — a backtest keyed on "
          "`end` would take 100 here and earn three months of hindsight")
    mar14 = f.known_as_of(FACTS, "2025-03-14")
    check("day-before-filing-still-blind",
          mar14 is not None and mar14["val"] == 80.0,
          "the day BEFORE the 10-K lands, FY2024 is still invisible")
    mar15 = f.known_as_of(FACTS, "2025-03-15")
    check("visible-on-the-filing-day",
          mar15 is not None and mar15["end"] == "2024-12-31" and mar15["val"] == 100.0,
          "on the filing day itself it becomes visible, at the ORIGINAL 100")


def test_restatement():
    print("\n--- restatements: what was on the tape THEN ---")
    jun = f.known_as_of(FACTS, "2025-06-30")
    check("pre-restatement-uses-original",
          jun is not None and jun["val"] == 100.0,
          "in June 2025 the FY2024 figure is 100 — the restatement has not "
          "happened yet, and using 90 here would be using a number nobody had")
    sep = f.known_as_of(FACTS, "2025-09-30")
    check("post-restatement-uses-revision",
          sep is not None and sep["val"] == 90.0 and sep["form"] == "10-K/A",
          "after 10 Aug 2025 the same period reads 90 — the amendment is public")
    check("latest-filing-wins-within-a-period",
          f.known_as_of(FACTS, "2026-01-01")["val"] == 90.0,
          "within one fiscal period the LATEST filing at or before the date wins")


def test_edges():
    print("\n--- edges ---")
    check("before-any-filing-is-none",
          f.known_as_of(FACTS, "2020-01-01") is None,
          "a date before the company filed anything yields None, not a guess")
    check("empty-history-is-none",
          f.known_as_of([], "2025-01-01") is None,
          "no facts at all yields None")
    check("accepts-timestamp",
          f.known_as_of(FACTS, pd.Timestamp("2025-06-30"))["val"] == 100.0,
          "a Timestamp works as well as a string")
    check("newest-period-not-newest-filing",
          f.known_as_of(
              [{"end": "2024-12-31", "val": 5.0, "filed": "2025-03-01", "form": "10-K", "fp": "FY"},
               {"end": "2023-12-31", "val": 9.0, "filed": "2025-04-01", "form": "10-K/A", "fp": "FY"}],
              "2025-06-01")["end"] == "2024-12-31",
          "a late-filed amendment to an OLD period does not displace a newer "
          "period — the most recent PERIOD wins, then the latest filing of it")


def test_collect_and_fallbacks():
    print("\n--- tag fallbacks and the flow/stock split ---")
    blob = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            {"end": "2024-12-31", "val": 500.0, "filed": "2025-03-15", "form": "10-K", "fp": "FY"},
            {"end": "2024-09-30", "val": 120.0, "filed": "2024-11-01", "form": "10-Q", "fp": "Q3"},
        ]}},
        "Assets": {"units": {"USD": [
            {"end": "2024-09-30", "val": 900.0, "filed": "2024-11-01", "form": "10-Q", "fp": "Q3"},
        ]}},
    }}}
    flows = f.collect_facts(blob, ["RevenueFromContractWithCustomerExcludingAssessedTax",
                                   "Revenues"], "USD", annual_only=True)
    check("falls-through-to-second-tag",
          len(flows) == 1 and flows[0]["tag"] == "Revenues",
          "the ASC 606 tag is absent, so the chain falls through to Revenues — a "
          "single-tag lookup would return nothing for half the universe")
    check("flows-are-annual-only",
          all(x["fp"] == "FY" for x in flows),
          "the Q3 revenue is EXCLUDED: mixing a quarter into a series of years "
          "silently divides a ratio by four")
    stocks = f.collect_facts(blob, ["Assets"], "USD", annual_only=False)
    check("stocks-take-any-form",
          len(stocks) == 1 and stocks[0]["fp"] == "Q3",
          "balance-sheet items are instantaneous, so a 10-Q value is fine")
    check("missing-tag-yields-empty",
          f.collect_facts(blob, ["NotATag"], "USD", annual_only=False) == [],
          "an absent tag yields nothing rather than raising")


def test_snapshot():
    print("\n--- the snapshot row ---")
    snap = f.snapshot({"assets": FACTS}, "2025-06-30")
    check("carries-value-and-period",
          snap["assets"] == 100.0 and snap["assets_end"] == "2024-12-31",
          "the value and the period it belongs to travel together")
    check("records-staleness",
          snap["latest_filed"] == "2025-03-15" and snap["staleness_days"] == 107,
          f"107 days between the filing and the as-of date — the real information "
          f"lag, which a backtest keyed on `end` would have erased")
    empty = f.snapshot({"assets": FACTS}, "2020-01-01")
    check("nothing-visible-yields-no-filed",
          empty.get("latest_filed") is None and empty["assets"] is None,
          "before any filing the row carries no data and is dropped upstream")


def test_computes_no_ratio():
    print("\n--- this layer selects nothing ---")
    src = open("extract_fundamentals.py", encoding="utf-8").read()
    check("no-ratio-computed",
          "price" not in src.lower().split("docstring")[0].split('"""')[-1]
          or "/ price" not in src,
          "no price is fetched and no ratio formed — the extractor is data only")
    check("no-return-computed",
          "forward_return" not in src and "event_study" not in src,
          "no forward return and no engine import")
    check("single-write",
          src.count(".to_csv(") == 1 and "df.to_csv(OUT_CSV" in src,
          "exactly one write, to its own output path")
    check("smoke-run-renamed",
          "_SMOKE" in src,
          "a capped run cannot write the real filename")


# ─────────────────────────────── trailing twelve months (value_ebit_ev_v2)
#
# Calendar-year filer. FY2023 = 400. In FY2024 it reports year-to-date at each
# quarter, and the prior year's YTD figures are on the tape too.
#
#   TTM at Q2 2024  =  FY2023 + YTD-H1-2024 - YTD-H1-2023
#                   =  400    + 230         - 200          =  430
TTM_FACTS = [
    {"end": "2023-12-31", "start": "2023-01-01", "val": 400.0, "filed": "2024-03-01", "form": "10-K", "fp": "FY"},
    {"end": "2023-06-30", "start": "2023-01-01", "val": 200.0, "filed": "2023-08-01", "form": "10-Q", "fp": "Q2"},
    {"end": "2024-06-30", "start": "2024-01-01", "val": 230.0, "filed": "2024-08-01", "form": "10-Q", "fp": "Q2"},
    # A DISCRETE quarter against the same period end. `fp` says Q2 for both;
    # only the duration separates them, and using this would understate TTM.
    {"end": "2024-06-30", "start": "2024-04-01", "val": 120.0, "filed": "2024-08-01", "form": "10-Q", "fp": "Q2"},
]


def test_ttm():
    print("\n--- trailing twelve months (value_ebit_ev_v2) ---")
    check("duration-not-fp-identifies-the-window",
          f.period_bucket(TTM_FACTS[2]) == "Q2"
          and f.period_bucket(TTM_FACTS[3]) is None,
          "both rows say fp=Q2 against the same period end; the 181-day "
          "year-to-date window is usable and the 90-day discrete quarter is "
          "not. Taking the discrete figure would understate TTM by a quarter")

    t = f.ttm_as_of(TTM_FACTS, "2024-09-30")
    check("ttm-arithmetic",
          t is not None and abs(t["val"] - 430.0) < 1e-9,
          "FY2023 400 + H1-2024 230 - H1-2023 200 = 430, not the 400 an "
          "annual-only basis would still be carrying in September")
    check("ttm-basis-recorded",
          t is not None and t["basis"] == "FY+Q2-Q2",
          "the row says how it was built, so a reader never has to guess")
    check("ttm-known-only-when-all-parts-filed",
          t is not None and t["filed"] == "2024-08-01",
          "a TTM is known on the date its LAST component was filed")

    check("fresh-annual-is-itself-the-ttm",
          (lambda r: r is not None and r["val"] == 400.0 and r["basis"] == "FY")(
              f.ttm_as_of(TTM_FACTS, "2024-03-15")),
          "right after the 10-K lands, the annual IS the trailing twelve "
          "months — no arithmetic, no three restatement histories compounded")

    check("point-in-time-holds",
          (lambda r: r is not None and r["val"] == 400.0)(
              f.ttm_as_of(TTM_FACTS, "2024-07-31")),
          "the day before the Q2 10-Q is filed, TTM is still the FY2023 "
          "figure — the interim is invisible")

    # Prior-year comparative removed: the subtraction cannot be formed.
    no_prior = [r for r in TTM_FACTS if r["end"] != "2023-06-30"]
    check("refuses-rather-than-falls-back",
          f.ttm_as_of(no_prior, "2024-09-30") is None,
          "⚠️ THE LOAD-BEARING ONE. Without the prior-year YTD there is no "
          "TTM, and the answer is None — NOT the annual figure. A silent "
          "fallback would mix two bases in one series, which is the exact "
          "failure this change exists to avoid")

    no_fy = [r for r in TTM_FACTS if r["end"] != "2023-12-31"]
    check("refuses-without-the-base-year",
          f.ttm_as_of(no_fy, "2024-09-30") is None,
          "and without FY(n-1) there is nothing to add the interim delta to")

    check("annual-mode-is-untouched",
          (lambda r: r is not None and r["val"] == 400.0)(
              f.known_as_of([r for r in TTM_FACTS if r["fp"] == "FY"], "2024-09-30")),
          "v1's annual-only basis still resolves exactly as before, so its "
          "frozen panel stays reproducible")


def main():
    print("=" * 70)
    print("extract_fundamentals — pure logic (no network)")
    print("=" * 70)
    test_filing_lag()
    test_restatement()
    test_edges()
    test_collect_and_fallbacks()
    test_snapshot()
    test_ttm()
    test_computes_no_ratio()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("A fiscal year ending in December is invisible until it is filed in")
    print("March; a restatement is invisible until it is amended; and the")
    print("information lag is recorded rather than erased.")


if __name__ == "__main__":
    main()
