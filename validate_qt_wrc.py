#!/usr/bin/env python3
"""Validate WRC / SPA. No network.

THE LOAD-BEARING TEST is `test_data_snooping`. It is the entire reason the
E1 clause exists, shown rather than asserted: draw K=5 series of pure noise,
pick the best, and the naive one-sided t-test "rejects" ~40% of the time at
alpha=0.10 (1 - 0.9^5). WRC and SPA must bring that back near 10%. A
correction that does not do this is decoration.

Everything else here is a guard around that: a real signal must still be
found, the declared-K charge must bite, and K > K_declared must refuse.
"""
from __future__ import annotations

import sys

import numpy as np

from qt import wrc

FAILURES: list = []
SEED = 20260904


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)


def test_bootstrap_indices():
    print("\n--- stationary bootstrap: shape, range, dependence ---")
    rng = np.random.default_rng(SEED)
    n, B = 120, 50
    iid = wrc.stationary_bootstrap_indices(n, B, mean_block=1.0, rng=rng)
    blk = wrc.stationary_bootstrap_indices(n, B, mean_block=8.0, rng=rng)
    check("bs-shape", iid.shape == (B, n) and blk.shape == (B, n), f"({B}, {n})")
    check("bs-range", iid.min() >= 0 and iid.max() < n and blk.min() >= 0 and blk.max() < n,
          "all indices in [0, n)")
    consec_iid = np.mean((np.diff(iid, axis=1) % n) == 1)
    consec_blk = np.mean((np.diff(blk, axis=1) % n) == 1)
    check("bs-block-dependence", consec_blk > 0.7 and consec_iid < 0.05,
          f"consecutive-index rate: block-8 {consec_blk:.2f} vs iid {consec_iid:.3f} "
          f"— blocks preserve local order, iid does not")


def test_data_snooping():
    print("\n--- DATA SNOOPING: best-of-5 noise vs. the correction (the point) ---")
    rng = np.random.default_rng(SEED)
    K, n, trials, B = 5, 200, 120, 400
    alpha = 0.10
    naive_rej = wrc_rej = spa_rej = 0
    for _ in range(trials):
        d = rng.normal(0.0, 1.0, size=(n, K))
        # naive: pick the best series, one-sided t-test, no correction
        means, sds = d.mean(axis=0), d.std(axis=0, ddof=1)
        t_best = (means / (sds / np.sqrt(n))).max()
        if t_best > 1.2816:              # one-sided 10% critical value
            naive_rej += 1
        p_w, _, _ = wrc.wrc_pvalue(d, B=B, mean_block=1.0, rng=rng)
        p_s, _, _ = wrc.spa_pvalue(d, B=B, mean_block=1.0, rng=rng)
        wrc_rej += p_w < alpha
        spa_rej += p_s < alpha
    naive_rate, wrc_rate, spa_rate = naive_rej / trials, wrc_rej / trials, spa_rej / trials
    check("snoop-naive-inflated", naive_rate > 0.25,
          f"naive best-of-5 rejects {naive_rate:.0%} at alpha=10% (theory ≈41%) — "
          f"THIS is why the clause exists")
    check("snoop-wrc-controls", wrc_rate < 0.20,
          f"WRC rejects {wrc_rate:.0%} — brought back near the nominal 10%")
    check("snoop-spa-controls", spa_rate < 0.20,
          f"SPA rejects {spa_rate:.0%} — brought back near the nominal 10%")
    check("snoop-correction-bites", naive_rate > wrc_rate + 0.10,
          f"the correction removes at least 10 points of false rejection "
          f"({naive_rate:.0%} -> {wrc_rate:.0%})")


def test_signal_survives():
    print("\n--- a REAL effect among nulls must still be found ---")
    rng = np.random.default_rng(SEED + 1)
    n, K = 200, 5
    d = rng.normal(0.0, 1.0, size=(n, K))
    d[:, 2] += 0.35                      # t ≈ 0.35*sqrt(200) ≈ 4.9
    p_w, V, means = wrc.wrc_pvalue(d, B=800, mean_block=1.0, rng=rng)
    p_s, T, _ = wrc.spa_pvalue(d, B=800, mean_block=1.0, rng=rng)
    check("signal-wrc", p_w < 0.01, f"WRC p={p_w:.4f} on a t≈4.9 series among 4 nulls")
    check("signal-spa", p_s < 0.01, f"SPA p={p_s:.4f}")
    check("signal-identified", int(np.argmax(means)) == 2,
          f"the max is series 2 (mean {means[2]:+.3f})")


