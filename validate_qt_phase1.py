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

import json
import re
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
    _read = sorted(sid for sid, s in r.specs.items() if s.is_read)
    check("live-k-within-budget", 0 <= r.k_used() <= r.k_budget,
          f"K {r.k_used()}/{r.k_budget} spent, {r.k_remaining()} remaining. "
          f"The budget belongs to the PROGRAMME, so this asserts the ceiling "
          f"and lets the count grow: read so far -> {', '.join(_read)}")
    check("live-every-read-has-a-verdict",
          all(r.specs[sid].result.get("verdict")
              or (r.specs[sid].result.get("read") or {}).get("verdict")
              for sid in _read),
          "every spec with a read_at carries a recorded verdict — a spent slot "
          "with no verdict is a read that never reached the reader")
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
    check("spec2-k-spent",
          s2 is not None and s2.is_read and 2 <= r.k_used() <= r.k_budget,
          f"v2's read is counted in K {r.k_used()}/{r.k_budget}. A LOWER bound, "
          f"not an equality: ref-k-counts already proves a read spends a slot, "
          f"and a later spec's read must not turn this check red")
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

def test_v29_registry_budget():
    print("\n--- the Referee reads the BUDGET from the registry it was handed ---")
    v29 = Path("data/registry/v29_specifications.json")
    if not v29.exists():
        check("v29-registry-present", False, "data/registry/v29_specifications.json missing")
        return
    r29 = referee.Referee(registry_path=v29)
    check("v29-budget-from-file",
          r29.k_budget == 3,
          f"K {r29.k_budget}, not 5. THE BUG THIS CATCHES: budget and terminal "
          f"date used to come from module constants, so a v29 registry reported "
          f"v27's K=5 and v27's deadline — v29 specs counted against the wrong "
          f"budget while the status line looked entirely normal")
    check("v29-terminal-from-file",
          r29.terminal_date == "2027-06-30",
          f"terminal {r29.terminal_date}, not v27's 2027-03-31")
    # ⚠️ TRIPWIRES FLIPPED 2026-11-08, when value_ebit_ev_v1 was read.
    # These three asserted V29 was unread and correctly went red the moment
    # that stopped being true (run 42455224973). They now pin the POST-READ
    # state, which is the stronger assertion: anti-deferral means a read
    # specification can never be re-read, so a future green here proves the
    # budget was not quietly rewound.
    # ⚠️ This check pinned K at 1 and went red when v2 was read on 2026-11-09
    # — the second time today a tripwire did its job. It no longer pins the
    # TOTAL: the budget belongs to the programme, and v29-v2-read-and-frozen
    # owns that count. This one asserts only what it is about, which is that
    # v1's read happened and is frozen.
    check("v29-v1-read-spent", r29.specs["value_ebit_ev_v1"].is_read
          and r29.k_used() >= 1,
          "value_ebit_ev_v1 was read 2026-11-08 (run 42453397592)")
    check("v29-spec-read-and-frozen",
          "value_ebit_ev_v1" in r29.specs and r29.specs["value_ebit_ev_v1"].is_read,
          "value_ebit_ev_v1 is READ and can never be read again")
    check("v29-reread-is-refused",
          not r29.authorize_read("value_ebit_ev_v1", "2026-12-01").allowed,
          "the Referee REFUSES a second read — anti-deferral, and no bug found "
          "afterwards reopens it")
    check("v29-refuses-past-its-own-terminal",
          not r29.authorize_read("value_ebit_ev_v1", "2027-08-01").allowed,
          "and refuses one past 2027-06-30, which under the old behaviour would "
          "have been checked against v27's earlier deadline instead")

    # value_ebit_ev_v2 — declared 2026-11-08, UNREAD. These exist because the
    # Referee tests none of them: authorize_read checks already-read, date
    # order, terminal date and K budget, and nothing else. The prior and the
    # declared holdings are enforced here or nowhere.
    _v2 = r29.specs.get("value_ebit_ev_v2")
    _p2 = str((_v2.result if _v2 is not None else {}).get("prior", ""))
    # ⚠️ TRIPWIRE FLIPPED 2026-11-09, when v2 was read. It asserted v2 was
    # unread and went red the moment that stopped being true, which is the
    # tripwire working. It now pins the post-read state, the stronger claim:
    # both V29 slots spent are accounted for, and a re-read is refused.
    check("v29-v2-read-and-frozen",
          _v2 is not None and _v2.is_read and _v2.read_at == "2026-11-09"
          and 2 <= r29.k_used() <= r29.k_budget,
          f"v2 was read 2026-11-09 and is counted in K {r29.k_used()}/"
          f"{r29.k_budget}. Lower bound on K, not an equality: a third V29 "
          f"read must not turn a check about v2 red")
    check("v29-v2-reread-is-refused",
          not r29.authorize_read("value_ebit_ev_v2", "2026-12-01").allowed,
          "and a second read is REFUSED — anti-deferral, and no bug found "
          "afterwards reopens it")
    check("v29-v2-verdict-recorded",
          str((_v2.result if _v2 is not None else {})
              .get("read", {}).get("verdict", "")) == "NOT MET",
          "the verdict is in the registry, not only in a run log that ages out")
    check("v29-v2-prior-signed",
          "NOT YET RECORDED" not in _p2 and "signed Southpaw3234" in _p2
          and "%" in _p2,
          "the prior is RECORDED, signed and dated BEFORE any read")
    check("v29-v2-prior-not-above-v1",
          "12%" in _p2,
          "and it is 12%, BELOW v1's 20-25%: the mechanism is identical and "
          "only the plumbing differs, so a prior that already missed on four "
          "of eight criteria is a ceiling here, not a floor")
    r27 = referee.Referee(registry_path=Path("data/registry/specifications.json"))

    # Declared holdings must be machine-readable and must be what the script
    # uses. The Referee tests neither; this is the only check there is.
    _v2h = r29.specs.get("value_ebit_ev_v2")
    check("v29-v2-holdings-declared-machine-readable",
          _v2h is not None and int(_v2h.params.get("n_hold_value", 0)) == 50,
          "n_hold_value is 50 and run_value_backtest.py reads it from here — "
          "a hardcoded 20 would measure a specification nobody declared")
    check("v27-budget-unchanged",
          r27.k_budget == 5 and r27.terminal_date == "2027-03-31",
          "and v27 still reads 5 and 2027-03-31 from its own file — the fix "
          "changes where the numbers come from, not what they are")
    check("explicit-argument-still-wins",
          referee.Referee(registry_path=v29, k_budget=9).k_budget == 9,
          "an explicit argument overrides the file, which overrides the constants")
    print(f"\n{r29.status()}")


