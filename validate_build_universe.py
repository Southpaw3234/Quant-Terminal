#!/usr/bin/env python3
"""Validate the inverted universe screen's pure logic. No network.

The load-bearing test is `test_adv_point_in_time`. A universe screen that
uses bars from after the event decides MEMBERSHIP with the future, which is
a look-ahead that never touches the entry bar and so is invisible to every
guard in `event_study.py`. It is the same class of error, entering through
a different door.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import build_universe as bu

FAILURES: list = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)


def test_filter_symbols():
    print("\n--- filter_symbols: operating companies only ---")
    raw = [
        {"symbol": "ACME", "type": "Common Stock", "description": "Acme"},
        {"symbol": "SPY", "type": "ETP", "description": "SPDR"},
        {"symbol": "BRK.B", "type": "Common Stock", "description": "class"},
        {"symbol": "ACME", "type": "Common Stock", "description": "dupe"},
        {"symbol": "TOOLONG", "type": "Common Stock", "description": "7 chars"},
        {"symbol": "AB1", "type": "Common Stock", "description": "digit"},
        {"symbol": "", "type": "Common Stock", "description": "blank"},
        "not a dict",
        {"symbol": "GOOD", "type": "Common Stock", "description": "keep"},
    ]
    got = [r["ticker"] for r in bu.filter_symbols(raw)]
    check("filter-symbols", got == ["ACME", "GOOD"],
          f"kept {got} (ETF, class share, dupe, 7-char, digit, blank, "
          f"non-dict all dropped)")


def test_adv_median_not_mean():
    print("\n--- compute_adv: median resists a single untradeable spike ---")
    idx = pd.bdate_range("2025-01-01", periods=100)
    close = pd.Series(10.0, index=idx)
    vol = pd.Series(10_000.0, index=idx)
    vol.iloc[-5] = 50_000_000.0            # one halt-resume / rebalance day
    adv, n = bu.compute_adv(close, vol, asof=None, lookback=60)
    mean_adv = float((close * vol).tail(60).mean())
    check("adv-median", abs(adv - 100_000.0) < 1e-6,
          f"median ADV=${adv:,.0f} (steady state $100,000)")
    check("adv-not-mean", mean_adv > 5 * adv,
          f"mean would be ${mean_adv:,.0f} — {mean_adv / adv:.0f}x higher, "
          f"and would admit this name on one untradeable day")


def test_adv_point_in_time():
    print("\n--- compute_adv: POINT-IN-TIME (the load-bearing one) ---")
    idx = pd.bdate_range("2025-01-01", periods=200)
    close = pd.Series(10.0, index=idx)
    vol = pd.Series(10_000.0, index=idx)
    # Volume explodes only AFTER the event. A screen that sees it would let
    # the future decide whether this name was ever eligible.
    ev = idx[100]
    vol[idx > ev] = 10_000_000.0

    before, n_before = bu.compute_adv(close, vol, asof=ev, lookback=60)
    allhist, _ = bu.compute_adv(close, vol, asof=None, lookback=60)
    check("adv-pit-value", abs(before - 100_000.0) < 1e-6,
          f"as-of {ev.date()} ADV=${before:,.0f} — post-event volume excluded")
    check("adv-pit-differs", allhist > 10 * before,
          f"all-history ADV=${allhist:,.0f} vs point-in-time ${before:,.0f} "
          f"({allhist / before:.0f}x) — using it would screen on the future")
    check("adv-pit-strict", n_before == 100,
          f"{n_before} bars strictly before the event (expect 100, "
          f"i.e. the event bar itself excluded)")


def test_screen_inverted():
    print("\n--- screen: it is a CEILING, not a floor ---")
    def row(tk, adv, px=10.0, n=250):
        return {"ticker": tk, "adv": adv, "last_price": px, "n_bars": n}
    rows = [
        row("MEGA", 500_000_000),      # what v25 kept -> must be dropped
        row("BIG", 50_000_000),        # exactly v25's old floor -> dropped
        row("SMALL", 1_000_000),       # keep
        row("TINY", 300_000),          # keep
        row("DUST", 10_000),           # below tradeable floor -> dropped
        row("PENNY", 1_000_000, px=0.50),   # sub-$2 -> dropped
        row("NEW", 1_000_000, n=50),        # too little history -> dropped
        {"ticker": "NOPX", "adv": None, "last_price": None, "n_bars": 0},
    ]
    kept, funnel = bu.screen(rows, adv_max=5_000_000, adv_min=250_000,
                             price_min=2.00, min_history=200)
    names = sorted(r["ticker"] for r in kept)
    check("screen-inverted", names == ["SMALL", "TINY"],
          f"kept {names} — the liquid names v25 traded are exactly what "
          f"this drops")
    check("screen-funnel", funnel["too_liquid"] == 2
          and funnel["too_illiquid"] == 1
          and funnel["price_too_low"] == 1
          and funnel["short_history"] == 1
          and funnel["no_price"] == 1,
          f"funnel accounts for every dropped name: {funnel}")
    check("screen-funnel-sums",
          funnel["kept"] + funnel["too_liquid"] + funnel["too_illiquid"]
          + funnel["price_too_low"] + funnel["short_history"]
          + funnel["no_price"] == funnel["input"],
          "funnel stages sum to the input count — no name vanishes silently")


def test_adv_empty():
    print("\n--- compute_adv: degenerate inputs ---")
    idx = pd.bdate_range("2025-01-01", periods=10)
    adv, n = bu.compute_adv(pd.Series(dtype=float), pd.Series(dtype=float))
    check("adv-empty", adv is None and n == 0, "empty series -> (None, 0)")
    adv2, n2 = bu.compute_adv(pd.Series(10.0, index=idx),
                              pd.Series(100.0, index=idx),
                              asof=idx[0])
    check("adv-asof-before-history", adv2 is None,
          "asof earlier than all bars -> None, not a crash")


def main():
    print("=" * 68)
    print("build_universe.py — pure-logic validation (no network)")
    print("=" * 68)
    test_filter_symbols()
    test_adv_median_not_mean()
    test_adv_point_in_time()
    test_screen_inverted()
    test_adv_empty()
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
