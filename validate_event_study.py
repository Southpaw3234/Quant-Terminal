#!/usr/bin/env python3
"""Validate the event-study engine against synthetic data with KNOWN answers.

Why this exists
---------------
v25's central lesson was that almost every real failure was a MEASUREMENT
failure, not a modelling one: a scorer that silently died, a rank-IC series
that ranked on a column flattened to 0.50 for 97.5% of rows, an analyzer
that rewrote three-week-old history off fresh downloads. In every case the
instrument was reporting confidently and wrongly, and nobody could tell
because there was no case with a known answer to check it against.

So before `event_study.py` is pointed at a single real Form 4, it has to
recover effects that were planted deliberately, and report NOTHING when
nothing was planted. Seven tests, all hermetic (no network), all
deterministic (fixed seed).

  1  null-exact       identical series      -> abnormal EXACTLY 0.0
  2  null-stochastic  no planted effect     -> |t| small, E1 does NOT pass
  3  signal recovery  +2% planted           -> recovered, E1 PASSES
  4  look-ahead       jump AFTER the event  -> must be MISSED, not captured
  5  independence     overlapping events    -> deduplicated
  6  settlement       exit on newest bar    -> withheld
  7  freeze           prices change         -> written rows do NOT move

Test 4 is the one that matters most. It is the mirror image of the bug that
cost v25 fifty prediction days (fix 5e96366), and it is constructed so that
a look-ahead is not merely detected but LOUD: the buggy answer is +50% and
the correct answer is 0%.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 20260901
HORIZON = 21
FAILURES: list[str] = []


# ------------------------------------------------------------- utilities

def _bdays(n: int, start: str = "2025-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


def _write_prices(path: Path, series: dict) -> None:
    rows = []
    for tk, s in series.items():
        for d, p in s.items():
            rows.append({"date": d.date().isoformat(), "ticker": tk,
                         "close": float(p)})
    pd.DataFrame(rows).to_csv(path, index=False)


def _write_events(path: Path, evs: list) -> None:
    pd.DataFrame(evs).to_csv(path, index=False)


def _run(events: Path, prices: Path, out: Path, **env_extra) -> str:
    env = dict(os.environ)
    env.update({
        "QT_EVENTS_CSV": str(events),
        "QT_EVENT_PRICE_CSV": str(prices),
        "QT_EVENT_OUT": str(out),
        "QT_EVENT_HORIZON": str(HORIZON),
        "QT_EVENT_BENCH": "SPY",
    })
    env.update({k: str(v) for k, v in env_extra.items()})
    r = subprocess.run([sys.executable, "event_study.py"], env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"event_study.py exited {r.returncode}")
    return r.stdout


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)


# ------------------------------------------------------------ test 1 & 2

def test_null(tmp: Path) -> None:
    print("\n--- 1/2. NULL: no planted effect ---")
    rng = np.random.default_rng(SEED)
    idx = _bdays(200)
    n_ev = 45

    # 1. EXACT null: every event ticker is a verbatim copy of the benchmark,
    #    so the abnormal return must be exactly 0.0 -- no tolerance, no noise.
    bench = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx))), index=idx)
    series = {"SPY": bench}
    evs = []
    for i in range(n_ev):
        tk = f"NUL{i:02d}"
        series[tk] = bench.copy()
        evs.append({"event_id": f"nx{i:02d}", "ticker": tk,
                    "event_ts": idx[40 + i].date().isoformat(),
                    "event_type": "null_exact"})
    ev_csv, px_csv = tmp / "e1.csv", tmp / "p1.csv"
    out = tmp / "o1.csv"
    _write_events(ev_csv, evs)
    _write_prices(px_csv, series)
    _run(ev_csv, px_csv, out)
    got = pd.read_csv(out)
    worst = got["abnormal_ret"].abs().max()
    check("null-exact", len(got) == n_ev and worst == 0.0,
          f"n={len(got)} max|abnormal|={worst}")

    # 2. STOCHASTIC null: independent noise, no planted drift. Bar is |t| < 4
    #    -- loose enough never to flake (p ~ 6e-5), tight enough to catch a
    #    systematically biased engine, which is the realistic failure.
    series = {"SPY": bench}
    evs = []
    for i in range(n_ev):
        tk = f"RND{i:02d}"
        series[tk] = pd.Series(
            100 * np.cumprod(1 + rng.normal(0, 0.02, len(idx))), index=idx)
        evs.append({"event_id": f"rx{i:02d}", "ticker": tk,
                    "event_ts": idx[40 + i].date().isoformat(),
                    "event_type": "null_random"})
    ev_csv, px_csv = tmp / "e2.csv", tmp / "p2.csv"
    out = tmp / "o2.csv"
    _write_events(ev_csv, evs)
    _write_prices(px_csv, series)
    stdout = _run(ev_csv, px_csv, out)
    got = pd.read_csv(out)
    x = got["abnormal_ret"].values
    t = float(np.mean(x) / (np.std(x, ddof=1) / np.sqrt(len(x))))
    check("null-stochastic", abs(t) < 4.0, f"n={len(x)} t={t:+.2f} (bar |t|<4)")
    check("null-verdict", "E1 verdict     : NOT MET" in stdout,
          "engine reports NOT MET on a null")


# ---------------------------------------------------------------- test 3

def test_signal(tmp: Path) -> None:
    print("\n--- 3. SIGNAL: +2% planted over the horizon ---")
    rng = np.random.default_rng(SEED + 1)
    idx = _bdays(220)
    n_ev = 45
    bench = pd.Series(100.0, index=idx)          # flat benchmark
    series = {"SPY": bench}
    evs = []
    for i in range(n_ev):
        tk = f"SIG{i:02d}"
        ev_i = 40 + i
        p = np.full(len(idx), 100.0)
        # drift begins at the ENTRY bar (ev_i+1) and accrues over the horizon
        bump = 0.02 + rng.normal(0, 0.005)
        p[ev_i + 1:] = 100.0
        p[ev_i + 1 + HORIZON:] = 100.0 * (1 + bump)
        # ramp linearly across the window so the exit bar carries the effect
        for k in range(1, HORIZON + 1):
            p[ev_i + 1 + k] = 100.0 * (1 + bump * k / HORIZON)
        series[tk] = pd.Series(p, index=idx)
        evs.append({"event_id": f"sx{i:02d}", "ticker": tk,
                    "event_ts": idx[ev_i].date().isoformat(),
                    "event_type": "planted"})
    ev_csv, px_csv, out = tmp / "e3.csv", tmp / "p3.csv", tmp / "o3.csv"
    _write_events(ev_csv, evs)
    _write_prices(px_csv, series)
    stdout = _run(ev_csv, px_csv, out)
    got = pd.read_csv(out)
    m = float(got["abnormal_ret"].mean())
    check("signal-recovered", 0.015 < m < 0.025,
          f"mean abnormal={m:+.4f} (planted +0.0200)")
    check("signal-verdict", "E1 verdict     : PASS" in stdout,
          "engine reports PASS when the effect is real")


# ---------------------------------------------------------------- test 4

def test_lookahead(tmp: Path) -> None:
    print("\n--- 4. LOOK-AHEAD GUARD (the one that matters) ---")
    idx = _bdays(120)
    ev_i = 50
    # Flat at 100 through the event bar, then a +50% jump on the NEXT bar.
    # Correct behaviour enters at that next bar's close and therefore MISSES
    # the jump entirely -> 0% return. An engine that enters on the event bar
    # captures the whole +50%.
    p = np.full(len(idx), 100.0)
    p[ev_i + 1:] = 150.0
    series = {"SPY": pd.Series(100.0, index=idx),
              "JUMP": pd.Series(p, index=idx)}
    evs = [{"event_id": "la01", "ticker": "JUMP",
            "event_ts": idx[ev_i].date().isoformat(),
            "event_type": "lookahead_probe"}]
    ev_csv, px_csv, out = tmp / "e4.csv", tmp / "p4.csv", tmp / "o4.csv"
    _write_events(ev_csv, evs)
    _write_prices(px_csv, series)
    _run(ev_csv, px_csv, out)
    got = pd.read_csv(out)
    r = float(got["abnormal_ret"].iloc[0])
    entry = str(got["entry_date"].iloc[0])
    expect_entry = idx[ev_i + 1].date().isoformat()
    check("lookahead-return", abs(r) < 1e-9,
          f"abnormal={r:+.4f} (correct 0.0000; a look-ahead reads +0.5000)")
    check("lookahead-entry", entry == expect_entry,
          f"entry={entry} == first bar after event ({expect_entry})")


# ---------------------------------------------------------------- test 5

def test_independence(tmp: Path) -> None:
    print("\n--- 5. INDEPENDENCE FILTER ---")
    idx = _bdays(160)
    series = {"SPY": pd.Series(100.0, index=idx),
              "DUP": pd.Series(100.0, index=idx)}
    # three events on one ticker, 3 bars apart, horizon 21 -> all overlap
    evs = [{"event_id": f"dp{k}", "ticker": "DUP",
            "event_ts": idx[50 + 3 * k].date().isoformat(),
            "event_type": "overlap"} for k in range(3)]
    ev_csv, px_csv, out = tmp / "e5.csv", tmp / "p5.csv", tmp / "o5.csv"
    _write_events(ev_csv, evs)
    _write_prices(px_csv, series)
    _run(ev_csv, px_csv, out)
    got = pd.read_csv(out)
    check("independence", len(got) == 1,
          f"3 overlapping events -> {len(got)} kept (expect 1)")


# ---------------------------------------------------------------- test 6

def test_settlement(tmp: Path) -> None:
    print("\n--- 6. SETTLEMENT (unsettled exit bar withheld) ---")
    idx = _bdays(80)
    series = {"SPY": pd.Series(100.0, index=idx),
              "SETL": pd.Series(100.0, index=idx)}
    last = len(idx) - 1
    evs = [
        # exit lands exactly on the newest bar -> NOT settled, must be withheld
        {"event_id": "st_unsettled", "ticker": "SETL",
         "event_ts": idx[last - HORIZON - 1].date().isoformat(),
         "event_type": "settle_probe"},
    ]
    ev_csv, px_csv, out = tmp / "e6.csv", tmp / "p6.csv", tmp / "o6.csv"
    _write_events(ev_csv, evs)
    _write_prices(px_csv, series)
    _run(ev_csv, px_csv, out)
    withheld = not out.exists() or len(pd.read_csv(out)) == 0
    check("settlement", withheld,
          "event whose exit bar is the newest bar was withheld")


# ---------------------------------------------------------------- test 7

def test_freeze(tmp: Path) -> None:
    print("\n--- 7. FREEZE (written rows never move) ---")
    idx = _bdays(160)
    rng = np.random.default_rng(SEED + 2)
    series = {"SPY": pd.Series(100.0, index=idx)}
    evs = []
    for i in range(10):
        tk = f"FRZ{i:02d}"
        series[tk] = pd.Series(
            100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx))), index=idx)
        evs.append({"event_id": f"fz{i:02d}", "ticker": tk,
                    "event_ts": idx[40 + i].date().isoformat(),
                    "event_type": "freeze_probe"})
    ev_csv, px_csv, out = tmp / "e7.csv", tmp / "p7.csv", tmp / "o7.csv"
    _write_events(ev_csv, evs)
    _write_prices(px_csv, series)
    _run(ev_csv, px_csv, out)
    first = pd.read_csv(out).set_index("event_id")["abnormal_ret"].to_dict()

    # Now materially change the prices and re-run against the same ledger.
    # This is exactly what bit v25: a full-overwrite analyzer recomputing
    # history from a fresh download.
    for tk in list(series):
        if tk != "SPY":
            series[tk] = series[tk] * 1.25
    _write_prices(px_csv, series)
    stdout = _run(ev_csv, px_csv, out)
    second = pd.read_csv(out).set_index("event_id")["abnormal_ret"].to_dict()
    moved = [k for k in first if abs(first[k] - second.get(k, 1e9)) > 1e-12]
    check("freeze", not moved,
          f"{len(first)} written rows, {len(moved)} moved after a price change")
    check("freeze-loud", "FROZE" in stdout,
          "the freeze announced itself rather than silently discarding drift")


def main() -> None:
    print("=" * 68)
    print("event_study.py — validation against synthetic data with KNOWN answers")
    print("=" * 68)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_null(tmp)
        test_signal(tmp)
        test_lookahead(tmp)
        test_independence(tmp)
        test_settlement(tmp)
        test_freeze(tmp)

    print("\n" + "=" * 68)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("The instrument recovers planted effects, reports nothing on a null,")
    print("cannot look ahead, deduplicates overlaps, withholds unsettled bars,")
    print("and never moves a written row.")


if __name__ == "__main__":
    main()
