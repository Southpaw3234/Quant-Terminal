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


def test_live_registry():
    print("\n--- referee: the LIVE registry, not a fixture ---")
    p = Path("data/registry/specifications.json")
    if not p.exists():
        check("live-registry-present", False, f"{p} missing")
        return
    r = referee.Referee(registry_path=p)
    check("live-k-used", r.k_used() == 2,
          f"K {r.k_used()}/{r.k_budget} spent, {r.k_remaining()} remaining "
          f"(v1 read 2026-09-02, v2 read 2026-09-05)")
    check("live-e1-locked",
          not r.authorize_read("form4_cluster_buy_v1", "2026-12-15").allowed,
          "form4_cluster_buy_v1 is READ and cannot be read again")
    spec = r.specs.get("form4_cluster_buy_v1")
    check("live-e1-verdict", spec and spec.result.get("verdict") == "NOT MET",
          f"recorded verdict: {spec.result.get('verdict') if spec else 'n/a'}")
    check("live-caveats-travel", spec and len(spec.result.get("caveats", [])) >= 3,
          "the survivorship / next-session-entry / WRC caveats are stored WITH "
          "the number, not left in a PR description nobody reads later")
    print(f"\n{r.status()}")




def test_spec2_declared():
    print("\n--- referee: spec #2 was DECLARED 09-04, then READ 09-05 — NOT MET ---")
    p = Path("data/registry/specifications.json")
    r = referee.Referee(registry_path=p)
    s2 = r.specs.get("form4_cluster_buy_v2")
    check("spec2-present", s2 is not None, "form4_cluster_buy_v2 is in the live registry")
    check("spec2-read", s2 is not None and s2.is_read and s2.read_at == "2026-09-05",
          f"read_at={s2.read_at if s2 else 'n/a'} — declared 09-04, read 09-05, "
          f"in that order")
    check("spec2-k-spent", r.k_used() == 2 and r.k_remaining() == 3,
          f"K {r.k_used()}/{r.k_budget} spent — the read consumed one; "
          f"{r.k_remaining()} remain")
    v = r.authorize_read("form4_cluster_buy_v2", "2026-12-15")
    check("spec2-anti-deferral", not v.allowed and "already read" in v.reason,
          f"a second read of v2 is REFUSED — no bug found afterwards reopens it: "
          f"{v.reason[:60]}")
    check("spec2-not-before-declared",
          not r.authorize_read("form4_cluster_buy_v2", "2026-09-03").allowed,
          "a read dated BEFORE 2026-09-04 is refused")
    check("spec2-horizon-63", s2 is not None and s2.params.get("horizon_bars") == 63,
          "frozen horizon is 63 bars (the fork decision)")
    check("spec2-lookback-1095", s2 is not None and s2.params.get("lookback_days") == 1095,
          "frozen lookback is 1,095 days")
    check("spec2-output-not-v1-ledger",
          s2 is not None and "event_study_v2.csv" in s2.params.get("output_ledger", "")
          and "NEVER" in s2.params.get("output_ledger", ""),
          "output ledger is event_study_v2.csv and the record says NEVER v1's")
    _prior = str(s2.result.get("prior", "")) if s2 is not None else ""
    check("spec2-prior-recorded",
          "= 10%" in _prior and "signed" in _prior and "BEFORE the read" in _prior,
          f"the per-spec prior is RECORDED, signed, dated before the read: "
          f"{_prior[:58]}...")
    _dev = (s2.result.get("deviations") or []) if s2 is not None else []
    check("spec2-miswire-recorded",
          any("MISWIRED" in str(d.get("what", "")) for d in _dev),
          "the 2026-09-05 miswired dispatch is RECORDED as a deviation, with its "
          "result, so the accidental subset peek cannot be forgotten either way")
    _rd = (s2.result.get("read") or {}) if s2 is not None else {}
    check("spec2-read-recorded",
          _rd.get("verdict") == "NOT MET" and _rd.get("n") == 406
          and _rd.get("run_id") == "33934605296",
          f"the declared read is RECORDED: n={_rd.get('n')} mean={_rd.get('mean')} "
          f"t={_rd.get('t')} -> {_rd.get('verdict')} (run {_rd.get('run_id')})")
    check("spec2-read-after-decision",
          any("PROCEED" in str(d.get("operator_decision", "")) for d in _dev),
          "the operator's PROCEED decision is recorded alongside the deviation, "
          "and precedes the read in git history")
    check("spec2-linear-accrual-falsified",
          "falsified" in str(_rd.get("vs_miswired_peek", "")),
          "the record says plainly that tripling the sample CUT the mean — the "
          "power assumption behind every projection did not hold")
    _tail = _rd.get("tail_analysis") or {}
    check("spec2-tail-recorded",
          _tail.get("median", 0) < 0 and _tail.get("mean_without_top_decile", 0) < 0
          and "LOSES MONEY" in str(_tail.get("reading", "")),
          f"the tail analysis is RECORDED: median {_tail.get('median')}, mean "
          f"without top decile {_tail.get('mean_without_top_decile')} — the "
          f"typical event loses; the mean is a 10% right tail")
    check("spec2-tail-implication",
          "MEDIAN" in str(_tail.get("implication_for_spec_3", ""))
          and "variations" in str(_tail.get("implication_for_spec_3", "")),
          "and it says what spec #3 must NOT be: a filter on the same "
          "distribution")
    check("spec2-caveats-travel",
          s2 is not None and len(s2.result.get("caveats_to_travel_with_any_number", [])) >= 5,
          "survivorship / two-parameter / linear-accrual caveats stored WITH the spec")
    print(f"\n{r.status()}")