def test_declared_k_charge():
    print("\n--- the declared-K charge: unread slots are not free ---")
    rng = np.random.default_rng(SEED + 2)
    n = 200
    strong = rng.normal(0.30, 1.0, size=n)          # clearly real
    null = rng.normal(0.00, 1.0, size=n)
    res = wrc.event_study_correction({"spec_a": strong, "spec_b": null},
                                     k_declared=5, alpha=0.10, B=800, rng=rng)
    a, b = res["spec_a"], res["spec_b"]
    check("charge-bar", abs(a.adjusted_alpha - 0.02) < 1e-12,
          f"K_declared=5 -> per-spec bar alpha/K = {a.adjusted_alpha:.3f}, "
          f"not {a.alpha:.2f}")
    check("charge-strong-passes", a.passes and a.p_value <= 0.02,
          f"strong: raw p={a.p_value:.4f} <= 0.02 -> PASS")
    check("charge-null-fails", not b.passes,
          f"null: raw p={b.p_value:.4f} -> NOT MET")
    check("charge-unread-counted", "3 unread slot(s) charged" in a.note,
          f"note says so: '{a.note}'")

    # k_tested override: the E1 job passes ONE series while earlier specs sit
    # frozen in their own ledgers, so it supplies the Referee's read count.
    # The bar must not move; the label must.
    solo = wrc.event_study_correction({"spec_c": strong}, k_declared=5,
                                      alpha=0.10, B=200, rng=rng, k_tested=2)
    c = solo["spec_c"]
    check("ktested-bar-unchanged", abs(c.adjusted_alpha - 0.02) < 1e-12,
          f"bar still alpha/K_declared = {c.adjusted_alpha:.3f} with k_tested=2")
    check("ktested-label-correct", "3 unread slot(s) charged" in c.note
          and c.k_tested == 2,
          f"one series supplied, k_tested=2 -> '{c.note}'")
    try:
        wrc.event_study_correction({"a": strong, "b": null}, k_declared=5,
                                   B=50, rng=rng, k_tested=1)
        bad = False
    except ValueError as exc:
        bad = "fewer than" in str(exc)
    check("ktested-refuses-undercount", bad,
          "k_tested below the number of series supplied RAISES")

    # Joint version on aligned series charges the same way.
    d = np.column_stack([strong, null])
    j = wrc.joint_correction(d, k_declared=5, alpha=0.10, B=400, rng=rng)
    check("joint-charge", abs(j["WRC"].adjusted_alpha - 0.04) < 1e-12,
          f"2 of 5 tested -> joint bar 0.10*2/5 = {j['WRC'].adjusted_alpha:.3f}")
    check("joint-signal", j["WRC"].passes and j["SPA"].passes,
          f"WRC p={j['WRC'].p_value:.4f}, SPA p={j['SPA'].p_value:.4f}")


def test_refusals():
    print("\n--- refusals ---")
    rng = np.random.default_rng(SEED + 3)
    d6 = rng.normal(size=(50, 6))
    try:
        wrc.joint_correction(d6, k_declared=5, B=50, rng=rng)
        ok = False
    except ValueError as exc:
        ok = "exceeds the declared K" in str(exc)
    check("refuse-over-k", ok, "6 series against K=5 RAISES — testing beyond K "
                               "invalidates E1 for every spec")
    try:
        wrc.wrc_pvalue(np.array([[1.0, np.nan], [2.0, 3.0]]), B=10)
        ok2 = False
    except ValueError:
        ok2 = True
    check("refuse-nan", ok2, "NaN input RAISES rather than silently shrinking n")
    r = wrc.event_study_correction({"tiny": np.array([0.1])}, k_declared=5, B=10)
    check("tiny-not-pass", not r["tiny"].passes and "too few" in r["tiny"].note,
          "a one-observation spec cannot pass")


def main():
    print("=" * 70)
    print("qt.wrc — White's Reality Check + Hansen's SPA (no network)")
    print("=" * 70)
    test_bootstrap_indices()
    test_data_snooping()
    test_signal_survives()
    test_declared_k_charge()
    test_refusals()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Best-of-5 noise is not significant; a real effect still is; and")
    print("the unread K slots are charged.")


if __name__ == "__main__":
    main()
