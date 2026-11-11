#!/usr/bin/env python3
"""Pure-logic validation for the tax-loss book extractor. No network.

The question this suite exists to answer is not "does the code run". It is:
**could the selection have seen the future?** Every other defect in a
selection-only extractor is recoverable; that one silently manufactures a
result and cannot be detected afterwards from the output.

So the look-ahead checks are structural -- signatures and imports -- rather
than behavioural. A behavioural test proves the code did not look ahead on
the inputs it was given. A signature test proves it could not have.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import sys

import pandas as pd

import extract_tax_loss_book as ex

# The repo's prose uses section marks and box-drawing freely, and this file
# prints it. A Windows console defaults to cp1252 and raises on the first
# such character -- which would make a suite that PASSED look like a crash.
# The stream is fixed rather than the prose.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))



def _identifiers(fn) -> set:
    """Every identifier a function's code binds or reads, from its AST.

    Docstrings and comments are NOT identifiers and are deliberately not
    returned: a guard that greps raw source cannot distinguish a sentence
    saying "no forward return is in scope" from a variable called
    forward_return, and will fail on the comment that documents it.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.keyword) and n.arg:
            out.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
    return out


SESSIONS = pd.DatetimeIndex(sorted(set(
    pd.bdate_range("2014-06-02", "2026-06-30"))))


# ── the look-ahead guard, structurally ───────────────────────────────────────

def test_selection_cannot_see_the_future():
    print("\nselection is STRUCTURALLY blind to anything after formation")

    sig = list(inspect.signature(ex.select_cycle).parameters)
    check("select-cycle-signature",
          sig == ["eligible", "ytd_by_ticker", "quantile"],
          f"{sig} — no exit date, no exit price, no forward return and no "
          f"outcome is in scope, so none can leak into the choice however "
          f"this function is later edited")

    # Over IDENTIFIERS from the AST, not words in the source. The functions
    # explain in their own docstrings that they hold no forward return and
    # no outcome, and a substring search cannot tell that sentence from a
    # variable -- the same confusion that has now bitten three separate
    # guards in this repo (the banned-host check and the Referee check being
    # the others). What makes an outcome unreachable is the absence of a
    # NAME bound to one.
    names = _identifiers(ex.select_cycle) | _identifiers(ex.ytd_returns)
    for banned in ("exit_ts", "exit_price", "forward", "future", "outcome",
                   "pnl", "realised", "realized"):
        hit = sorted(n for n in names if banned in n.lower())
        check(f"selection-binds-no-{banned}", not hit,
              f"no identifier in the selection path names one"
              + (f" — found {hit}" if hit else ""))

    ysig = list(inspect.signature(ex.ytd_returns).parameters)
    check("ytd-signature",
          ysig == ["closes", "base_ts", "formation_ts", "eligible"],
          f"{ysig} — both price anchors are at or before formation")

    tree = ast.parse(inspect.getsource(ex))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported.add(a.name)
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                imported.add(n.module)
                for a in n.names:
                    imported.add(f"{n.module}.{a.name}")
    for m in ("qt.referee", "qt.wrc", "qt.calendar_time"):
        check(f"does-not-import-{m}", m not in imported,
              "a selection file must not be able to spend a read or "
              "evaluate a criterion")


