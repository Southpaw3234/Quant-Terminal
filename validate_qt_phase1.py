#!/usr/bin/env python3
"""Validate qt Phase 1 — Measurement and Referee. No network.

THE EXIT GATE for Phase 1 is `test_reproduces_e1`: the rebuilt Measurement must
reproduce the frozen E1 read EXACTLY from `data/events/event_study.csv`:

    n = 160    mean = +1.7647%    t = +1.16    stability = -0.52
    first third +1.0061%  ->  last third -0.5196%

"Exactly" is not a figure of speech. A rebuild that shifts a statistic by 0.03
against a bar of 2.0, on a series nobody re-derives by hand, is the failure mode
v25 spent months discovering — and it would be worse here, because the new
number would look more defensible than the old one.

The Referee tests are all about REFUSAL. A referee that only says yes is
scenery.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from qt import measurement, referee

FAILURES: list = []
LEDGER = Path("data/events/event_study.csv")


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)


# ───────────────────────────────────────────────── THE EXIT GATE

def test_reproduces_e1():
    print("\n--- PHASE 1 EXIT GATE: reproduce the frozen E1 read ---")
    if not LEDGER.exists():
        check("e1-ledger-present", False, f"{LEDGER} missing")
        return
    df = measurement.read_ledger(LEDGER)
    r = measurement.summarize(df, "E1-repro")

    check("e1-n", r.n == 160, f"n={r.n} (frozen read: 160)")
    check("e1-mean", f"{r.mean:+.4%}" == "+1.7647%",
          f"mean={r.mean:+.4%} (frozen read: +1.7647%)")
    check("e1-t", f"{r.t:+.2f}" == "+1.16",
          f"t={r.t:+.2f} (frozen read: +1.16)")
    check("e1-stability", f"{r.stability:.2f}" == "-0.52",
          f"stability={r.stability:.2f} (frozen read: -0.52)")
    check("e1-first-third", f"{r.first_third:+.4%}" == "+1.0061%",
          f"first third={r.first_third:+.4%} (frozen read: +1.0061%)")
    check("e1-last-third", f"{r.last_third:+.4%}" == "-0.5196%",
          f"last third={r.last_third:+.4%} (frozen read: -0.5196%)")
    check("e1-verdict", not r.passes,
          f"verdict NOT MET, missing: {'; '.join(r.failures())}")


def test_summarize_edges():
    print("\n--- measurement: the stability clause and its edges ---")
    # A planted decay: strong first half, weak second. Must read as unstable.
    n = 60
    vals = [0.04] * (n // 2) + [0.001] * (n - n // 2)
    df = pd.DataFrame({"abnormal_ret": vals,
                       "event_ts": pd.date_range("2026-01-01", periods=n)})
    r = measurement.summarize(df, "decay")
    check("stability-decays", r.stability < measurement.E1_STABILITY,
          f"stability={r.stability:.2f} — a decaying effect fails the clause "
          f"even with a healthy mean of {r.mean:+.2%}")

    # Negative first third -> stability is UNDEFINED, and undefined must MISS.
    # A ratio against a negative baseline is a sign artifact, and can read
    # positive for a series that got worse.
    vals2 = [-0.02] * 20 + [0.0] * 20 + [-0.01] * 20
    df2 = pd.DataFrame({"abnormal_ret": vals2,
                        "event_ts": pd.date_range("2026-01-01", periods=60)})
    r2 = measurement.summarize(df2, "neg-first")
    check("stability-undefined", not np.isfinite(r2.stability) and not r2.passes,
          "negative first third -> stability undefined -> NOT MET, never a "
          "pass by sign artifact")

    tiny = pd.DataFrame({"abnormal_ret": [0.5], "event_ts": ["2026-01-01"]})
    r3 = measurement.summarize(tiny, "tiny")
    check("summarize-tiny", r3.n == 1 and not r3.passes,
          "a single observation cannot pass anything")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "empty.csv"
        p.write_text("abnormal_ret,event_ts\n")
        try:
            measurement.read_ledger(p)
            ok = False
        except ValueError:
            ok = True
        check("empty-ledger-raises", ok,
              "an empty ledger RAISES rather than summarising as n=0 — a null "
              "read and a missing file must never look alike")


# ───────────────────────────────────────────────────────── referee

def _fresh(td):
    return referee.Referee(registry_path=Path(td) / "reg.json")


def test_referee_refuses():
    print("\n--- referee: the refusals (a referee that only says yes is scenery) ---")
    with tempfile.TemporaryDirectory() as td:
        r = _fresh(td)

        v = r.authorize_read("never_declared", "2026-10-01")
        check("ref-undeclared", not v.allowed,
              f"undeclared spec REFUSED — {v.reason[:60]}…")

        r.declare("spec_a", {"horizon": 21}, "2026-10-01")
        check("ref-declare-free", r.k_used() == 0,
              "declaring costs nothing — thinking is free, measuring is scarce")

        v = r.authorize_read("spec_a", "2026-09-01")
        check("ref-read-before-declare", not v.allowed,
              "a read dated BEFORE its declaration is refused — that is the "
              "whole difference between pre-registration and rationalisation")

        v = r.declare("spec_a", {"horizon": 42}, "2026-10-02")
        check("ref-redefine", not v.allowed,
              f"redefining a declared spec REFUSED — {v.reason[:60]}…")

        r.record_read("spec_a", "2026-10-05", {"verdict": "NOT MET"})
        check("ref-k-counts", r.k_used() == 1 and r.k_remaining() == 4,
              f"a READ spends budget: {r.k_used()}/5 used")

        v = r.authorize_read("spec_a", "2026-11-01")
        check("ref-anti-deferral", not v.allowed,
              "re-reading a read spec REFUSED — no bug found afterwards "
              "reopens it")

        r.declare("spec_late", {"h": 1}, "2027-01-01")
        v = r.authorize_read("spec_late", "2027-06-01")
        check("ref-terminal", not v.allowed,
              f"a read past the terminal date REFUSED — {v.reason[:50]}…")


def test_referee_budget_and_persistence():
    print("\n--- referee: budget exhaustion and durable state ---")
    with tempfile.TemporaryDirectory() as td:
        r = _fresh(td)
        for i in range(5):
            r.declare(f"s{i}", {"i": i}, "2026-10-01")
            r.record_read(f"s{i}", "2026-10-02", {"verdict": "NOT MET"})
        check("ref-budget-used", r.k_used() == 5 and r.k_remaining() == 0,
              "5 of 5 spent")
        r.declare("s5", {"i": 5}, "2026-10-03")
        v = r.authorize_read("s5", "2026-10-04")
        check("ref-budget-exhausted", not v.allowed,
              "the 6th read REFUSED — testing beyond K invalidates E1 for "
              "every specification, including those already read")
        r.save()

        r2 = referee.Referee(registry_path=Path(td) / "reg.json")
        check("ref-persists", r2.k_used() == 5,
              f"reloaded registry still reads {r2.k_used()}/5 — a rebuild does "
              f"NOT reset the budget")

        bad = Path(td) / "bad.json"
        bad.write_text("{ not json")
        try:
            referee.Referee(registry_path=bad)
            ok = False
        except ValueError:
            ok = True
        check("ref-fail-closed", ok,
              "an unreadable registry RAISES — an unknown budget must never be "
              "treated as an empty one")


def test_referee_records_e1():
    print("\n--- referee: the real E1 spec-1 state ---")
    with tempfile.TemporaryDirectory() as td:
        r = _fresh(td)
        params = {
            "universe": "907 names, 250k <= ADV <= 5M, price >= $2, >=200 bars",
            "events": ">=2 distinct insiders within 5d, aggregate >= $100,000",
            "horizon_bars": 21,
            "control": "SPY market-adjusted",
        }
        r.declare("form4_cluster_buy_v1", params, "2026-09-01")
        v = r.record_read("form4_cluster_buy_v1", "2026-09-02",
                          {"verdict": "NOT MET", "n": 160, "mean": 0.017647,
                           "t": 1.16, "stability": -0.52})
        check("ref-e1-recorded", v.allowed and r.k_used() == 1,
              f"E1 spec 1 recorded — {r.k_remaining()} of 5 remain")
        check("ref-e1-locked",
              not r.authorize_read("form4_cluster_buy_v1", "2026-12-15").allowed,
              "and it cannot be read again")


def main():
    print("=" * 70)
    print("qt Phase 1 — Measurement + Referee (no network)")
    print("=" * 70)
    test_reproduces_e1()
    test_summarize_edges()
    test_referee_refuses()
    test_referee_budget_and_persistence()
    test_referee_records_e1()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Measurement reproduces the frozen E1 read exactly; the Referee")
    print("refuses undeclared, re-read, over-budget and post-terminal reads.")


if __name__ == "__main__":
    main()
