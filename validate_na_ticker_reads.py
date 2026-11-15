#!/usr/bin/env python3
"""Pure-logic validation for ONE DEFECT across TWO READERS — no network.

`NA` is a real listed company (Nano Labs). pandas.read_csv converts the
string "NA" to NaN by default, so

    panel = pd.read_csv("data/universe/v29_universe_pit.csv")

deletes 16 of that panel's 152,006 name-months before any logic sees them.
run_value_backtest.py's ticker filter then reported the loss as "16 panel rows
dropped on an unusable ticker" — which is the line the V29 registry records as
a property of the data. THE TICKER WAS NEVER UNUSABLE. A CSV parsing default
ate it, and the symptom was written down as a finding.

extract_8k_deficiency.load_panel was fixed in 313f455, and that commit flagged
the other two readers of the same file without touching them:

    run_value_backtest.py:231     panel = pd.read_csv(PANEL)
    extract_fundamentals.py:374   panel = pd.read_csv(PANEL)

Both are fixed now and tested here, together, because it is one defect and not
two. Each test DEMONSTRATES the bug before asserting the fix: it performs the
ordinary read, counts what that read loses, and only then checks the loader. A
regression test that asserted the fix alone would still pass if the default
ever changed, and would not say what it was protecting against.

⚠️ WHAT THIS DOES NOT CLAIM. value_ebit_ev_v1 (t 0.71) and value_ebit_ev_v2
(t 0.53) were both READ on a panel missing this one name, both are NOT MET,
and anti-deferral (CLAUDE.md §3 rule 3) means a bug found afterwards does not
reopen a spent read. 16 of 152,006 name-months cannot move either t to 2.0.
Those reads stand. This suite protects FUTURE reads; it re-runs nothing and
nothing under data/ is regenerated or rewritten anywhere in this file.

The second half is the general case. "NAN", "NULL", "NONE" and "N/A" are also
strings a venue could assign, and which of them a given pandas swallows is a
version detail — so the demonstration REPORTS the set it loses rather than
asserting a count, while the loaders are required to keep all of them. With
nothing treated as missing, an empty numeric cell arrives as "" rather than
NaN, so the explicit coercion is tested too: getting the fix wrong in that
direction would hand the scorer a string where it expects a number.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

import extract_fundamentals as ef
import run_value_backtest as rv

FAILURES = []

PANEL = Path("data/universe/v29_universe_pit.csv")
REGISTRY = Path("data/registry/v29_specifications.json")

# Tickers pandas may treat as missing. Every one is a string a listing venue
# could legitimately assign, and NA is one that was.
POISON = ["NA", "NAN", "NULL", "NONE", "N/A"]


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


def _ordinary_read(path) -> "pd.DataFrame":
    """What the plain call does. The bug, performed rather than described."""
    return pd.read_csv(path, usecols=["ticker"])


# ═══════════════════════════════════════════ 1. the frozen V29 panel


def test_backtest_driver_keeps_ticker_na():
    print("\n--- 🔑 run_value_backtest reads 'NA' as a company ---")
    if not PANEL.exists():
        check("panel-present", False, f"{PANEL} missing")
        return

    naive = _ordinary_read(PANEL)
    lost = int(naive["ticker"].isna().sum())
    check("ordinary-read-loses-NA", lost > 0,
          f"a default read_csv turns {lost} of this panel's tickers into NaN. "
          f"That is the '16 panel rows dropped on an unusable ticker' the V29 "
          f"registry records — not malformed identifiers, one real company")

    df = rv.load_declared_csv(str(PANEL), rv.PANEL_NUMERIC)
    check("loader-keeps-ticker-NA", "NA" in set(df["ticker"]),
          "load_declared_csv keeps NA (Nano Labs) as a string, so the ticker "
          "filter downstream can no longer report it as unusable")
    check("loader-keeps-every-row", len(df) == len(naive),
          f"and it keeps all {len(df):,} rows: nothing is treated as missing, "
          f"so nothing is dropped except a genuinely empty ticker")
    check("loader-keeps-no-blank-ticker",
          bool((df["ticker"].str.len() > 0).all()),
          "every ticker it returns is a non-empty string, so a later sorted() "
          "cannot die comparing a float to a str (run 34050877302)")
    check("loader-coerces-panel-numerics",
          str(df["adv"].dtype) != "object",
          f"and `adv` comes back numeric ({df['adv'].dtype}), not as the "
          f"strings keep_default_na=False would otherwise leave behind — the "
          f"liquidity screen multiplies it")


def test_fundamentals_extractor_keeps_ticker_na():
    print("\n--- 🔑 extract_fundamentals sweeps 'NA' instead of skipping it ---")
    if not PANEL.exists():
        check("panel-present-fundamentals", False, f"{PANEL} missing")
        return

    naive = _ordinary_read(PANEL)
    lost = int(naive["ticker"].isna().sum())
    would_sweep = sorted({str(t).upper() for t in naive["ticker"].dropna()})
    check("ordinary-read-loses-NA-here-too", lost > 0 and "NA" not in would_sweep,
          f"the extractor's old read lost the same {lost} rows, and 'NA' is "
          f"absent from the {len(would_sweep)} names it would have asked SEC "
          f"XBRL about. A name never swept is a name the value model cannot "
          f"score, which is why fundamentals_pit.csv and fundamentals_ttm.csv "
          f"contain no NA row to find")

    names = sorted(set(ef.load_panel(str(PANEL))["ticker"]))
    check("fundamentals-loader-keeps-ticker-NA", "NA" in names,
          f"load_panel keeps NA, so a future sweep covers {len(names)} names "
          f"including it")
    check("fundamentals-loader-returns-strings",
          all(isinstance(t, str) and t.strip() for t in names),
          "and every name it returns is a non-empty string")


# ═══════════════════════════════════════════ 2. the general case


def _synthetic(tmp: Path) -> Path:
    """A panel-shaped file of tickers pandas may treat as missing, plus holes.

    The empty `adv` cell is the other half of the fix: with nothing treated as
    missing, that cell arrives as "" and has to be coerced back deliberately.
    The blank ticker is the row that SHOULD be dropped.
    """
    path = tmp / "poison.csv"
    rows = ["date,ticker,price,adv,n_bars"]
    for i, tk in enumerate(POISON):
        adv = "" if tk == "NULL" else "50000"
        rows.append(f"2026-01-30,{tk},1{i}.0,{adv},21")
    rows.append("2026-01-30, ,1.0,1000,21")       # a genuinely empty ticker
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_every_poison_ticker_survives():
    print("\n--- the general case: NAN, NULL, NONE and N/A are tickers too ---")
    with tempfile.TemporaryDirectory() as td:
        path = _synthetic(Path(td))

        naive = _ordinary_read(path)
        survived = {str(t) for t in naive["ticker"].dropna()}
        swallowed = [tk for tk in POISON if tk not in survived]
        check("ordinary-read-loses-NA-and-friends", "NA" in swallowed,
              f"a default read_csv swallows {swallowed} out of {POISON} on "
              f"this pandas. WHICH ones is a version detail; that NA is among "
              f"them is the defect, and the loaders below must keep all five "
              f"either way")

        df = rv.load_declared_csv(str(path), rv.PANEL_NUMERIC)
        kept = set(df["ticker"])
        check("backtest-loader-keeps-all-five",
              all(tk in kept for tk in POISON),
              f"load_declared_csv keeps every one of them: {sorted(kept)}")
        check("backtest-loader-drops-the-truly-empty",
              len(df) == len(POISON),
              "and drops the one row whose ticker is genuinely blank — "
              "emptiness is tested explicitly, never inferred from a spelling")
        check("backtest-loader-restores-the-numeric-hole",
              bool(pd.isna(df.loc[df["ticker"] == "NULL", "adv"]).all())
              and float(df.loc[df["ticker"] == "NA", "adv"].iloc[0]) == 50000.0,
              "the empty adv cell comes back as NaN and the populated ones as "
              "numbers: 'nothing is missing' must not mean 'every number is "
              "now a string'")

        pf = ef.load_panel(str(path))
        check("fundamentals-loader-keeps-all-five",
              all(tk in set(pf["ticker"]) for tk in POISON)
              and len(pf) == len(POISON),
              "and extract_fundamentals.load_panel behaves identically — one "
              "defect, one fix, two readers")


def test_whitespace_and_case():
    print("\n--- the loaders normalise, so 'na' and ' NA ' are one name ---")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "messy.csv"
        path.write_text(
            "date,ticker,price,adv,n_bars\n"
            "2026-01-30, na ,10.0,50000,21\n"
            "2026-01-30,nano,11.0,50000,21\n", encoding="utf-8")
        for label, got in (("backtest",
                            rv.load_declared_csv(str(path), rv.PANEL_NUMERIC)),
                           ("fundamentals", ef.load_panel(str(path)))):
            check(f"{label}-strips-and-uppers",
                  list(got["ticker"]) == ["NA", "NANO"],
                  "stripped and uppercased at read time, once, on the frame — "
                  "coercing at each use is how a mismatched key goes unnoticed")


# ═══════════════════════════════════════════ 3. the spent reads stay spent


def test_the_frozen_reads_are_untouched():
    print("\n--- ⚠️ the fix changes no measured result ---")
    if not REGISTRY.exists():
        check("v29-registry-present", False, f"{REGISTRY} missing")
        return
    blob = json.loads(REGISTRY.read_text(encoding="utf-8"))
    specs = blob.get("specifications", {})
    for sid in ("value_ebit_ev_v1", "value_ebit_ev_v2"):
        s = specs.get(sid, {})
        check(f"{sid}-still-read-and-not-met",
              bool(s.get("read_at"))
              and str(s.get("result", {}).get("read", {})
                      .get("verdict", "")) == "NOT MET",
              f"{sid} was read {s.get('read_at')} and is NOT MET. This suite "
              f"does not re-run it, re-score it or touch its result: "
              f"anti-deferral means a bug found afterwards does not reopen a "
              f"spent read, and 16 of 152,006 name-months cannot move a t of "
              f"0.71 or 0.53 to 2.0")
    note = " ".join(str(x) for x in blob.get("_note", []))
    check("defect-recorded-at-programme-level",
          "Nano Labs" in note,
          "and the defect is written down at programme level in the registry "
          "itself — next to the reads it qualifies, not only in a commit "
          "message nobody reads twice")


def main():
    print("=" * 70)
    print("the ticker 'NA' — one parsing default, two readers (no network)")
    print("=" * 70)
    test_backtest_driver_keeps_ticker_na()
    test_fundamentals_extractor_keeps_ticker_na()
    test_every_poison_ticker_survives()
    test_whitespace_and_case()
    test_the_frozen_reads_are_untouched()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("A default read_csv still loses the ticker 'NA'; neither of these")
    print("two readers does any more. The frozen V29 reads were taken on a")
    print("panel missing that name, they are NOT MET, and they STAND — this")
    print("protects the next read, not the two already spent.")


if __name__ == "__main__":
    main()