def test_dates_come_only_from_the_calendar():
    print("\nentry and exit dates are calendar-derived, never name-derived")

    csig = list(inspect.signature(ex.cycle_dates).parameters)
    check("cycle-dates-signature", csig == ["sessions", "year"],
          f"{csig} — no ticker, no price, no book can move either date")

    f, x = ex.cycle_dates(SESSIONS, 2015)
    check("formation-on-or-after-15-dec",
          f is not None and f >= pd.Timestamp("2015-12-15"), str(f))
    check("exit-on-or-after-31-jan",
          x is not None and x >= pd.Timestamp("2016-01-31"), str(x))
    check("exit-after-formation", x > f)

    # 15 December 2019 is a SUNDAY. The anchor must roll forward to a real
    # session, not silently land on a date the market was shut.
    f19, _ = ex.cycle_dates(SESSIONS, 2019)
    check("weekend-anchor-rolls-forward",
          f19 == pd.Timestamp("2019-12-16"),
          f"{f19} — 15 Dec 2019 was a Sunday")

    base = ex.prior_year_end_session(SESSIONS, 2016)
    check("ytd-base-is-prior-year-end",
          base is not None and base.year == 2015 and base.month == 12,
          f"{base} — the tax year is the calendar year, so the base is the "
          f"last session of the PRIOR year")
    check("base-before-formation", base < ex.cycle_dates(SESSIONS, 2016)[0])

    # Running off the end of the price history must yield nothing rather
    # than a half-formed cycle.
    short = pd.DatetimeIndex(sorted(set(pd.bdate_range("2015-01-01", "2015-12-20"))))
    fs, xs = ex.cycle_dates(short, 2015)
    check("no-exit-session-is-no-cycle", fs is None or xs is None,
          "a cycle whose exit is past the data is not emitted")


# ── the quantile rule ────────────────────────────────────────────────────────

def test_select_cycle():
    print("\nbottom-quintile selection")
    elig = frozenset(f"T{i:02d}" for i in range(20))
    ytd = {f"T{i:02d}": (i - 10) / 10.0 for i in range(20)}   # -1.0 .. +0.9

    got = ex.select_cycle(elig, ytd)
    check("takes-bottom-quintile", len(got) == 4, f"{len(got)} of 20")
    check("takes-the-WORST", got == ["T00", "T01", "T02", "T03"],
          f"{got} — most negative YTD, which is where loss realisation bites")

    check("ignores-ineligible",
          ex.select_cycle(frozenset({"T19"}), ytd) == ["T19"],
          "a name outside the universe cannot be selected however bad its "
          "return")

    check("nan-dropped-not-ranked",
          "BAD" not in ex.select_cycle(elig | {"BAD"},
                                       dict(ytd, BAD=float("nan"))),
          "a NaN return must not sort to the bottom and be bought")

    check("empty-input-empty-book", ex.select_cycle(frozenset(), {}) == [])
    check("always-at-least-one",
          len(ex.select_cycle(frozenset({"A"}), {"A": -0.5})) == 1,
          "rounding must not produce a zero-size book from a non-empty pool")

    # 🔑 Ties. Slicing to an exact count would make the book depend on sort
    # order, and sort order is not a declared parameter.
    tied = {f"T{i:02d}": -0.5 for i in range(10)}
    got = ex.select_cycle(frozenset(tied), tied)
    check("ties-take-everyone-at-the-cutoff", len(got) == 10,
          f"{len(got)} of 10 — all tied at the cutoff value, so all are "
          f"taken rather than an arbitrary two")

    # A wider cut DILUTES, which is the conservative direction. Asserted so
    # that narrowing the quantile later is a visible change, not a tweak.
    check("quintile-not-decile", abs(ex.QUANTILE - 0.20) < 1e-9,
          f"{ex.QUANTILE} — the wider cut biases the read AGAINST a pass")


def test_ytd_returns():
    print("\nyear-to-date return, computable on the formation day")
    idx = pd.DatetimeIndex(["2015-12-31", "2016-06-30", "2016-12-15",
                            "2017-01-31"])
    closes = {
        "DOWN": pd.Series([100.0, 70.0, 50.0, 80.0], index=idx),
        "FLAT": pd.Series([100.0, 100.0, 100.0, 10.0], index=idx),
        "NOBASE": pd.Series([50.0, 40.0], index=idx[2:]),
        "ZERO": pd.Series([0.0, 1.0, 2.0, 3.0], index=idx),
    }
    base, form = pd.Timestamp("2015-12-31"), pd.Timestamp("2016-12-15")
    got = ex.ytd_returns(closes, base, form,
                         frozenset({"DOWN", "FLAT", "NOBASE", "ZERO"}))

    check("down-name-negative", abs(got["DOWN"] - (-0.5)) < 1e-9,
          f"{got['DOWN']} — 100 -> 50")
    check("flat-name-zero", abs(got["FLAT"]) < 1e-9)
    check("missing-base-dropped", "NOBASE" not in got,
          "a missing base price defaulted to zero would rank the name as "
          "unchanged and keep it OUT of the bottom quintile")
    check("zero-base-dropped", "ZERO" not in got, "no division by zero")

    # 🔑 The look-ahead test with teeth. FLAT collapses 90% AFTER formation.
    # If that move reached the ranking, FLAT would be the worst name in the
    # book. It must not appear as anything but flat.
    check("post-formation-crash-invisible", abs(got["FLAT"]) < 1e-9,
          "FLAT falls 90% after the formation date; the ranking must not "
          "see it")
    check("crash-name-not-selected",
          "FLAT" not in ex.select_cycle(frozenset(got), got),
          "and it must not end up in the book because of it")


