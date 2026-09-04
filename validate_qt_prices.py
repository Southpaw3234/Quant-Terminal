#!/usr/bin/env python3
"""Validate the price layer. No network.

The three load-bearing tests make the back-adjustment claim CHECKABLE instead
of asserted, by simulating a 1-for-10 reverse split and measuring what each
quantity does:

    returns    -> invariant      (the factor cancels in the ratio)
    dollar ADV -> ~invariant     (price and volume scale inversely)
    price floor-> NOT invariant  ← the leak, and it has a direction

That third one is the whole point. A stock that truly traded at $0.50 shows as
$5.00 in the back-adjusted series and clears a $2 floor it should have failed —
and since reverse splits are a distress signal concentrated in small illiquid
names, the bias systematically admits exactly what the floor exists to exclude.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from qt import prices

FAILURES: list = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)


def _series(n=300, px=0.50, vol=2_000_000, start="2025-01-01"):
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(float(px), index=idx), pd.Series(float(vol), index=idx)


def test_backadjustment_semantics():
    print("\n--- back-adjustment: what it breaks and what it does not ---")
    close, vol = _series(px=0.50, vol=2_000_000)
    # A 1-for-10 REVERSE split back-adjusts history UP by 10x and volume DOWN.
    adj_close, adj_vol = close * 10.0, vol / 10.0

    i0, i1 = 100, 121
    r_raw = float(close.iloc[i1] / close.iloc[i0] - 1.0)
    r_adj = float(adj_close.iloc[i1] / adj_close.iloc[i0] - 1.0)
    check("returns-invariant", abs(r_raw - r_adj) < 1e-12,
          f"raw {r_raw:+.6f} == adjusted {r_adj:+.6f} — the factor cancels, so "
          f"A2's abnormal returns are SAFE on an adjusted series")

    asof = close.index[200]
    adv_raw, _ = prices.dollar_adv(close, vol, asof=asof)
    adv_adj, _ = prices.dollar_adv(adj_close, adj_vol, asof=asof)
    check("adv-approx-invariant", abs(adv_raw - adv_adj) / adv_raw < 1e-9,
          f"raw ${adv_raw:,.0f} ~= adjusted ${adv_adj:,.0f} — price and volume "
          f"scale inversely, so dollar ADV largely survives")

    px_raw = prices.price_as_of(close, asof)
    px_adj = prices.price_as_of(adj_close, asof)
    check("price-NOT-invariant", abs(px_raw - px_adj) > 1.0,
          f"raw ${px_raw:.2f} vs adjusted ${px_adj:.2f} — THE LEAK")

    # And the direction: the adjusted level passes a floor the truth fails.
    floor = 2.00
    e_raw = prices.screen_as_of(close, vol, asof, adv_max=5e6, adv_min=2.5e5,
                                price_min=floor, min_history=200)
    e_adj = prices.screen_as_of(adj_close, adj_vol, asof, adv_max=5e6,
                                adv_min=2.5e5, price_min=floor, min_history=200)
    check("leak-has-a-direction",
          (not e_raw.eligible and e_raw.reason == "price_too_low")
          and e_adj.eligible,
          f"truth ${px_raw:.2f} -> {e_raw.reason}; back-adjusted ${px_adj:.2f} "
          f"-> eligible. Back-adjustment ADMITS the distressed name a ${floor:.2f} "
          f"floor exists to exclude")


def test_point_in_time():
    print("\n--- as-of: strictly before, and it binds ---")
    close, vol = _series(n=300, px=10.0, vol=1_000_000)
    ev = close.index[150]
    # Liquidity explodes only AFTER the event.
    vol2 = vol.copy()
    vol2[vol2.index >= ev] = 50_000_000.0

    adv_pit, n_pit = prices.dollar_adv(close, vol2, asof=ev)
    adv_all, _ = prices.dollar_adv(close, vol2, asof=None)
    check("adv-point-in-time", abs(adv_pit - 10_000_000) < 1e-6,
          f"as-of ${adv_pit:,.0f} — post-event volume excluded")
    check("adv-differs-from-today", adv_all > 10 * adv_pit,
          f"all-history ${adv_all:,.0f} is {adv_all/adv_pit:.0f}x the as-of "
          f"value — screening on it decides eligibility with the future")
    check("asof-strictly-before", n_pit == 150,
          f"{n_pit} bars strictly before the event (the event bar itself is "
          f"excluded, matching the entry rule)")

    empty = prices.as_of(close, close.index[0])
    check("asof-before-history", len(empty) == 0,
          "as-of earlier than all bars -> empty, not a crash")
    check("asof-nan-price", not np.isfinite(prices.price_as_of(close, close.index[0])),
          "and price_as_of returns NaN rather than a stale value")


def test_screen_reasons():
    print("\n--- screen_as_of: a reason, not a bare boolean ---")
    close, vol = _series(n=300, px=10.0, vol=100_000)   # $1M/day
    asof = close.index[250]
    e = prices.screen_as_of(close, vol, asof, adv_max=5e6, adv_min=2.5e5,
                            price_min=2.0, min_history=200)
    check("screen-eligible", e.eligible and e.reason == "eligible",
          f"${e.price:.2f}, ADV ${e.adv:,.0f} -> {e.reason}")

    liquid, lv = _series(n=300, px=50.0, vol=1_000_000)   # $50M/day
    e2 = prices.screen_as_of(liquid, lv, asof, adv_max=5e6, adv_min=2.5e5,
                             price_min=2.0, min_history=200)
    check("screen-too-liquid", not e2.eligible and e2.reason == "too_liquid",
          f"ADV ${e2.adv:,.0f} -> {e2.reason} (the screen is a CEILING)")

    e3 = prices.screen_as_of(close, vol, close.index[50], adv_max=5e6,
                             adv_min=2.5e5, price_min=2.0, min_history=200)
    check("screen-short-history", not e3.eligible and e3.reason == "short_history",
          f"only {e3.n_bars} bars available as-of that date -> {e3.reason}")

    e4 = prices.screen_as_of(pd.Series(dtype=float), pd.Series(dtype=float),
                             asof, 5e6, 2.5e5, 2.0, 200)
    check("screen-no-price", not e4.eligible and e4.reason == "no_price",
          "no data -> no_price, distinguishable from 'screened out'")


def test_forward_return():
    print("\n--- forward_return: settlement ---")
    close, _ = _series(n=100, px=10.0)
    r = prices.forward_return(close, entry_i=10, horizon=21)
    check("fwd-flat", r is not None and abs(r) < 1e-12,
          "a flat series returns 0.0, not None")
    r2 = prices.forward_return(close, entry_i=len(close) - 22, horizon=21)
    check("fwd-unsettled", r2 is None,
          "exit landing on the newest bar is withheld — settlement requires a "
          "LATER bar to prove the session completed")
    r3 = prices.forward_return(close, entry_i=len(close) - 22, horizon=21,
                               settled_only=False)
    check("fwd-settled-off", r3 is not None,
          "settled_only=False allows it (tests only)")


def main():
    print("=" * 70)
    print("qt.prices — price layer validation (no network)")
    print("=" * 70)
    test_backadjustment_semantics()
    test_point_in_time()
    test_screen_reasons()
    test_forward_return()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Returns are invariant to back-adjustment; dollar ADV is ~invariant;")
    print("the PRICE FLOOR is not, and the bias admits distressed names.")


if __name__ == "__main__":
    main()