def test_spec3_declared():
    print("\n--- referee: spec #3 DECLARED 09-05, UNREAD, prior still owed ---")
    p = Path("data/registry/specifications.json")
    r = referee.Referee(registry_path=p)
    s3 = r.specs.get("pead_8k_car_v3")
    check("spec3-present", s3 is not None, "pead_8k_car_v3 is in the live registry")
    # ⚠️ TRIPWIRES FLIPPED 2026-11-10, when pead_8k_car_v3 was read. These
    # three asserted it was declared-and-unread and went red the moment that
    # stopped being true. They now pin the post-read state, which is the
    # stronger claim: v27 has spent three of five, and a re-read is refused.
    check("spec3-read-and-frozen",
          s3 is not None and s3.declared_at == "2026-09-05" and s3.is_read
          and s3.read_at == "2026-11-10",
          "declared 2026-09-05, read 2026-11-10 — the declaration preceded "
          "the read, which is the whole point")
    check("spec3-k-counted", 3 <= r.k_used() <= r.k_budget,
          f"K {r.k_used()}/{r.k_budget} — declaring spent nothing; the reads "
          f"did, and spec #3's is the third. Lower bound, not an equality: a "
          f"fourth read must not turn a check about spec #3 red")
    check("spec3-reread-is-refused",
          not r.authorize_read("pead_8k_car_v3", "2026-12-15").allowed,
          "the Referee REFUSES a second read — anti-deferral, and no bug found "
          "afterwards reopens it")
    check("spec3-verdict-recorded",
          str((s3.result if s3 is not None else {})
              .get("read", {}).get("verdict", "")) == "NOT MET",
          "the verdict is in the registry, not only in a run log that ages out")
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
    # ⚠️ TRIPWIRE FLIPPED 2026-11-08, when the operator signed the prior. The
    # old assertion ("NOT YET RECORDED" is present) existed to go red the
    # moment a prior was written, so the check could be replaced with a real
    # one. This is that real one: a prior must be present, signed, dated, and
    # NOT the placeholder. The Referee does not test this field at all
    # (qt/referee.py:161), so this suite is the only thing that does.
    _prior3 = str(_res.get("prior", ""))
    check("spec3-prior-signed",
          "NOT YET RECORDED" not in _prior3
          and "signed Southpaw3234" in _prior3
          and "%" in _prior3,
          "the prior is RECORDED, signed and dated — and it is the operator's "
          "record, not the Referee's, that makes a read legitimate")
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


