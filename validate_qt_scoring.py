#!/usr/bin/env python3
"""Validation for qt.scoring — V29 value and risk. No network, no files.

Every answer below is computable by hand, which is the only way a scoring
layer can be checked: once it runs on real data, a wrong ranking looks exactly
like a right one.

Four things get attacked hardest, because each is a way the model would look
fine and be wrong:

  1. NEGATIVE ENTERPRISE VALUE. A company with more cash than market cap plus
     debt produces a negative denominator, and an unguarded EBIT/EV would rank
     it as the single cheapest name in the universe.
  2. THE ORDER OF OPERATIONS. Quality gate, then tier, then rank. Rank first
     and the portfolio changes.
  3. UNKNOWN RISK MUST NOT SORT SAFE. A name missing most inputs has to be
     excluded, not averaged into the low tier.
  4. DRAWDOWN SIGN. Every component must run the same direction, or one of
     them silently cancels the others.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from qt import scoring as sc

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


def test_enterprise_value():
    print("\n--- enterprise value ---")
    ev = sc.enterprise_value(price=10.0, shares=1_000_000, debt=2_000_000, cash=500_000)
    check("ev-hand-computed", ev == 11_500_000.0,
          "10 x 1M shares = 10M market cap, plus 2M debt, minus 0.5M cash = 11.5M")
    check("ev-missing-debt-and-cash-are-zero",
          sc.enterprise_value(10.0, 1_000_000, None, None) == 10_000_000.0,
          "absent debt and cash are treated as zero, not as NaN")
    check("ev-needs-price-and-shares",
          not np.isfinite(sc.enterprise_value(None, 1_000_000, 0, 0))
          and not np.isfinite(sc.enterprise_value(10.0, 0, 0, 0)),
          "no price or no share count yields NaN")


def test_ebit_ev():
    print("\n--- EBIT / EV, the primary metric ---")
    check("ratio-hand-computed",
          abs(sc.ebit_ev(1_150_000, 11_500_000) - 0.10) < 1e-12,
          "1.15M EBIT over 11.5M EV = 10%")
    check("negative-ev-is-nan",
          not np.isfinite(sc.ebit_ev(1_000_000, -5_000_000)),
          "THE TRAP: cash exceeding market cap plus debt gives a negative "
          "denominator. Unguarded, a positive EBIT over it flips negative and "
          "sorts to the TOP as the cheapest name in the universe")
    check("zero-ev-is-nan", not np.isfinite(sc.ebit_ev(1_000_000, 0)),
          "a zero denominator yields NaN rather than infinity")
    check("negative-ebit-passes-through",
          sc.ebit_ev(-1_000_000, 10_000_000) < 0,
          "a loss-making company gets a negative ratio here — it is the QUALITY "
          "GATE that removes it, not this function")


def test_quality_gate():
    print("\n--- the quality gate ---")
    ok, why = sc.passes_quality(op_income=1e6, equity=5e6, debt=2e6)
    check("profitable-modest-leverage-passes", ok and why == "ok",
          "positive EBIT, positive equity, debt/equity 0.4")
    check("unprofitable-refused",
          sc.passes_quality(-1.0, 5e6, 0)[1] == "unprofitable",
          "a cheap company losing money is not cheap, it is dying")
    check("negative-equity-refused",
          sc.passes_quality(1e6, -1.0, 0)[1] == "negative_equity",
          "negative book equity is the same story from the balance sheet")
    check("over-levered-refused",
          sc.passes_quality(1e6, 1e6, 3e6)[1] == "over_levered",
          "debt/equity 3.0 exceeds the declared bound of 2.0")
    check("at-the-bound-passes",
          sc.passes_quality(1e6, 1e6, 2e6)[0],
          "exactly 2.0 is inside the bound — the boundary is declared, so it "
          "must be tested rather than assumed")
    check("missing-inputs-refused",
          not sc.passes_quality(None, 1e6, 0)[0] and not sc.passes_quality(1e6, None, 0)[0],
          "missing fundamentals refuse the gate rather than passing by default")


def test_price_risk():
    print("\n--- volatility and drawdown ---")
    idx = pd.bdate_range("2024-01-01", periods=260)
    flat = pd.Series([100.0] * 260, index=idx)
    check("flat-series-zero-vol", sc.realized_vol(flat) == 0.0,
          "a series that never moves has zero volatility")
    check("flat-series-zero-drawdown", sc.max_drawdown(flat) == 0.0,
          "and no drawdown")

    v = np.concatenate([np.linspace(100, 200, 130), np.linspace(200, 150, 130)])
    updown = pd.Series(v, index=idx)
    mdd = sc.max_drawdown(updown)
    check("drawdown-is-positive-and-correct",
          abs(mdd - 0.25) < 1e-6,
          f"peak 200 falling to 150 is a 25% drawdown, reported POSITIVE ({mdd:.4f}) "
          f"so that larger always means riskier — a component running the other "
          f"way would cancel the others in the composite")
    check("vol-positive-on-a-moving-series", sc.realized_vol(updown) > 0,
          "a moving series has positive volatility")
    check("short-series-is-nan",
          not np.isfinite(sc.realized_vol(flat.head(5)))
          and not np.isfinite(sc.max_drawdown(flat.head(5))),
          "too little history yields NaN rather than a number from five bars")


def test_components():
    print("\n--- leverage, illiquidity, thin equity ---")
    check("leverage-hand-computed", sc.leverage_ratio(2e6, 4e6) == 0.5, "2M/4M")
    check("leverage-negative-equity-nan",
          not np.isfinite(sc.leverage_ratio(2e6, -1.0)),
          "leverage against negative equity is meaningless")
    check("illiquidity-larger-when-thinner",
          sc.illiquidity(10_000) > sc.illiquidity(1_000_000),
          "a $10k/day name scores riskier than a $1M/day name")
    check("thin-equity-hand-computed",
          abs(sc.thin_equity(equity=2e6, assets=10e6) - 0.8) < 1e-12,
          "equity 20% of assets leaves 0.8 — larger means less cushion")


def test_ranking_and_composite():
    print("\n--- percentiles and the composite ---")
    r = sc.percentile_rank([1.0, 2.0, 3.0, 4.0])
    check("percentile-monotone", r == sorted(r) and r[-1] == 1.0,
          "ranks rise with the value and top out at 1.0")
    r2 = sc.percentile_rank([1.0, float("nan"), 3.0])
    check("nan-stays-nan", not np.isfinite(r2[1]),
          "a missing value does not get a rank")
    full = {c: 0.5 for c in sc.RISK_COMPONENTS}
    check("composite-averages", sc.composite_risk(full) == 0.5,
          "five components at the median give a composite of 0.5")
    partial = {"volatility": 0.9, "drawdown": 0.7, "leverage": 0.8}
    check("composite-uses-what-is-there",
          abs(sc.composite_risk(partial) - 0.8) < 1e-12,
          "three of five present averages those three")
    check("unknown-risk-is-not-safe",
          not np.isfinite(sc.composite_risk({"volatility": 0.1})),
          "ONE component present yields NaN. A name whose inputs are missing has "
          "UNKNOWN risk, and unknown must never sort into the low tier")


def test_tiers():
    print("\n--- the operator's knob ---")
    scores = [0.05, 0.15, 0.25, 0.45, 0.55, 0.65, 0.85, 0.95, 0.99]
    tiers = sc.assign_tiers(scores)
    check("tiers-are-ordered",
          tiers[0] == "low" and tiers[4] == "moderate" and tiers[-1] == "high",
          "terciles of the day's opportunity set, not fixed thresholds")
    check("all-three-populated",
          set(tiers) == {"low", "moderate", "high"},
          f"{tiers.count('low')} low / {tiers.count('moderate')} moderate / "
          f"{tiers.count('high')} high")
    check("nan-has-no-tier",
          sc.assign_tiers([0.1, float("nan"), 0.9])[1] is None,
          "an unscored name is placed in no tier and cannot be bought")
    check("too-few-names-no-tiers",
          sc.assign_tiers([0.5, 0.6]) == [None, None],
          "two names cannot be split into terciles, so nothing is assigned")


def test_selection():
    print("\n--- portfolio selection, and the order of operations ---")
    rows = [
        {"ticker": "CHEAP_MOD", "ebit_ev": 0.30, "risk_tier": "moderate", "quality_ok": True},
        {"ticker": "MID_MOD",   "ebit_ev": 0.20, "risk_tier": "moderate", "quality_ok": True},
        {"ticker": "DEAR_MOD",  "ebit_ev": 0.05, "risk_tier": "moderate", "quality_ok": True},
        {"ticker": "CHEAPEST",  "ebit_ev": 0.90, "risk_tier": "high",     "quality_ok": True},
        {"ticker": "JUNK",      "ebit_ev": 0.80, "risk_tier": "moderate", "quality_ok": False},
    ]
    picked = sc.select_portfolio(rows, tier="moderate", n=2)
    names = [r["ticker"] for r in picked]
    check("picks-cheapest-within-tier", names == ["CHEAP_MOD", "MID_MOD"],
          "top 2 by EBIT/EV inside the moderate tier")
    check("tier-binds-before-value", "CHEAPEST" not in names,
          "the cheapest name in the universe is EXCLUDED because it sits in the "
          "high tier — the knob binds before the ranking, which is the whole "
          "point of having a knob")
    check("quality-gate-binds-first", "JUNK" not in names,
          "a name failing the gate is never ranked, however cheap")
    check("equal-weight", all(abs(r["weight"] - 0.5) < 1e-12 for r in picked),
          "two names, equal weight")
    check("weights-sum-to-one",
          abs(sum(r["weight"] for r in sc.select_portfolio(rows, "moderate", 20)) - 1.0) < 1e-12,
          "asking for more names than exist still gives a fully invested book")
    check("empty-tier-is-empty",
          sc.select_portfolio(rows, "low", 20) == [],
          "no eligible names in a tier yields no portfolio, not a fallback to "
          "another tier")
    try:
        sc.select_portfolio(rows, "aggressive", 5)
        bad = True
    except ValueError:
        bad = False
    check("unknown-tier-refused", not bad,
          "a tier outside low/moderate/high raises rather than silently "
          "returning nothing")
    check("deterministic-on-ties",
          [r["ticker"] for r in sc.select_portfolio(
              [{"ticker": "B", "ebit_ev": 0.1, "risk_tier": "low", "quality_ok": True},
               {"ticker": "A", "ebit_ev": 0.1, "risk_tier": "low", "quality_ok": True}],
              "low", 1)] == ["A"],
          "equal ratios break by ticker, so the portfolio does not depend on "
          "input order")


def main():
    print("=" * 70)
    print("qt.scoring — V29 value and risk (no network)")
    print("=" * 70)
    test_enterprise_value()
    test_ebit_ev()
    test_quality_gate()
    test_price_risk()
    test_components()
    test_ranking_and_composite()
    test_tiers()
    test_selection()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Negative enterprise value cannot rank first; unknown risk cannot")
    print("sort safe; the knob binds before the ranking; and every risk")
    print("component runs the same direction.")


if __name__ == "__main__":
    main()
