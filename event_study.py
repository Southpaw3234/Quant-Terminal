#!/usr/bin/env python3
"""Event-study engine — v27 component A2.

Measures whether a class of EVENTS is followed by abnormal returns, and
reports the result against the E1 criterion pre-registered in
`docs/V27_PREREGISTRATION.md`.

WHAT THIS IS NOT
----------------
It is not a predictor and it does not produce a score per name per day.
That was v25's shape and it is measured at walk-forward AUC ~0.50. This
consumes EVENTS — a Form 4 cluster buy, an index deletion, a short-interest
threshold crossing — and answers one question: after this happens, what does
the forward return distribution look like relative to a control?

THREE THINGS IT REFUSES TO DO, BY CONSTRUCTION
----------------------------------------------
1. LOOK AHEAD.  Entry is the first bar STRICTLY AFTER the event timestamp
   (`searchsorted(..., side="right")`).  An 8-K accepted at 16:05 ET on a
   Tuesday cannot be entered at Tuesday's close, because Tuesday's close
   already happened.  v25's most expensive bug was signals computed on bars
   5-10 sessions stale (fix 5e96366); the mirror-image error here would be
   entering on a bar the event had not yet occurred in, and it is made
   unrepresentable rather than merely tested for.

2. COUNT OVERLAPPING WINDOWS AS INDEPENDENT.  E1 requires N >= 40
   INDEPENDENT events.  Two Form 4s on the same ticker three days apart,
   with a 21-bar horizon, share 18 bars of the same price path — they are
   one observation wearing two hats, and counting both inflates the t-stat
   by roughly sqrt(2) for free.  `_dedupe_overlaps` keeps the first and
   drops any later event on the same ticker inside the horizon.

3. SCORE AN UNSETTLED BAR.  Carried over verbatim from
   `analyze_rank_ic.py`: require at least one bar AFTER the exit bar, so the
   exit is provably a completed session.  This is a STRUCTURAL test, not a
   clock test — this repo has been bitten three times by clock-based
   reasoning (8d3e9df, e7b1d5f, c225537) and a structural test cannot drift
   with timezone, market calendar or run time.

CONTROL / ABNORMAL RETURN
-------------------------
Default is market-adjusted: `abnormal = event_return - benchmark_return`
over the identical window.  For the inverted universe v27 targets (small,
illiquid, uncovered) a plain SPY adjustment leaves real beta mismatch, so
the engine also accepts a per-event `control_ticker` column and uses it when
present.  That is the seam where characteristic matching (size / sector /
momentum) plugs in later; it is deliberately NOT guessed at now, because a
matching scheme invented before the events exist is a free parameter nobody
counted against K.

OUTPUT IS APPEND-ONLY
---------------------
`_freeze_first_write` is carried over, keyed on `event_id`.  A per-event
abnormal return, once written, never moves.  v25 learned this the hard way:
a full-overwrite analyzer silently rewrote three-week-old rows off
re-downloaded prices (2026-07-16 long_short 0.05304 -> 0.04148, a 22% move),
which meant the number read on decision day was not the series that had been
accumulated.

ENV
---
  QT_EVENTS_CSV        input events   (default data/events/events.csv)
  QT_EVENT_OUT         output ledger  (default data/events/event_study.csv)
  QT_EVENT_HORIZON     holding bars   (default 63 = one quarter)
  QT_EVENT_BENCH       benchmark      (default SPY)
  QT_EVENT_PRICE_CSV   offline prices (tests; long format date,ticker,close)
  QT_EVENT_SETTLED     "0" disables the settlement test (tests only)
  QT_EVENT_MUTABLE     "1" disables the freeze (debugging only, never in CI)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

EVENTS_CSV = Path(os.environ.get("QT_EVENTS_CSV", "data/events/events.csv"))
OUT_CSV = Path(os.environ.get("QT_EVENT_OUT", "data/events/event_study.csv"))
HORIZON = int(os.environ.get("QT_EVENT_HORIZON", "63"))
# 63 bars = one quarter. Moved from 21 by the 2026-09-04 fork decision
# (docs/V27_FORK_DECISION.md): chosen by measurement, not preference --
# against the frozen 205-event set, 252 bars (one year) yields ZERO settled
# observations and 63 yields 170. A quarter is the longest horizon the
# existing event history can actually support.
BENCH = os.environ.get("QT_EVENT_BENCH", "SPY").strip().upper()
PRICE_CSV = os.environ.get("QT_EVENT_PRICE_CSV", "").strip()
SETTLED_ONLY = os.environ.get("QT_EVENT_SETTLED", "1").strip() != "0"

# ── MERGED WITH THE qt PACKAGE, 2026-09-04 ──────────────────────────────────
# The E1 bars and the summary arithmetic used to be duplicated here. They now
# come from qt.measurement, which docs/V28_AGENT_ARCHITECTURE.md §3.2 makes the
# ONLY role permitted to compute a return.
#
# 🔑 This swap is SAFE TO MAKE BECAUSE IT IS VERIFIABLE, and that is the whole
# reason Phase 1's exit gate exists. validate_qt_phase1.py asserts that
# qt.measurement.summarize reproduces the frozen E1 read EXACTLY from
# data/events/event_study.csv -- n=160, mean +1.7647%, t +1.16, stability
# -0.52. Replacing a private copy with a shared implementation is normally the
# most dangerous kind of refactor precisely because a silent shift in a verdict
# is invisible; here the shift would fail CI.
from qt import ledger as qt_ledger              # noqa: E402
from qt import measurement as qt_measurement    # noqa: E402
from qt import referee as qt_referee            # noqa: E402

E1_MIN_N = qt_measurement.E1_MIN_N
E1_MIN_EFFECT = qt_measurement.E1_MIN_EFFECT
E1_MIN_T = qt_measurement.E1_MIN_T
E1_STABILITY = qt_measurement.E1_STABILITY

# Referee gate. When QT_SPEC_ID is set, the read must be AUTHORISED before any
# return is computed -- the Referee is otherwise tested but decorative, and a
# guard nothing calls is not a guard.
SPEC_ID = os.environ.get("QT_SPEC_ID", "").strip()
# Availability mode: dedup + settlement classification, NO returns, nothing
# written. Bypasses the Referee because there is nothing to authorise.
COUNT_ONLY = os.environ.get("QT_EVENT_COUNT_ONLY", "").strip() == "1"
READ_DATE = os.environ.get("QT_READ_DATE", "").strip()

REQUIRED_COLS = {"event_id", "ticker", "event_ts", "event_type"}


# ---------------------------------------------------------------- loading

def _load_events() -> pd.DataFrame:
    if not EVENTS_CSV.exists():
        print(f"[event-study] no events file at {EVENTS_CSV} — nothing to do.")
        sys.exit(0)
    df = pd.read_csv(EVENTS_CSV)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        print(f"[event-study] FATAL: events file missing columns {sorted(missing)}")
        sys.exit(2)
    df["event_ts"] = pd.to_datetime(df["event_ts"], errors="coerce")
    bad = int(df["event_ts"].isna().sum())
    if bad:
        print(f"[event-study] dropping {bad} row(s) with unparseable event_ts")
        df = df.dropna(subset=["event_ts"])
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    if "control_ticker" in df.columns:
        df["control_ticker"] = (df["control_ticker"].fillna("").astype(str)
                                .str.strip().str.upper())
    return df.sort_values("event_ts").reset_index(drop=True)


def _load_prices(tickers: list[str], start: str) -> dict[str, pd.Series]:
    """Offline CSV when QT_EVENT_PRICE_CSV is set, else yfinance.

    The offline path exists so the validate suite is hermetic: a test that
    depends on a live download is a test that fails for reasons unrelated to
    the code under test.
    """
    if PRICE_CSV:
        raw = pd.read_csv(PRICE_CSV)
        raw["date"] = pd.to_datetime(raw["date"])
        out: dict[str, pd.Series] = {}
        for tk, grp in raw.groupby("ticker"):
            s = grp.set_index("date")["close"].astype(float).sort_index()
            s = s[~s.index.duplicated(keep="first")]
            out[str(tk).upper()] = s
        return out

    import yfinance as yf
    start_buf = (pd.Timestamp(start) - pd.Timedelta(days=15)).date().isoformat()
    out: dict[str, pd.Series] = {}
    try:
        raw = yf.download(tickers, start=start_buf, progress=False,
                          auto_adjust=True, threads=True)
    except Exception as exc:
        print(f"[event-study] yfinance bulk download failed: {exc}")
        return out
    if raw is None or len(raw) == 0:
        return out
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    for t in close.columns:
        s = pd.to_numeric(close[t], errors="coerce").dropna()
        if len(s):
            s.index = pd.to_datetime(s.index)
            out[str(t).upper()] = s
    return out


# ------------------------------------------------------------- mechanics

def _entry_index(s: pd.Series, event_ts) -> "int | None":
    """First bar STRICTLY AFTER the event. This is the look-ahead guard.

    side="right" is doing all the work here: if event_ts lands exactly on a
    bar, that bar is the session the event occurred in and is NOT tradeable
    on the information, so we take the next one.
    """
    i = int(s.index.searchsorted(pd.Timestamp(event_ts), side="right"))
    return None if i >= len(s) else i


def _fwd_ret(s: pd.Series, entry_i: int, h: int,
             data_end=None) -> "tuple":
    """Return AND status: ok | unmatured | delisted. Delegates to qt.prices.

    🔴 The previous version returned a bare `None` when the series ran out,
    which conflated "this event has not matured" with "this name STOPPED
    TRADING" — and silently dropped the second. Delistings are overwhelmingly
    losses, so the engine was discarding exactly the observations that would
    pull the mean down. See qt.prices.forward_return_ex.
    """
    from qt import prices as qt_prices
    return qt_prices.forward_return_ex(s, entry_i, h, data_end=data_end,
                                       settled_only=SETTLED_ONLY)


def _dedupe_overlaps(df: pd.DataFrame, prices: dict, h: int) -> pd.DataFrame:
    """Independence filter: one event per ticker per horizon window.

    Overlapping windows on the same name share most of their price path.
    Counting them as independent observations inflates the t-stat for free,
    which is precisely how a study talks itself into an effect.
    """
    keep, dropped = [], 0
    last_exit: dict = {}
    for _, r in df.sort_values("event_ts").iterrows():
        tk = r["ticker"]
        s = prices.get(tk)
        if s is None or len(s) == 0:
            continue
        ei = _entry_index(s, r["event_ts"])
        if ei is None:
            continue
        prev = last_exit.get(tk)
        if prev is not None and pd.Timestamp(r["event_ts"]) <= prev:
            dropped += 1
            continue
        exit_i = min(ei + h, len(s) - 1)
        last_exit[tk] = s.index[exit_i]
        keep.append(r)
    if dropped:
        print(f"[event-study] independence filter: dropped {dropped} "
              f"overlapping event(s) inside the {h}-bar window")
    return pd.DataFrame(keep).reset_index(drop=True) if keep else pd.DataFrame()


def run_study(df: pd.DataFrame, prices: dict, h: int) -> pd.DataFrame:
    bench = prices.get(BENCH)
    has_ctl = "control_ticker" in df.columns
    if bench is None and not has_ctl:
        print(f"[event-study] WARNING: benchmark {BENCH} unavailable and no "
              f"control_ticker column — returns will be RAW, not abnormal.")
    # "Now", inferred from the data itself rather than the wall clock: the
    # benchmark trades every session, so its last bar is the freshest date the
    # run could possibly know about. A name whose series ends materially before
    # it has STOPPED TRADING. Structural, like the settlement test — a
    # clock-based version would drift with timezone and market calendar.
    data_end = None
    for _s in prices.values():
        if len(_s) and (data_end is None or _s.index[-1] > data_end):
            data_end = _s.index[-1]

    rows, skipped, n_delisted, n_unmatured = [], 0, 0, 0
    for _, r in df.iterrows():
        tk = r["ticker"]
        s = prices.get(tk)
        if s is None:
            skipped += 1
            continue
        ei = _entry_index(s, r["event_ts"])
        if ei is None:
            skipped += 1
            continue
        ev_ret, status = _fwd_ret(s, ei, h, data_end=data_end)
        if ev_ret is None:
            skipped += 1
            n_unmatured += 1 if status == "unmatured" else 0
            continue
        if status == "delisted":
            n_delisted += 1

        ctl_tk = str(r["control_ticker"]).strip() if has_ctl else ""
        ctl_series = prices.get(ctl_tk) if ctl_tk else bench
        ctl_ret, ctl_used = None, ""
        if ctl_series is not None and len(ctl_series):
            ci = _entry_index(ctl_series, r["event_ts"])
            if ci is not None:
                # The control is a live benchmark; a truncated control window
                # is an immature event, never a delisting.
                ctl_ret, _ = _fwd_ret(ctl_series, ci, h, data_end=None)
                if ctl_ret is not None:
                    ctl_used = ctl_tk if ctl_tk else BENCH
        abn = ev_ret if ctl_ret is None else ev_ret - ctl_ret

        # A delisted name has no bar at entry+h; its exit is its last trade.
        exit_i = min(ei + h, len(s) - 1)
        rows.append({
            "event_id":     r["event_id"],
            "ticker":       tk,
            "event_type":   r["event_type"],
            "event_ts":     pd.Timestamp(r["event_ts"]).isoformat(),
            "entry_date":   s.index[ei].date().isoformat(),
            "exit_date":    s.index[exit_i].date().isoformat(),
            "horizon":      h,
            "event_ret":    round(ev_ret, 6),
            "control":      ctl_used,
            "control_ret":  "" if ctl_ret is None else round(ctl_ret, 6),
            "abnormal_ret": round(abn, 6),
            "exit_status":  status,
        })
    if n_delisted:
        print(f"[event-study] ⚠️  {n_delisted} event(s) DELISTED mid-hold and "
              f"are RECORDED at their last traded price, not dropped. The "
              f"previous engine discarded these as 'unmatured', which removed "
              f"disproportionately bad outcomes.")
    if n_unmatured:
        print(f"[event-study] {n_unmatured} event(s) genuinely not matured yet "
              f"(series still trading) — correctly withheld.")
    if skipped:
        print(f"[event-study] skipped {skipped} event(s): no price, no entry "
              f"bar, or exit bar not yet settled")
    return pd.DataFrame(rows)

# ────────────────────────────── freezing + summary, delegated to qt ─────────
# Both were private copies here. qt.ledger and qt.measurement now own them.
# The wrappers stay so this module's callers and its printed output are
# unchanged -- the merge is an internal one, and a refactor that also changes
# what a script prints is two changes wearing one commit.

def _freeze_first_write(new_df: pd.DataFrame, path: Path,
                        key: str = "event_id") -> pd.DataFrame:
    """Delegates to qt.ledger. First write wins; a written row NEVER moves."""
    merged, report = qt_ledger.freeze_first_write(
        new_df, path, key=key, mutable_env="QT_EVENT_MUTABLE")
    line = report.summary("event-study")
    if report.frozen or report.appended or report.mutable or report.unreadable:
        print(line)
    return merged


def summarize(df: pd.DataFrame, event_type: str = "ALL") -> dict:
    """Delegates to qt.measurement. Returns the legacy dict shape."""
    r = qt_measurement.summarize(df, label=event_type)
    return {"event_type": r.label, "n": r.n, "mean": r.mean, "sd": r.sd,
            "t": r.t, "first_third": r.first_third, "last_third": r.last_third,
            "stability": r.stability, "pass": r.passes}
def _print_summary(s: dict) -> None:
    def flag(ok):
        return "OK  " if ok else "MISS"
    print(f"\n=== E1 read — event_type={s['event_type']} ===")
    if s["n"] < 2:
        print(f"  n={s['n']} — too few settled events to summarise.")
        return
    print(f"  n              : {s['n']:<9} [{flag(s['n'] >= E1_MIN_N)} "
          f"bar N >= {E1_MIN_N}]")
    print(f"  mean abnormal  : {s['mean']:+.4%}  [{flag(s['mean'] >= E1_MIN_EFFECT)} "
          f"bar >= {E1_MIN_EFFECT:+.2%}]")
    t_ok = np.isfinite(s["t"]) and s["t"] >= E1_MIN_T
    print(f"  t-stat         : {s['t']:+.2f}      [{flag(t_ok)} "
          f"bar >= {E1_MIN_T}]")
    st = s["stability"]
    st_txt = "n/a" if not np.isfinite(st) else f"{st:.2f}"
    ok_st = np.isfinite(st) and st >= E1_STABILITY
    print(f"  stability      : {st_txt:<9} [{flag(ok_st)} "
          f"bar >= {E1_STABILITY:.2f}]  "
          f"(first {s['first_third']:+.4%} -> last {s['last_third']:+.4%})")
    print(f"  E1 verdict     : {'PASS' if s['pass'] else 'NOT MET'}")
    print("  (point estimate only — WRC/SPA against the declared K follows "
          "below and is REQUIRED for a pass)")


def _count_only(events: pd.DataFrame, prices: dict, h: int) -> None:
    """Availability check: how many events would SETTLE at horizon h?

    Answers the second blocking question in docs/V27_FORK_DECISION.md §⑥
    without spending budget. It classifies each surviving event as
    ok / unmatured / delisted using ONLY index arithmetic on series length
    and dates — no price is divided by another price, so no return exists
    even transiently. That distinction is what keeps this free under the
    Referee: a READ is a verdict-producing summary, and nothing here can
    produce one.

    Writes nothing. Prints counts and exits.
    """
    from qt import prices as qt_prices
    data_end = None
    for _s in prices.values():
        if len(_s) and (data_end is None or _s.index[-1] > data_end):
            data_end = _s.index[-1]

    counts = {"ok": 0, "unmatured": 0, "delisted": 0, "no_price": 0,
              "no_entry": 0}
    for _, r in events.iterrows():
        s = prices.get(r["ticker"])
        if s is None or len(s) == 0:
            counts["no_price"] += 1
            continue
        ei = _entry_index(s, r["event_ts"])
        if ei is None:
            counts["no_entry"] += 1
            continue
        exit_i = ei + h
        last_usable = len(s) - 2 if SETTLED_ONLY else len(s) - 1
        if exit_i <= last_usable:
            counts["ok"] += 1
            continue
        gap = len(pd.bdate_range(s.index[-1], data_end)) - 1 if data_end is not None else 0
        counts["delisted" if gap > qt_prices.STALE_BARS_TOLERANCE else "unmatured"] += 1

    settled = counts["ok"] + counts["delisted"]
    print(f"\n=== COUNT ONLY — horizon {h} bars — NO returns computed ===")
    for k, v in counts.items():
        print(f"  {k:<10} {v:>5}")
    print(f"  {'SETTLED':<10} {settled:>5}   (ok + delisted; this is the N a read would see)")
    print(f"  E1 needs N >= {E1_MIN_N}: {'CLEARS' if settled >= E1_MIN_N else 'SHORT'} "
          f"by {abs(settled - E1_MIN_N)}")
    print("  Nothing written. Budget untouched.")


def _referee_gate() -> None:
    """Refuse to compute a single return without authorisation.

    Runs BEFORE prices are fetched, so an unauthorised read costs nothing and
    produces nothing. The Referee owns docs/V27_PREREGISTRATION.md's K budget
    (V28_AGENT_ARCHITECTURE.md §3.4); until this call existed it was fully
    tested and never invoked, which is a guard in name only.

    Opt-in via QT_SPEC_ID rather than mandatory, because the same script runs
    the hermetic synthetic suites where there is no specification and nothing
    to spend. ⚠️ The E1 workflow MUST set it — that is what makes the budget
    enforced in code rather than by memory.
    """
    if COUNT_ONLY:
        print("[event-study] COUNT-ONLY mode — no read, Referee not consulted.")
        return
    if not SPEC_ID:
        print("[event-study] no QT_SPEC_ID — running unauthorised. This is "
              "correct ONLY for synthetic/test runs; a real read must set it.")
        return
    read_date = READ_DATE or pd.Timestamp.utcnow().date().isoformat()
    ref = qt_referee.Referee()
    verdict = ref.authorize_read(SPEC_ID, read_date)
    print(f"[referee] {ref.k_used()}/{ref.k_budget} spent — "
          f"'{SPEC_ID}' @ {read_date}: {verdict.reason}")
    if not verdict:
        print(f"::error::REFEREE REFUSED THE READ — {verdict.reason}")
        sys.exit(3)


def main() -> None:
    _referee_gate()
    events = _load_events()
    if events.empty:
        print("[event-study] no usable events.")
        return
    tickers = set(events["ticker"]) | {BENCH}
    if "control_ticker" in events.columns:
        tickers |= {t for t in events["control_ticker"] if t}
    tickers = sorted(tickers)
    start = events["event_ts"].min().date().isoformat()
    print(f"[event-study] {len(events)} event(s) | {len(tickers)} ticker(s) | "
          f"horizon={HORIZON} bars | from {start} | settled_only={SETTLED_ONLY}")

    prices = _load_prices(tickers, start)
    if not prices:
        print("[event-study] FATAL: no price data")
        sys.exit(2)

    events = _dedupe_overlaps(events, prices, HORIZON)
    if events.empty:
        print("[event-study] no independent events survived the filter.")
        return

    if COUNT_ONLY:
        _count_only(events, prices, HORIZON)
        return

    fresh = run_study(events, prices, HORIZON)
    if fresh.empty:
        print("[event-study] no settled events yet — nothing written.")
        return

    merged = _freeze_first_write(fresh, OUT_CSV)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(OUT_CSV, index=False)
    print(f"[event-study] wrote {OUT_CSV} ({len(merged)} row(s))")

    _print_summary(summarize(merged, "ALL"))

    # Survivorship sensitivity. A long-horizon result on a survivor-biased
    # universe is a CEILING, and the only honest way to quote one is beside
    # the number showing how far it moves without the delistings.
    from qt import measurement as _qm
    print()
    print(_qm.format_sensitivity(_qm.survivorship_sensitivity(merged)))

    # Multiple-testing correction against the DECLARED K. E1 requires WRC/SPA
    # at p<0.10 against K=5; until qt.wrc existed this was a NOTE nobody could
    # act on. Event sets are unaligned across specs, so the honest route is a
    # per-spec bootstrap p charged at alpha/K_declared -- unread slots count.
    from qt import wrc as _wrc
    from qt import referee as _ref
    _label = SPEC_ID or "unspecified"
    # k_tested counts specs READ including this one. Earlier reads sit frozen in
    # their own ledgers and are not passed here, so ask the Referee rather than
    # counting the one series supplied -- otherwise the "N unread slot(s)
    # charged" label is off by the number of specs already spent. The BAR is
    # alpha/K_declared either way; only the label was wrong.
    if SPEC_ID:
        _k_tested = _ref.Referee().k_used() + 1
    else:
        _k_tested = None          # synthetic/test runs: no registry context
    _mt = _wrc.event_study_correction({_label: merged["abnormal_ret"].values},
                                      k_declared=_ref.K_BUDGET, alpha=0.10,
                                      k_tested=_k_tested)
    print()
    print("=== E1 multiple-testing (WRC/SPA, declared K) ===")
    for _r in _mt.values():
        print("  " + _r.summary())
    print("  A point-estimate PASS above is NOT an E1 pass unless this line also "
          "reads PASS. The bar is alpha/K_declared, and unread slots are charged.")
    for et, grp in merged.groupby("event_type"):
        if len(grp) >= 2:
            _print_summary(summarize(grp, str(et)))


if __name__ == "__main__":
    main()