def test_spec3_declared():
    print("\n--- referee: spec #3 DECLARED 09-05, UNREAD, prior still owed ---")
    p = Path("data/registry/specifications.json")
    r = referee.Referee(registry_path=p)
    s3 = r.specs.get("pead_8k_car_v3")
    check("spec3-present", s3 is not None, "pead_8k_car_v3 is in the live registry")
    check("spec3-declared-unread",
          s3 is not None and s3.declared_at == "2026-09-05" and not s3.is_read,
          f"declared {s3.declared_at if s3 else 'n/a'}, read_at empty — "
          f"declaration precedes any read, which is the whole point")
    check("spec3-costs-no-k", r.k_used() == 2 and r.k_remaining() == 3,
          f"K is still {r.k_used()}/{r.k_budget} — DECLARING SPENDS NOTHING; "
          f"only a read does. {r.k_remaining()} slots remain")
    check("spec3-read-would-be-authorised",
          r.authorize_read("pead_8k_car_v3", "2026-12-15").allowed,
          "the Referee WOULD authorise a read — declared, unread, in budget, "
          "before terminal")
    _p3 = s3.params if s3 is not None else {}
    check("spec3-threshold-fixed-before-data",
          "+5.0%" in str(_p3.get("selection_threshold", ""))
          and "before the CAR distribution" in str(_p3.get("selection_threshold", "")),
          "the +5.0% selection bar is RECORDED and the record says it was fixed "
          "before the distribution was ever computed")
    check("spec3-surprise-is-abnormal-return",
          "ABNORMAL RETURN" in str(_p3.get("surprise_measure", ""))
          and "NOT an analyst-estimate SUE" in str(_p3.get("surprise_measure", "")),
          "surprise = announcement-window abnormal return, and the record says "
          "plainly that it is not a consensus-based SUE")
    check("spec3-entry-after-selection-window",
          "STRICTLY AFTER the close of A+1" in str(_p3.get("entry", ""))
          and "A+2" in str(_p3.get("entry", "")),
          "entry is A+2 — strictly after the two sessions the SELECTION used, "
          "so the selection window is never traded")
    check("spec3-horizon-63", _p3.get("horizon_bars") == 63,
          "horizon 63 bars, unchanged from the fork decision")
    _out = str(_p3.get("output_ledger", ""))
    check("spec3-ledger-not-v1-or-v2",
          "event_study_v3.csv" in _out and "NEVER" in _out
          and "event_study_v2.csv" in _out,
          "output is v3's own ledger and the record names BOTH frozen files it "
          "must never touch")
    _res = s3.result if s3 is not None else {}
    check("spec3-prior-still-owed",
          "NOT YET RECORDED" in str(_res.get("prior", "")),
          "THE PRIOR IS STILL OWED. This check is a TRIPWIRE: it must be "
          "flipped to assert a signed prior before any read is dispatched")
    check("spec3-extractor-not-built",
          "NOT YET BUILT" in str(_res.get("availability_checks", {}).get("extractor", "")),
          "the events file does not exist yet, and the record says so — the "
          "threshold was fixed before the extractor that will produce it")
    check("spec3-reversal-caveat",
          any("REVERSAL" in c for c in _res.get("caveats_to_travel_with_any_number", [])),
          "the short-term-reversal headwind built into selecting on a +5% move "
          "is recorded as a DOWNWARD bias, not hidden")
    check("spec3-is-not-a-variation",
          "SLOW DIFFUSION OF PUBLIC INFORMATION" in str(_p3.get("why_this_is_not_a_variation_of_spec_1_or_2", "")),
          "the record states the mechanism differs from specs #1 and #2 rather "
          "than filtering the same distribution")
    v = r.declare("pead_8k_car_v3", {"horizon_bars": 21}, "2026-09-06")
    check("spec3-frozen-once-declared",
          not v.allowed and "NEW specification" in v.reason,
          f"redefining v3's parameters is REFUSED: {v.reason[:64]}")
    print(f"\n{r.status()}")


def main():
    print("=" * 70)
    print("qt Phase 1 — Measurement + Referee (no network)")
    print("=" * 70)
    test_reproduces_e1()
    test_summarize_edges()
    test_referee_refuses()
    test_referee_budget_and_persistence()
    test_referee_records_e1()
    test_live_registry()
    test_spec2_declared()
    test_spec3_declared()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Measurement reproduces the frozen E1 read exactly; the Referee")
    print("refuses undeclared, re-read, over-budget and post-terminal reads.")


if __name__ == "__main__":
    main()