def test_build_keeps_thin_cycles_visible():
    print("\na thin cycle is reported, not deleted")
    idx = pd.DatetimeIndex(sorted(set(pd.bdate_range("2014-06-02", "2017-03-31"))))
    bench = pd.Series(range(1, len(idx) + 1), index=idx, dtype="float64")
    closes = {ex.BENCHMARK: bench}
    for i in range(3):                      # only 3 names: below MIN_BOOK
        closes[f"N{i}"] = pd.Series(
            [100.0 - i * 10] * len(idx), index=idx, dtype="float64")
    nov = pd.Timestamp("2015-11-30")
    by_month = {nov: frozenset({"N0", "N1", "N2"})}
    rows, report = ex.build(closes, by_month, pd.DatetimeIndex([nov]))

    c15 = next((r for r in report if r["cycle"] == 2015), None)
    check("cycle-present-in-report", c15 is not None)
    if c15:
        check("thin-cycle-flagged", c15["status"] == "below_min_book",
              f"{c15['selected']} names < MIN_BOOK {ex.MIN_BOOK} — reported, "
              f"because 'the book was too small that year' is a finding "
              f"about the mechanism, not a row to delete")
    check("report-covers-every-cycle",
          len(report) == ex.LAST_CYCLE - ex.FIRST_CYCLE + 1,
          f"{len(report)} cycles reported, none skipped silently")


def test_declared_parameters():
    print("\nthe declared parameters match the specification document")
    for name, want in (("QUANTILE", 0.20), ("MIN_BOOK", 5),
                       ("ENTRY_MONTH", 12), ("ENTRY_DAY", 15),
                       ("EXIT_MONTH", 1), ("EXIT_DAY", 31),
                       ("FIRST_CYCLE", 2015), ("LAST_CYCLE", 2024),
                       ("BENCHMARK", "IWM")):
        got = getattr(ex, name, None)
        check(f"param-{name}", got == want, f"{got}")
    check("ten-cycles", ex.LAST_CYCLE - ex.FIRST_CYCLE + 1 == 10,
          "§⑥: ten Decembers is the effective sample size, not 300 days")


def test_spec4_tax_loss_not_declared():
    print("\nthe slot is still unspent")
    import json
    from pathlib import Path
    specs = json.loads(
        Path("data/registry/specifications.json").read_text(encoding="utf-8")
    ).get("specifications", {})
    # ⚠️ TRIPWIRE. MEANT to go red the day this specification is declared.
    check("tax-loss-not-declared",
          not any(s.startswith("tax_loss") for s in specs),
          f"no tax_loss specification in the registry — the extractor "
          f"exists, the slot does not. {len(specs)} declared, and declaring "
          f"is what spends K, not this file")


def main():
    print("=" * 70)
    print("extract_tax_loss_book — pure logic (no network)")
    print("=" * 70)
    test_selection_cannot_see_the_future()
    test_dates_come_only_from_the_calendar()
    test_select_cycle()
    test_ytd_returns()
    test_build_keeps_thin_cycles_visible()
    test_declared_parameters()
    test_spec4_tax_loss_not_declared()

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, no return computed.")


if __name__ == "__main__":
    main()