# ═══════════════════════════════ the pre-read bundle keeps its own order
#
# The pre-read steps (sign the prior, land the input guard, record the
# fingerprint and the availability rehearsal) are now bundled into ONE pull
# request instead of one each. The bundling is a convenience; the ORDER is
# not. Everything the pre-registration claims rests on the prior having been
# fixed before anyone saw how the data would behave.
#
# Until now that order was proved by git: separate PRs squash-merged to
# separate commits with separate timestamps, and spec #2's prior literally
# cites "see git timestamps" as its evidence. Bundle four steps into one
# squash-merged PR and that evidence is DESTROYED -- squash collapses the
# intermediate commits, and a reviewer sees one commit that could have been
# written in any order, including after the numbers were known.
#
# So the bundle carries its own order, in the registry, as data:
#
#     prior_signed_at  <=  inputs_recorded_at  <=  read_at
#
# This is the stronger arrangement, not merely a replacement. Dates in the
# registry are reviewable in the diff, survive any merge strategy, and do not
# depend on a reader reconstructing history from commit metadata.
#
# FORWARD-ONLY. The four specs read before 2026-11-10 state their dates in
# prose _note fields and are recorded as such; this does not retrofit them,
# because editing a read specification is exactly what the protocol forbids.
PRE_READ_DATES_REQUIRED_FROM = "2026-11-11"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _first_date(blob) -> str | None:
    """The first ISO date appearing anywhere in a value, or None."""
    m = _DATE.search(json.dumps(blob) if not isinstance(blob, str) else blob)
    return m.group(0) if m else None


def test_pre_read_ordering():
    print("\n--- the pre-read bundle: prior <= inputs <= read ---")
    for path in ("data/registry/specifications.json",
                 "data/registry/v29_specifications.json"):
        p = Path(path)
        if not p.exists():
            continue
        reg = json.loads(p.read_text(encoding="utf-8"))
        for sid, spec in (reg.get("specifications") or {}).items():
            declared = spec.get("declared_at") or ""
            res = spec.get("result") or {}
            legacy = declared < PRE_READ_DATES_REQUIRED_FROM

            signed = res.get("prior_signed_at") or _first_date(res.get("prior"))
            fp = spec.get("inputs_fingerprint") or {}
            recorded = fp.get("recorded_at") or _first_date(fp) if fp else None
            read_at = spec.get("read_at") or ""

            if legacy:
                # Stated, not asserted: these predate the rule and their dates
                # live in prose. Naming them keeps the exemption visible rather
                # than letting it look like coverage.
                check(f"prereg-legacy-{sid}", True,
                      f"declared {declared or '?'} — predates "
                      f"{PRE_READ_DATES_REQUIRED_FROM}, dates in prose "
                      f"(prior {signed or 'n/a'}, inputs {recorded or 'n/a'}, "
                      f"read {read_at or 'unread'}). Not retrofitted: editing a "
                      f"read specification is what the protocol forbids")
                continue

            check(f"prereg-prior-dated-{sid}", bool(signed),
                  f"prior_signed_at is RECORDED ({signed}) — a bundled PR "
                  f"squashes to one commit, so git no longer proves the prior "
                  f"came first; the registry has to"
                  if signed else
                  "no prior_signed_at: the prior's date must be data in the "
                  "registry, because a squash-merged bundle destroys the commit "
                  "order that used to prove it")
            if signed and recorded:
                check(f"prereg-prior-before-inputs-{sid}", signed <= recorded,
                      f"prior {signed} <= inputs {recorded} — the prior was "
                      f"fixed before the input fingerprint existed")
            if signed and read_at:
                check(f"prereg-prior-before-read-{sid}", signed <= read_at,
                      f"prior {signed} <= read {read_at}")
            if recorded and read_at:
                check(f"prereg-inputs-before-read-{sid}", recorded <= read_at,
                      f"inputs {recorded} <= read {read_at} — the read refuses "
                      f"without a fingerprint recorded first")


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
    test_v29_registry_budget()
    test_spec3_declared()
    test_pre_read_ordering()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Measurement reproduces the frozen E1 read exactly; the Referee")
    print("refuses undeclared, re-read, over-budget and post-terminal reads.")


if __name__ == "__main__":
    main()
