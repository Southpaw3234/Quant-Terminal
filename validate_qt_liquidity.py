#!/usr/bin/env python3
"""Validation for qt.liquidity — the Corwin-Schultz spread estimator.

The estimator has to do one thing: recover a spread that is actually there,
and report roughly zero when one is not. Both directions are constructed here
from synthetic bars where the true spread is known, because on real data a
wrong spread estimate looks exactly like a right one.

This suite exists because the FIRST attempt at measuring spreads did not have
one. probe_spreads.py asked the broker for live quotes on a Sunday, got 3,336
basis points, reported the same figure for every liquidity bucket across a
hundredfold range of ADV, and nothing caught it. A number with no test behind
it is a number nobody has checked.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from qt import liquidity as lq

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


def synth(n=600, vol=0.02, spread=0.0, seed=11):
    """Daily H/L bars from a random walk, with a known spread applied.

    The high is lifted half a spread and the low dropped half a spread, which
    is the mechanism the estimator is built to invert: a trade at the high is
    usually a buy at the ask, one at the low a sell at the bid.
    """
    rng = np.random.default_rng(seed)
    mid = 100.0 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    intraday = np.abs(rng.normal(0, vol, n))
    hi = mid * (1 + intraday) * (1 + spread / 2)
    lo = mid * (1 - intraday) * (1 - spread / 2)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(hi, index=idx), pd.Series(lo, index=idx)


def test_recovers_a_known_spread():
    print("\n--- does it recover a spread that is really there? ---")
    for true_bps in (50, 200, 500):
        h, l = synth(spread=true_bps / 10_000.0, seed=int(true_bps))
        est = lq.spread_estimate(h, l)["spread_bps"]
        ok = 0.35 * true_bps <= est <= 2.2 * true_bps
        check(f"recovers-{true_bps}bps", ok,
              f"true {true_bps} bps -> estimated {est:.0f} bps "
              f"({est/true_bps:.2f}x). The estimator is noisy by construction; "
              f"what matters is the ORDER OF MAGNITUDE, which is what a cost "
              f"assumption is checked against")


def test_monotone():
    print("\n--- does a wider spread estimate wider? ---")
    ests = []
    for bps in (0, 100, 400, 900):
        h, l = synth(spread=bps / 10_000.0, seed=5)
        ests.append(lq.spread_estimate(h, l)["spread_bps"])
    check("monotone-in-the-true-spread",
          all(ests[i] < ests[i + 1] for i in range(len(ests) - 1)),
          f"estimates rise with the truth: {[f'{e:.0f}' for e in ests]} bps. "
          f"THIS is what the live-quote probe failed — it reported the same "
          f"3,300 bps for every liquidity bucket, which no real spread does")


def test_zero_spread():
    print("\n--- and roughly zero when there is no spread? ---")
    h, l = synth(spread=0.0, seed=3)
    est = lq.spread_estimate(h, l)
    check("near-zero-on-a-frictionless-series",
          est["spread_bps"] < 60,
          f"no spread applied -> {est['spread_bps']:.0f} bps, small relative to "
          f"the hundreds of bps the thin end is being tested for")
    check("negatives-are-reported",
          0.0 < est["pct_negative"] < 0.9,
          f"{est['pct_negative']:.0%} of pairs estimate NEGATIVE, which is the "
          f"estimator's known misbehaviour. It is reported rather than hidden, "
          f"and clipping them to zero biases the result DOWNWARD — the safe "
          f"direction when checking whether a cost assumption is too low")


def test_edges():
    print("\n--- edges ---")
    idx = pd.bdate_range("2024-01-01", periods=5)
    flat = pd.Series([100.0] * 5, index=idx)
    est = lq.spread_estimate(flat, flat)
    check("flat-bars-give-zero-not-nan",
          np.isfinite(est["spread_bps"]) and est["spread_bps"] == 0.0,
          "a series with no intraday range yields exactly zero")
    check("too-short-is-nan",
          not np.isfinite(lq.spread_estimate(flat.head(1), flat.head(1))["spread_bps"]),
          "one bar cannot form a pair")
    check("empty-is-nan",
          not np.isfinite(lq.spread_estimate(
              pd.Series(dtype=float), pd.Series(dtype=float))["spread_bps"]),
          "empty input yields NaN rather than raising")
    bad_h = pd.Series([100.0, 90.0, 100.0, 100.0, 100.0], index=idx)
    bad_l = pd.Series([100.0, 95.0, 100.0, 100.0, 100.0], index=idx)
    check("low-above-high-dropped",
          np.isfinite(lq.spread_estimate(bad_h, bad_l)["spread_bps"]),
          "a corrupt bar where the low exceeds the high is dropped, not "
          "propagated as NaN through the whole series")
    h, l = synth(spread=0.02, seed=9)
    h2 = h.copy(); h2.iloc[10] = np.nan
    check("nan-bars-survive",
          np.isfinite(lq.spread_estimate(h2, l)["spread_bps"]),
          "a missing bar reduces the sample instead of destroying it")


def main():
    print("=" * 70)
    print("qt.liquidity — Corwin-Schultz spread estimation (no network)")
    print("=" * 70)
    test_recovers_a_known_spread()
    test_monotone()
    test_zero_spread()
    test_edges()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("The estimator recovers a known spread to the right order of")
    print("magnitude, rises with it, and returns near zero without one —")
    print("which is exactly what the live-quote probe could not do.")


if __name__ == "__main__":
    main()
