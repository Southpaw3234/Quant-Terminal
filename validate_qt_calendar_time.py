#!/usr/bin/env python3
"""Validation for qt.calendar_time — V29 measurement. No network, no files.

Three properties decide whether this measures a portfolio or flatters one, and
each is checked against a constructed answer:

  1. NEWEY-WEST MUST ACTUALLY CORRECT. On independent data it has to collapse
     to the ordinary standard error; on autocorrelated data it has to be
     LARGER. A "correction" that does neither is decoration, and it would
     inflate t on exactly the overlapping book this specification holds.
  2. A DELISTED NAME MUST NOT LEAVE THE AVERAGE. Taking the mean of whatever
     still has data deletes the loser and improves the result.
  3. COSTS MUST LAND. 75 bps on a full turnover is 75 bps off that day.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from qt import calendar_time as ct

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


IDX = pd.bdate_range("2024-01-01", periods=10)


def test_newey_west():
    print("\n--- Newey-West: does the correction correct? ---")
    rng = np.random.default_rng(20260906)
    iid = pd.Series(rng.normal(0, 1, 4000))
    plain = float(iid.std(ddof=1) / np.sqrt(len(iid)))
    nw = ct.newey_west_se(iid, lag=10)
    check("collapses-to-ols-on-iid",
          abs(nw - plain) / plain < 0.15,
          f"independent data: NW {nw:.5f} vs ordinary {plain:.5f}, within 15% — "
          f"a correction that inflated here would be penalising noise")

    # AR(1) with phi = 0.8: strongly autocorrelated, same innovations
    e = rng.normal(0, 1, 4000)
    ar = np.zeros(4000)
    for i in range(1, 4000):
        ar[i] = 0.8 * ar[i - 1] + e[i]
    s = pd.Series(ar)
    plain_ar = float(s.std(ddof=1) / np.sqrt(len(s)))
    nw_ar = ct.newey_west_se(s, lag=10)
    check("larger-on-autocorrelated-data",
          nw_ar > plain_ar * 1.5,
          f"AR(1) phi=0.8: NW {nw_ar:.5f} vs ordinary {plain_ar:.5f} — the "
          f"overlapping book this spec holds looks exactly like this, and an "
          f"uncorrected t would be inflated by roughly that ratio")
    check("t-uses-the-corrected-se",
          abs(ct.t_stat(s, 10) - s.mean() / nw_ar) < 1e-9,
          "t is the mean over the Newey-West standard error, not the naive one")
    check("short-series-is-nan",
          not np.isfinite(ct.newey_west_se([1.0, 2.0])),
          "two observations yield NaN rather than a number")


def test_return_stats():
    print("\n--- return statistics ---")
    flat = pd.Series([0.0] * 252)
    check("zero-return-annualises-to-zero",
          abs(ct.annualized_return(flat)) < 1e-12, "no return, no growth")
    tenth = pd.Series([0.001] * 252)
    check("annualises-geometrically",
          abs(ct.annualized_return(tenth) - ((1.001 ** 252) - 1)) < 1e-9,
          "10 bps a day compounds to (1.001^252 - 1), not 252 x 10 bps")
    dd = ct.max_drawdown_from_returns(pd.Series([0.5, -0.5, 0.0]))
    check("drawdown-is-positive",
          abs(dd - 0.5) < 1e-12,
          "1.5 falling to 0.75 is a 50% drawdown, reported positive")
    check("drawdown-zero-when-monotone",
          ct.max_drawdown_from_returns(pd.Series([0.01] * 10)) == 0.0,
          "a series that only rises never draws down")
    ir = ct.information_ratio(pd.Series([0.001] * 100 + [-0.001] * 100))
    check("ir-finite-and-signed", np.isfinite(ir) and abs(ir) < 1e-9,
          "a symmetric excess series has an information ratio of zero")


def test_stability():
    print("\n--- stability ---")
    rising = pd.Series([0.001] * 30 + [0.002] * 30 + [0.003] * 30)
    check("improving-series-above-one",
          ct.stability(rising) > 1.0,
          "a strengthening effect scores above 1")
    decaying = pd.Series([0.003] * 30 + [0.002] * 30 + [0.0005] * 30)
    st = ct.stability(decaying)
    check("decaying-series-below-one", 0 < st < 0.5,
          f"a decaying effect scores {st:.2f}, below the 0.50 bar")
    check("negative-first-third-is-undefined",
          ct.stability(pd.Series([-0.002] * 30 + [0.001] * 30 + [0.003] * 30)) is None,
          "a non-positive first third makes the ratio meaningless, and the "
          "specification counts undefined as a MISS rather than a pass")


def test_portfolio_construction():
    print("\n--- the daily series, and the name that stops trading ---")
    rets = pd.DataFrame({"A": [0.10] * 10, "B": [0.0] * 10}, index=IDX)
    sched = [(IDX[0], ["A", "B"])]
    out = ct.portfolio_daily_returns(sched, rets)
    check("equal-weighted",
          abs(out.iloc[0] - 0.05) < 1e-12,
          "+10% and 0% at equal weight is +5%")

    # B delists after day 4: its returns go NaN
    dead = rets.copy()
    dead.loc[IDX[5:], "B"] = np.nan
    out2 = ct.portfolio_daily_returns(sched, dead, delisting_return=0.0)
    check("dead-name-does-not-leave-the-average",
          abs(out2.iloc[6] - 0.05) < 1e-12,
          "THE TRAP: a mean of surviving names would pay 10% here, silently "
          "deleting the dead sleeve. Weights stay fixed, so the book pays 5%")
    out3 = ct.portfolio_daily_returns(sched, dead, delisting_return=-1.0)
    check("delisting-assumption-is-a-parameter",
          abs(out3.iloc[6] - (-0.45)) < 1e-12,
          "a -100% delisting assumption gives 0.5(0.10) + 0.5(-1.00) = -45%; "
          "the value is DECLARED, and the registry records it as unmade")

    later = [(IDX[0], ["A"]), (IDX[5], ["B"])]
    out4 = ct.portfolio_daily_returns(later, rets)
    check("schedule-switches-on-the-effective-date",
          abs(out4.iloc[4] - 0.10) < 1e-12 and abs(out4.iloc[5] - 0.0) < 1e-12,
          "holds A through day 4 and B from day 5")
    check("before-any-rebalance-is-flat",
          ct.portfolio_daily_returns([(IDX[5], ["A"])], rets).iloc[0] == 0.0,
          "no book yet means no return, not an error")
    check("empty-inputs-safe",
          ct.portfolio_daily_returns([], rets).empty
          and ct.portfolio_daily_returns(sched, pd.DataFrame()).empty,
          "empty schedule or empty returns yield an empty series")


def test_turnover_and_costs():
    print("\n--- turnover and costs ---")
    check("full-replacement", ct.turnover(["A", "B"], ["C", "D"]) == 1.0,
          "nothing retained is a full round trip")
    check("no-change", ct.turnover(["A", "B"], ["A", "B"]) == 0.0,
          "an unchanged book trades nothing")
    check("half-replacement", ct.turnover(["A", "B"], ["A", "C"]) == 0.5,
          "one of two names replaced")
    check("first-rebalance-is-full", ct.turnover([], ["A", "B"]) == 1.0,
          "building the first book is a full purchase")
    base = pd.Series([0.0] * 10, index=IDX)
    netted = ct.apply_costs(base, {IDX[3]: 1.0}, round_trip_bps=75)
    check("cost-lands-on-the-rebalance-day",
          abs(netted.iloc[3] - (-0.0075)) < 1e-12 and netted.iloc[2] == 0.0,
          "75 bps on a full turnover is -0.75% that day and nothing on the others")
    check("cost-scales-with-turnover",
          abs(ct.apply_costs(base, {IDX[3]: 0.5}, 75).iloc[3] - (-0.00375)) < 1e-12,
          "half the book turning over costs half as much")


def test_evaluate():
    print("\n--- the read ---")
    rng = np.random.default_rng(7)
    n = 1200
    bench = pd.Series(rng.normal(0.0002, 0.01, n))
    # NOISE MATTERS HERE. The first version of this test used `bench + 0.0008`,
    # a CONSTANT excess. Constant means zero variance, zero standard error, and
    # a t-statistic of 4e16 — it passed while proving nothing, because no real
    # strategy has a riskless edge. The excess now carries its own dispersion,
    # so t and the information ratio are finite and have to be earned.
    strong = bench + 0.0008 + rng.normal(0, 0.004, n)
    res = ct.evaluate(strong, bench)
    check("strong-strategy-clears",
          res["verdict"] == "MET" and 2.0 < res["t_excess"] < 100.0,
          f"+8 bps a day with 40 bps of tracking error: t {res['t_excess']:.1f}, "
          f"IR {res['information_ratio']:.2f}, maxDD {res['max_dd']:.1%} vs "
          f"benchmark {res['max_dd_bench']:.1%}, verdict {res['verdict']}")
    check("t-is-finite-and-plausible",
          np.isfinite(res["t_excess"]) and res["t_excess"] < 100.0,
          f"t {res['t_excess']:.1f} is a number a real strategy could produce — "
          f"an unbounded t means the test built a riskless edge, not a good one")
    null = bench + rng.normal(0, 0.005, n)
    res2 = ct.evaluate(null, bench)
    check("null-strategy-fails",
          res2["verdict"] == "NOT MET",
          f"pure noise around the benchmark: {'; '.join(res2['missed'][:2])}")
    short = ct.evaluate(strong.iloc[:100], bench.iloc[:100])
    check("short-sample-misses-on-n",
          any("N 100" in m for m in short["missed"]),
          "100 days misses the 1,000-day floor and says so explicitly")
    check("reports-both-sides",
          res["cleared"] and isinstance(res["missed"], list),
          "each bar is reported cleared or missed, so a near miss stays legible")


def main():
    print("=" * 70)
    print("qt.calendar_time — V29 measurement (no network)")
    print("=" * 70)
    test_newey_west()
    test_return_stats()
    test_stability()
    test_portfolio_construction()
    test_turnover_and_costs()
    test_evaluate()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Newey-West collapses on independent data and widens on")
    print("autocorrelated data; a delisted name stays in the book at its")
    print("declared assumption; and costs land on the rebalance day.")


if __name__ == "__main__":
    main()
