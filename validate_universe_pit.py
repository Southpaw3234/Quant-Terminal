#!/usr/bin/env python3
"""Pure-logic validation for build_universe_pit.py — no network, no secrets.

The point of a point-in-time screen is that it cannot see the future. That is
one property and it is the only one worth testing hard, so most of this file
attacks it directly: a name that becomes liquid in 2025 must be INELIGIBLE at
every 2024 month-end, and a name that dies in 2024 must be ELIGIBLE before it
does. Both are constructed here from synthetic series with known answers.

The rest guards the two ways this could quietly stop being a fix:
the screen tuple silently drifting from the declared one, and the
identity-mismatch names leaking into the pool.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import build_universe_pit as b

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


def _series(start, end, price, vol):
    """Daily business-day close/volume series with constant price and volume."""
    idx = pd.bdate_range(start, end)
    return (pd.Series([float(price)] * len(idx), index=idx),
            pd.Series([float(vol)] * len(idx), index=idx))


def test_grid():
    print("\n--- the month-end grid ---")
    g = b.month_ends("2023-09-01", "2026-09-05")
    check("grid-spans-window",
          str(g[0].date()) == "2023-09-30" and str(g[-1].date()) == "2026-08-31",
          f"{len(g)} month-ends, {g[0].date()} .. {g[-1].date()}")
    check("grid-is-monthly", len(g) == 36, f"36 month-ends over three years, got {len(g)}")
    check("grid-tz-naive", g.tz is None, "naive, to compare against naive price indexes")


def test_no_lookahead():
    print("\n--- THE property: the screen cannot see forward ---")
    # A name that only becomes liquid in 2025. Volume is 0 before 2025-01-01.
    idx = pd.bdate_range("2023-01-01", "2026-06-30")
    close = pd.Series([10.0] * len(idx), index=idx)
    vol = pd.Series([0.0] * len(idx), index=idx)
    vol[vol.index >= "2025-01-01"] = 100_000.0        # $1M/day at $10
    early = b.qtp.screen_as_of(close, vol, pd.Timestamp("2024-06-30"),
                               adv_max=b.ADV_MAX, adv_min=b.ADV_MIN,
                               price_min=b.PRICE_MIN, min_history=b.MIN_HISTORY,
                               lookback=b.ADV_LOOKBACK)
    late = b.qtp.screen_as_of(close, vol, pd.Timestamp("2025-06-30"),
                              adv_max=b.ADV_MAX, adv_min=b.ADV_MIN,
                              price_min=b.PRICE_MIN, min_history=b.MIN_HISTORY,
                              lookback=b.ADV_LOOKBACK)
    check("future-liquidity-invisible",
          not early.eligible and early.reason == "too_illiquid",
          f"at 2024-06-30 the name is {early.reason} — its 2025 volume is NOT visible")
    check("becomes-eligible-when-real",
          late.eligible,
          "and it IS eligible at 2025-06-30, once the liquidity actually exists")

    # A name that dies mid-window must be eligible BEFORE it dies. This is the
    # whole survivorship point: today's screen would never see it.
    c2, v2 = _series("2023-01-01", "2024-06-28", 10.0, 100_000.0)
    before = b.qtp.screen_as_of(c2, v2, pd.Timestamp("2024-03-29"),
                                adv_max=b.ADV_MAX, adv_min=b.ADV_MIN,
                                price_min=b.PRICE_MIN, min_history=b.MIN_HISTORY,
                                lookback=b.ADV_LOOKBACK)
    after = b.qtp.screen_as_of(c2, v2, pd.Timestamp("2025-03-31"),
                               adv_max=b.ADV_MAX, adv_min=b.ADV_MIN,
                               price_min=b.PRICE_MIN, min_history=b.MIN_HISTORY,
                               lookback=b.ADV_LOOKBACK)
    check("dead-name-eligible-while-alive",
          before.eligible,
          "a company that delists in mid-2024 IS in the universe in March 2024 — "
          "exactly the name today's screen can never see")
    # 🔑 THIS CHECK CAUGHT A REAL DEFECT, and its first version hid it. It
    # originally allowed either answer — `after.eligible or after.reason in
    # (...)` — which passes whatever happens and decides nothing. The screen
    # alone DOES report this dead name as eligible in 2025, off its final 60
    # bars, forever. Liveness is what makes the answer right.
    check("screen-alone-would-zombie-it",
          after.eligible,
          "the ADV screen BY ITSELF still calls a name that stopped trading in "
          "June 2024 eligible in March 2025 — this is the defect, recorded")
    check("liveness-kills-the-zombie",
          not b.is_live(c2, pd.Timestamp("2025-03-31")),
          "is_live() refuses it: no bar within "
          f"{b.MAX_STALE_DAYS} days of the as-of date")
    check("liveness-allows-a-living-name",
          b.is_live(c2, pd.Timestamp("2024-03-29")),
          "and it does NOT refuse the same name while it was still trading")
    check("liveness-refuses-empty-history",
          not b.is_live(c2, pd.Timestamp("2022-01-01")),
          "a name with no bars before the as-of date is not live on it")


def test_screen_tuple_is_declared():
    print("\n--- the screen is the DECLARED one, not a new one ---")
    check("adv-max", b.ADV_MAX == 5_000_000.0, "ADV_MAX 5M")
    check("adv-min", b.ADV_MIN == 250_000.0, "ADV_MIN 250k")
    check("price-min", b.PRICE_MIN == 2.00, "PRICE_MIN $2.00")
    check("min-history", b.MIN_HISTORY == 200, "MIN_HISTORY 200 bars")
    check("lookback", b.ADV_LOOKBACK == 60, "ADV lookback 60 bars")
    src = open("build_universe_pit.py", encoding="utf-8").read()
    check("delegates-to-qt-prices",
          "qtp.screen_as_of" in src and "def screen_as_of" not in src,
          "the screen is DELEGATED to qt.prices, not reimplemented — two copies "
          "would drift")


def test_pool():
    print("\n--- who is in the candidate pool ---")
    uni = pd.DataFrame({"ticker": ["AAA", "BBB"]})
    roster = pd.DataFrame([
        {"ticker": "DEAD1", "cik": 111, "status": "otc-continuation"},
        {"ticker": "XFER1", "cik": 222, "status": "still-listed"},
        {"ticker": "REUSE", "cik": 333, "status": "identity-mismatch"},
        {"ticker": "GONE1", "cik": 444, "status": "no-data"},
        {"ticker": "AAA", "cik": 555, "status": "otc-continuation"},
    ])
    pool, excluded = b.candidate_pool(uni, roster)
    tks = [t for t, _, _ in pool]
    check("dead-names-added", "DEAD1" in tks, "otc-continuation names join the pool")
    check("transfers-added", "XFER1" in tks,
          "still-listed names join too — the SCREEN decides, not the roster")
    check("reused-ticker-excluded", "REUSE" not in tks,
          "identity-mismatch is EXCLUDED: splicing two companies would manufacture "
          "a return belonging to neither")
    check("unpriceable-excluded", "GONE1" not in tks,
          "no-data has no series to screen")
    check("exclusions-counted",
          excluded["identity-mismatch"] == 1 and excluded["no-data"] == 1,
          "and both exclusions are COUNTED, not silently dropped")
    check("no-duplicate-on-overlap",
          tks.count("AAA") == 1,
          "a name in both the live list and the roster appears once")
    check("live-name-source",
          dict((t, s) for t, _, s in pool)["BBB"] == "current",
          "source labels distinguish today's names from recovered ones — that "
          "label IS the survivorship measurement")


def test_evaluate():
    print("\n--- evaluation output ---")
    c, v = _series("2023-01-01", "2024-12-31", 10.0, 100_000.0)
    grid = pd.DatetimeIndex(["2023-12-31", "2024-06-30"])
    rows, funnel = b.evaluate([("DEAD1", "111", "roster")], {"DEAD1": (c, v)}, grid)
    check("emits-eligible-rows", len(rows) == 2, f"one row per eligible name-month, got {len(rows)}")
    check("row-shape",
          set(rows[0]) == {"date", "ticker", "cik", "source", "price", "adv", "n_bars"},
          "date / ticker / cik / source / price / adv / n_bars")
    check("source-preserved", rows[0]["source"] == "roster",
          "the roster label survives into the output")
    check("missing-series-counted",
          b.evaluate([("X", "", "roster")], {}, grid)[1]["no_series"] == 1,
          "a candidate with no price series is counted, not skipped in silence")
    check("funnel-totals",
          sum(funnel.values()) == 2,
          "every name-month lands in exactly one funnel bucket")


def main():
    print("=" * 70)
    print("build_universe_pit — pure logic (no network)")
    print("=" * 70)
    test_grid()
    test_no_lookahead()
    test_screen_tuple_is_declared()
    test_pool()
    test_evaluate()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Future liquidity is invisible to a past month-end; a name that dies")
    print("mid-window is eligible while it lived; the screen tuple is the")
    print("declared one; and reused tickers never enter the pool.")


if __name__ == "__main__":
    main()
