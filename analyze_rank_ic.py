#!/usr/bin/env python3
"""
analyze_rank_ic.py — cross-sectional rank-IC of the live model's daily predictions.

WHY THIS EXISTS
---------------
The Stage-1 real-money gate (see HANDOFF.md) hinges on the cross-sectional
**rank-IC** (≥0.03, t-stat ≥2.0): the rank correlation between the model's
predicted confidence and the realized forward return, measured each day across
the universe. None of the existing artifacts give it directly:

  * data/shadow/cross_sectional_pnl.csv stores only a decile long-short return,
    not a per-name rank correlation.
  * data/predictions/predictions.csv DOES log per-name predicted `confidence`
    for the full universe every day (current), but its realized-return columns
    (`scored`/`actual_return`) silently stopped backfilling on 2026-05-14, so
    the realized side cannot be read from the file.

KEY INSIGHT: realized forward returns are NOT lost — they are a deterministic
function of public price history, so they are recomputable for EVERY past
prediction. This script therefore joins the (current) predicted confidence from
predictions.csv with forward returns it recomputes itself from price data, and
reports the daily rank-IC series + t-stat over the whole window. It reads the
live model's output and recomputes returns independently; it changes NOTHING the
model trades and does not depend on the broken scorer.

Forward-return convention mirrors the in-run shadow harness
(quant_runner.py `_fwd_ret_sh`): per-ticker close-to-close over HORIZON trading
steps, located by searchsorted on each ticker's own calendar (so weekend-trading
crypto tickers align correctly).

SETTLED ROWS ONLY (2026-08-06): a day enters the series only once its exit bar
is provably a completed session — i.e. a later bar exists. Before this, the
newest row was computed off the current session's unsettled intraday print and
was silently restated next run (7/24 -0.1186 -> -0.0389; 7/27 +0.0168 ->
-0.0387, a sign flip). The series now lags one session and never moves. See
SETTLED_ONLY / QT_SETTLED_ONLY below.

OUTPUT
------
  data/shadow/rank_ic.csv   — per-day: date,n,rank_ic
  stdout                    — summary: mean rank-IC, std, t-stat, % days >0,
                              window length, and a decile long-short cross-check.

Safe to run anywhere with pandas + yfinance. Exits 0 even on partial data.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Stage-1 window start. Predictions BEFORE this date were produced by the
# stale-featured-row era (fix 5e96366, 5th dated model change): every signal's
# features were 5-10 sessions old per ticker, so the pre-fix rank-IC / L/S
# series measured a different (lagged) strategy. User decision 2026-07-14:
# restart the Stage-1 window at the fix rather than blend regimes. The full
# pre-fix series remains recomputable with QT_STAGE1_START=2026-05-12, and its
# final read is frozen in data/shadow/stale_era_final/.
STAGE1_START = os.environ.get("QT_STAGE1_START", "2026-07-14")

PRED_CSV = Path("data/predictions/predictions.csv")

# ── DUAL SERIES (2026-08-05) ────────────────────────────────────────────────
# Which column carries the model's per-name view. Defaults reproduce the legacy
# series byte-for-byte; the workflow runs this script a second time with
# QT_RANK_SCORE_COL=rank_score to build the parallel v2 series.
#
# WHY: `confidence` is NOT a ranking score. Cell 13's ternary execution gate
# (quant_runner.py, "Ternary gate:" log line) overwrites it to exactly 0.50 for
# every HOLD and every SELL in order to suppress execution — and log_prediction
# runs AFTER that, so predictions.csv records the execution flag, not the
# model's conviction. Measured 2026-08-05 over 2026-07-14..07-29:
#   * 95.3%-99.6% of the 279-name cross-section sits at exactly 0.500
#   * ZERO names ever score below 0.5 (SELLs are flattened too), so the short
#     decile contains no model-selected name on ANY day — sort_values is stable,
#     so both legs fall back to file order among the ties
#   * the long decile is 1-13 genuinely-ranked names padded to 30 with filler
#   * effective sample is ~91 pick-observations, not the 12 x 279 the header
#     prints, which is why beta_roll never identified
# `rank_score` is the same `confidence` captured immediately BEFORE that gate:
# calibrated P(bull) with the conformal shrink applied — every legitimate
# modelling layer, minus only the execution suppression.
#
# Both series run in parallel deliberately. The legacy one is NOT retired: it
# is what the Stage-1 gate has always read, and switching outright would
# restart the decision window a third time with no overlap to compare against.
SCORE_COL = os.environ.get("QT_RANK_SCORE_COL", "confidence")
SERIES_LABEL = os.environ.get("QT_RANK_SERIES_LABEL", "")
OUT_CSV = Path(os.environ.get("QT_RANK_IC_OUT", "data/shadow/rank_ic.csv"))
LS_CSV = Path(os.environ.get("QT_RANK_LS_OUT",
                             "data/shadow/cross_sectional_ls.csv"))
# ── SETTLED-ROW POLICY (2026-08-06) ─────────────────────────────────────────
# The newest row of this series used to be PROVISIONAL and got restated, often
# badly. The analyzer runs at ~11:50 ET on the day each book matures, so the
# exit bar it reads is that day's UNSETTLED intraday print; the next session
# recomputes the row off the real close. Observed restatements (8/05 audit):
#   * 2026-07-24  -0.1186 -> -0.0389   (-67%)
#   * 2026-07-27  +0.0168 -> -0.0387   (SIGN FLIP)
# Both rank_ic.csv and cross_sectional_ls.csv were affected, and every reported
# mean/t-stat carried one unsettled observation. The 7/31 per-day series quoted
# 7/24 at its provisional value.
#
# With this on, a day enters the series only once its exit bar is provably a
# completed session (see _fwd_ret). Cost: the newest observation appears one
# session later than it used to. Benefit: rows never move once written, so a
# quoted number stays true. Set QT_SETTLED_ONLY=0 to reproduce the old
# provisional behaviour (both series are rebuilt from scratch every run, so the
# flag round-trips exactly).
SETTLED_ONLY = os.environ.get("QT_SETTLED_ONLY", "1").strip().lower() \
    not in ("0", "false", "no")

HORIZON_DEFAULT = 5      # trading steps; predictions.csv `horizon_days` overrides per row
MIN_NAMES = 10           # need a real cross-section before an IC is meaningful
DECILE = 30              # for the long-short cross-check (mirrors shadow harness)

# The cross-section is meant to measure single-name EQUITY selection skill.
# Crypto (-USD) and ETFs are excluded so one DOGE candle or a sector-ETF move
# can't masquerade as stock-picking alpha — these contaminated the legacy
# in-notebook shadow book (which also under-sampled each leg). SPY is fetched
# separately for the long-short beta but is never part of the cross-section.
_ETF_TICKERS = {
    'ARKK', 'DIA', 'GLD', 'HYG', 'IWM', 'LQD', 'QQQ', 'SLV', 'SMH', 'SOXX',
    'SPY', 'TLT', 'VNQ', 'XLB', 'XLC', 'XLE', 'XLF', 'XLI', 'XLK', 'XLP',
    'XLRE', 'XLU', 'XLV', 'XLY',
}


def _freeze_first_write(new_df: "pd.DataFrame", path: Path, label: str) -> "pd.DataFrame":
    """First write wins: a row that already exists is NEVER recomputed.

    Added 2026-08-11. This analyzer is a FULL OVERWRITE — it rebuilds every
    historical row on every run from freshly downloaded prices — so written
    rows moved even though the code printed "Rows already written never move."
    Proven on the 8/10 morning run: `2026-07-15` went `278,0.0959` ->
    `279,0.0955`, and cross_sectional_ls.csv had all 13 rows rewritten
    (2026-07-16 long_short 0.05304 -> 0.04148, a 22% move on a three-week-old
    row). `41c7d17` only DEFERRED a row's first write until its exit bar
    settled; it never made written rows immutable, so it could not have
    prevented this.

    Why it matters beyond tidiness: rank_ic_v2.csv is written by this same
    script (QT_RANK_IC_OUT) and is the S4 decision input due ~2026-09-24. A
    recomputed series means the number read on decision day is not the 30
    observations that were accumulated — it is whatever today's price
    download implies. Freezing makes the window an append-only ledger.

    Timing note: v2 does not exist yet (first row due ~2026-08-13), so it is
    frozen from its very first row. The legacy series inherits whatever has
    already drifted, which is harmless — it is documented as INVALID and is
    not a gate input.

    Drift is REPORTED rather than discarded: knowing how far a recompute
    would have moved a written row is evidence about price-source stability,
    and silently dropping it would trade one blind spot for another.

    QT_RANK_IC_MUTABLE=1 restores the old full-overwrite behaviour for
    debugging. It must never be set on a scheduled run.
    """
    if os.environ.get("QT_RANK_IC_MUTABLE", "").strip() == "1":
        print(f"[rank-ic] {label}: QT_RANK_IC_MUTABLE=1 — full recompute, "
              f"written rows MAY MOVE. Not for scheduled runs.")
        return new_df
    if not path.exists():
        return new_df
    try:
        old = pd.read_csv(path)
    except Exception as exc:                       # unreadable/truncated file
        print(f"[rank-ic] {label}: existing {path} unreadable ({exc}) — "
              f"writing the fresh computation")
        return new_df
    if old.empty or "date" not in old.columns or "date" not in new_df.columns:
        return new_df

    new_by_date = {str(r["date"]): r for _, r in new_df.iterrows()}
    moved = []
    for _, orow in old.iterrows():
        nrow = new_by_date.get(str(orow["date"]))
        if nrow is None:
            continue
        for col in old.columns:
            if col == "date" or col not in new_df.columns:
                continue
            ov, nv = orow[col], nrow[col]
            try:
                if pd.isna(ov) and pd.isna(nv):
                    continue
                same = float(ov) == float(nv)
            except (TypeError, ValueError):
                same = str(ov) == str(nv)
            if not same:
                moved.append(f"{orow['date']}.{col} {ov}->{nv}")
    if moved:
        print(f"[rank-ic] {label}: FROZE {len(moved)} recomputed value(s) that "
              f"would have changed already-written rows — kept the originals: "
              f"{'; '.join(moved[:8])}{' ...' if len(moved) > 8 else ''}")

    fresh = new_df[~new_df["date"].astype(str).isin(set(old["date"].astype(str)))]
    if len(fresh):
        print(f"[rank-ic] {label}: appending {len(fresh)} new row(s): "
              f"{', '.join(fresh['date'].astype(str).tolist()[:5])}")
    return pd.concat([old, fresh], ignore_index=True).sort_values("date")


def _is_equity(tk) -> bool:
    tk = str(tk)
    return not tk.endswith('-USD') and tk not in _ETF_TICKERS


def _load_predictions() -> pd.DataFrame:
    if not PRED_CSV.exists():
        print(f"[rank-ic] {PRED_CSV} not found — nothing to do.")
        sys.exit(0)
    df = pd.read_csv(PRED_CSV, low_memory=False)
    need = {"pred_ts", "ticker", SCORE_COL}
    if not need.issubset(df.columns):
        # Expected for the v2 series until the first post-deploy run logs
        # rank_score. Exit 0 so the parallel invocation never fails the build.
        print(f"[rank-ic] predictions.csv has no '{SCORE_COL}' column yet — "
              f"series not started. Exiting 0.")
        sys.exit(0)
    df = df.dropna(subset=["pred_ts", "ticker", SCORE_COL]).copy()
    df["date"] = df["pred_ts"].astype(str).str.slice(0, 10)
    df = df[df["date"].str.match(r"\d{4}-\d{2}-\d{2}")]
    df[SCORE_COL] = pd.to_numeric(df[SCORE_COL], errors="coerce")
    df = df.dropna(subset=[SCORE_COL])
    if df.empty:
        print(f"[rank-ic] '{SCORE_COL}' present but empty on every row — "
              f"series not started. Exiting 0.")
        sys.exit(0)
    if "horizon_days" in df.columns:
        df["horizon_days"] = pd.to_numeric(df["horizon_days"], errors="coerce")
    # One prediction per (date, ticker): keep the first cycle of the day.
    df = df.sort_values("pred_ts").drop_duplicates(["date", "ticker"], keep="first")
    return df


def _download_prices(tickers: list[str], start: str) -> dict[str, pd.Series]:
    import yfinance as yf
    start_buf = (pd.Timestamp(start) - pd.Timedelta(days=10)).date().isoformat()
    out: dict[str, pd.Series] = {}
    try:
        raw = yf.download(tickers, start=start_buf, progress=False,
                          auto_adjust=True, threads=True)
    except Exception as e:
        print(f"[rank-ic] yfinance bulk download failed: {e}")
        return out
    if raw is None or len(raw) == 0:
        return out
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(close, pd.Series):  # single ticker edge case
        close = close.to_frame(tickers[0])
    for t in close.columns:
        s = pd.to_numeric(close[t], errors="coerce").dropna()
        if len(s):
            s.index = pd.to_datetime(s.index)
            out[t] = s
    return out


def _fwd_ret(prices: dict[str, pd.Series], tk: str, entry_iso: str, h: int,
             settled_only: bool = True):
    s = prices.get(tk)
    if s is None or len(s) == 0:
        return None
    p = int(s.index.searchsorted(pd.Timestamp(entry_iso)))
    if p >= len(s):
        return None
    exit_i = p + h
    # SETTLEMENT TEST: require at least one bar AFTER the exit bar. The newest
    # bar yfinance returns is the CURRENT session whenever the market is open —
    # an unsettled intraday print, not a close — and the analyzer runs ~11:50 ET.
    # The existence of a later bar proves the exit bar is a completed session,
    # with no dependence on wall-clock, timezone or market calendar. (This repo
    # has been bitten three times by clock-based reasoning: the ET marker fix
    # 8d3e9df, the market-hours guard e7b1d5f, and the pnl_history re-dating
    # c225537. A structural test cannot drift.)
    last_usable = len(s) - 2 if settled_only else len(s) - 1
    if exit_i > last_usable:
        return None  # not matured yet, or exit bar is still unsettled
    return float(s.iloc[exit_i] / s.iloc[p] - 1.0)


def main() -> None:
    df_all = _load_predictions()
    n_stale_days = df_all.loc[df_all["date"] < STAGE1_START, "date"].nunique()
    if n_stale_days:
        print(f"[rank-ic] Stage-1 window restart: excluding {n_stale_days} pred-days "
              f"before {STAGE1_START} (stale-feature era, fix 5e96366)")
    df_all = df_all[df_all["date"] >= STAGE1_START]
    df = df_all[df_all["ticker"].map(_is_equity)].copy()
    if df.empty:
        print(f"[rank-ic] no predictions on/after {STAGE1_START} yet — nothing to do.")
        sys.exit(0)
    n_excl = df_all["ticker"].nunique() - df["ticker"].nunique()
    tickers = sorted(df["ticker"].unique().tolist())
    first_date = df["date"].min()
    print(f"[rank-ic] {len(df)} equity (date,ticker) preds | "
          f"{len(tickers)} equity tickers (excluded {n_excl} crypto/ETF) | from {first_date}")

    # SPY is fetched for the long-short beta only; it is not in the cross-section.
    prices = _download_prices(sorted(set(tickers) | {"SPY"}), first_date)
    if not prices:
        print("[rank-ic] no price data — cannot compute. Exiting 0.")
        sys.exit(0)

    rows, ls_rows, ls_recs = [], [], []
    for date, g in df.groupby("date"):
        h = HORIZON_DEFAULT
        if "horizon_days" in g.columns and g["horizon_days"].notna().any():
            h = int(g["horizon_days"].dropna().mode().iloc[0])
        pairs = []
        for tk, conf in zip(g["ticker"], g[SCORE_COL]):
            r = _fwd_ret(prices, tk, date, h, settled_only=SETTLED_ONLY)
            if r is not None:
                pairs.append((float(conf), r, tk))
        if len(pairs) < MIN_NAMES:
            continue
        pdf = pd.DataFrame(pairs, columns=["conf", "ret", "tk"])
        ic = pdf["conf"].corr(pdf["ret"], method="spearman")
        if pd.isna(ic):
            continue
        rows.append({"date": date, "n": len(pdf), "rank_ic": round(float(ic), 4)})
        # Balanced decile long-short (top vs bottom DECILE by confidence). Both
        # legs are exactly DECILE names drawn from the SAME matured-equity set,
        # so the spread is count-balanced by construction (no leg under-sampling).
        if len(pdf) >= 2 * DECILE:
            srt = pdf.sort_values("conf", ascending=False)
            lr = float(srt.head(DECILE)["ret"].mean())
            sr = float(srt.tail(DECILE)["ret"].mean())
            ls_rows.append(lr - sr)
            ls_recs.append({"date": date, "h": int(h), "n_long": DECILE,
                            "n_short": DECILE, "long_ret": round(lr, 5),
                            "short_ret": round(sr, 5), "long_short": round(lr - sr, 5)})

    # Say out loud when a day is being held back, otherwise the settle policy is
    # invisible and the series just looks one row short.
    #
    # Walk the missing days NEWEST-FIRST and report the first one that would
    # have produced a row under the old rule. The walk is load-bearing: the
    # newest missing day is almost never the provisional one — every pred-day
    # from the last HORIZON sessions is also missing simply because it has not
    # matured (n=0). Reading only the newest would silently print nothing.
    # Bounded so a long genuine gap can't turn this into a full rescan.
    if SETTLED_ONLY:
        _missing = sorted(set(df["date"]) - {r["date"] for r in rows}, reverse=True)
        for _d in _missing[:10]:
            _g = df[df["date"] == _d]
            _h = HORIZON_DEFAULT
            if "horizon_days" in _g.columns and _g["horizon_days"].notna().any():
                _h = int(_g["horizon_days"].dropna().mode().iloc[0])
            _n = sum(_fwd_ret(prices, tk, _d, _h, settled_only=False) is not None
                     for tk in _g["ticker"])
            if _n >= MIN_NAMES:
                print(f"[rank-ic] withholding {_d} (n={_n}) — its exit bar is the "
                      f"newest bar and is not settled yet; it enters the series "
                      f"next session. Rows already written never move. "
                      f"(QT_SETTLED_ONLY=0 restores the old provisional row.)")
                break

    if not rows:
        print("[rank-ic] no matured days with enough names yet. Exiting 0.")
        sys.exit(0)

    res = pd.DataFrame(rows).sort_values("date")
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    # Freeze BEFORE writing, and keep the frozen frame — every statistic and
    # gate read below must come from the series that is actually on disk, not
    # from the throwaway recompute. This is the whole point: the gate and the
    # ledger have to agree.
    res = _freeze_first_write(res, OUT_CSV, "rank_ic")
    res.to_csv(OUT_CSV, index=False)

    def _stats(ic_series: pd.Series) -> dict:
        s = ic_series.dropna()
        m = len(s)
        mean = s.mean() if m else float("nan")
        sd = s.std(ddof=1) if m > 1 else float("nan")
        tstat = mean / (sd / (m ** 0.5)) if m > 1 and sd > 0 else float("nan")
        pos = 100.0 * (s > 0).mean() if m else float("nan")
        return {"n": m, "mean": mean, "sd": sd, "tstat": tstat, "pos": pos}

    full = _stats(res["rank_ic"])
    weeks = (pd.Timestamp(res["date"].iloc[-1]) - pd.Timestamp(res["date"].iloc[0])).days / 7.0

    # Trailing window: the recent regime, so a slow stalled-era tail can't mask a
    # turn (or vice-versa). 20 obs ≈ 4 trading weeks.
    TRAIL = 20
    trail = _stats(res["rank_ic"].tail(TRAIL))

    _hdr = f" [{SERIES_LABEL}]" if SERIES_LABEL else ""
    print(f"\n=== cross-sectional rank-IC (score column: {SCORE_COL}){_hdr} ===")
    print(res.tail(12).to_string(index=False))

    # ── TIE DIAGNOSTIC (2026-08-05) ─────────────────────────────────────────
    # The check that would have caught the `confidence` defect on day one. A
    # rank-IC over a column that is ~98% one value is not measuring a
    # cross-section: Spearman hands every tied name the same average rank, so
    # the statistic rides on a handful of names while the header still prints
    # n=279. Both decile legs degenerate too — with 270 ties, sort_values is
    # stable and the legs fall out in file order rather than by model view.
    # Printed for BOTH series so the legacy one carries its own health warning.
    _tie = []
    for _d, _g in df.groupby("date"):
        _v = _g[SCORE_COL]
        _mode_n = int(_v.value_counts().iloc[0]) if len(_v) else 0
        _tie.append((len(_v), _mode_n, int((_v < 0.5).sum())))
    _n_tot = sum(t[0] for t in _tie)
    _n_tied = sum(t[1] for t in _tie)
    _pct = 100.0 * _n_tied / _n_tot if _n_tot else 0.0
    _days_no_short = sum(1 for t in _tie if t[2] == 0)
    print(f"\n--- ranking-variable health ({SCORE_COL}) ---")
    print(f"tied at modal value : {_pct:.1f}% of all (day,name) rows"
          f"   [< 50% -> usable cross-section]")
    print(f"effective names/day : ~{(_n_tot - _n_tied) / max(len(_tie), 1):.0f} "
          f"of {_n_tot / max(len(_tie), 1):.0f} carry a distinct value")
    print(f"days with NO name below 0.5 : {_days_no_short}/{len(_tie)}"
          f"   [> 0 -> the SHORT decile is not model-selected]")
    if _pct >= 50.0 or _days_no_short:
        print("  *** WARNING: this series is NOT a valid cross-sectional rank-IC. ***")
        print("  *** Ties dominate, so both decile legs fall out in FILE ORDER,  ***")
        print("  *** the printed n overstates the real sample, and beta_roll     ***")
        print("  *** cannot identify. Do not gate a GO/NO-GO on it.              ***")
    print("\n--- summary (FULL window) ---")
    print(f"days (N)      : {full['n']}")
    print(f"window        : {res['date'].iloc[0]} -> {res['date'].iloc[-1]}  (~{weeks:.1f} weeks)")
    print(f"mean rank-IC  : {full['mean']:.4f}   [gate: >= 0.03]")
    print(f"std daily IC  : {full['sd']:.4f}")
    print(f"t-stat        : {full['tstat']:.2f}     [gate: >= 2.0]")
    print(f"% days IC > 0 : {full['pos']:.0f}%")
    if ls_rows:
        print(f"decile L/S    : mean {sum(ls_rows)/len(ls_rows):+.4f} over {len(ls_rows)} days (cross-check)")

    print(f"\n--- summary (TRAILING {TRAIL} days) ---")
    print(f"days (N)      : {trail['n']}  ({res['date'].iloc[-trail['n']]} -> {res['date'].iloc[-1]})")
    print(f"mean rank-IC  : {trail['mean']:.4f}   [gate: >= 0.03]")
    print(f"t-stat        : {trail['tstat']:.2f}     [gate: >= 2.0]")
    print(f"% days IC > 0 : {trail['pos']:.0f}%")
    _delta = trail["mean"] - full["mean"]
    print(f"trend         : trailing vs full {_delta:+.4f} "
          f"({'improving' if _delta > 0.005 else 'deteriorating' if _delta < -0.005 else 'flat'})")

    # ── Clean equity-only long-short: write series + beta/drawdown gate inputs ──
    # Supersedes the contaminated in-notebook data/shadow/cross_sectional_pnl.csv
    # (which under-sampled each leg to ~5-10 of 30 names via `featured` dropout
    # and mixed crypto/ETFs across legs). Here both legs are a full balanced
    # DECILE of matured equities, recomputed from public prices over the whole
    # window — so beta and drawdown are decision-grade, not 5-name noise.
    if ls_recs:
        lsdf = pd.DataFrame(ls_recs).sort_values("date").reset_index(drop=True)
        lsdf["spy_fwd"] = [_fwd_ret(prices, "SPY", d, int(h), settled_only=SETTLED_ONLY)
                           for d, h in zip(lsdf["date"], lsdf["h"])]

        # Beta-hedged variant (measurement-only, added 2026-07-08): the SAME
        # long-short picks with a causal SPY overlay sized to the rolling beta.
        # Pre-answers the August gate question: if the trailing rank-IC turns
        # positive, does the signal survive beta neutralization, or was it a
        # beta artifact? (The raw book fails |beta|<0.2 structurally: defensive
        # longs vs high-beta shorts.) beta_roll for row i is fit on the trailing
        # <=20 PRIOR rows only (shift(1) — implementable, no look-ahead), needs
        # >=5 obs, clamped to +/-3; rows without a beta estimate or SPY return
        # stay unhedged (warm-up ~first 5 rows).
        _ls_v = lsdf["long_short"].astype(float)
        _spy_v = pd.to_numeric(lsdf["spy_fwd"], errors="coerce")
        _beta_roll = (_ls_v.rolling(20, min_periods=5).cov(_spy_v)
                      / _spy_v.rolling(20, min_periods=5).var()
                      ).shift(1).replace([np.inf, -np.inf], np.nan).clip(-3.0, 3.0)
        lsdf["beta_roll"] = _beta_roll.round(4)
        lsdf["ls_hedged"] = (_ls_v - (_beta_roll * _spy_v).fillna(0.0)).round(5)

        LS_CSV.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ Caveat specific to this file: beta_roll and ls_hedged are TRAILING
        # rolling columns, so a newly appended row's values come from the fresh
        # recompute while the rows above it are frozen. Second-order, and on a
        # column the handoff already flags as unidentified and not to be quoted
        # (see [[frame1-beta-roll-warmup]]). The per-day columns that feed the
        # gate — long_ret, short_ret, long_short — are exact under the freeze.
        lsdf = _freeze_first_write(lsdf, LS_CSV, "cross_sectional_ls")
        lsdf.to_csv(LS_CSV, index=False)

        ls = lsdf["long_short"].astype(float)
        eq = (1.0 + ls).cumprod()
        maxdd = float((eq / eq.cummax() - 1.0).min())
        mfit = lsdf.dropna(subset=["spy_fwd"])
        beta = corr = float("nan")
        if len(mfit) >= 3 and float(mfit["spy_fwd"].std()) > 0:
            beta = float(np.polyfit(mfit["spy_fwd"].astype(float).values,
                                    mfit["long_short"].astype(float).values, 1)[0])
            corr = float(mfit["long_short"].corr(mfit["spy_fwd"]))

        print(f"\n--- clean equity-only long-short ({DECILE}L/{DECILE}S, gate inputs) ---")
        print(f"days (N)      : {len(lsdf)}  (crypto + ETFs excluded, legs balanced)")
        print(f"mean L/S ret  : {ls.mean():+.4f}   cumulative {(eq.iloc[-1] - 1) * 100:+.1f}%")
        print(f"max drawdown  : {maxdd * 100:.1f}%      [gate: > -15%  -> "
              f"{'OK' if maxdd > -0.15 else 'FAIL'}]")
        if beta == beta:  # not NaN
            print(f"beta vs SPY   : {beta:+.2f}   (corr {corr:+.2f}, n={len(mfit)})   "
                  f"[gate: |beta| < 0.2  -> {'OK' if abs(beta) < 0.2 else 'FAIL'}]")
        else:
            print(f"beta vs SPY   : n/a (need >=3 matured days with SPY)")

        # Beta-hedged read (same picks + causal SPY overlay).
        hs = lsdf["ls_hedged"].astype(float)
        heq = (1.0 + hs).cumprod()
        hdd = float((heq / heq.cummax() - 1.0).min())
        hfit = lsdf.dropna(subset=["spy_fwd", "beta_roll"])
        hbeta = float("nan")
        if len(hfit) >= 3 and float(hfit["spy_fwd"].std()) > 0:
            hbeta = float(np.polyfit(hfit["spy_fwd"].astype(float).values,
                                     hfit["ls_hedged"].astype(float).values, 1)[0])
        n_hedged = int(lsdf["beta_roll"].notna().sum())
        print(f"\n--- beta-HEDGED long-short (same picks + causal SPY overlay) ---")
        print(f"days hedged   : {n_hedged}/{len(lsdf)}  (first ~5 are warm-up, unhedged)")
        print(f"mean ret      : {hs.mean():+.4f}   cumulative {(heq.iloc[-1] - 1) * 100:+.1f}%")
        print(f"max drawdown  : {hdd * 100:.1f}%      [gate: > -15%  -> "
              f"{'OK' if hdd > -0.15 else 'FAIL'}]")
        if hbeta == hbeta:  # not NaN
            print(f"residual beta : {hbeta:+.2f}   (hedged rows only, n={len(hfit)})   "
                  f"[gate: |beta| < 0.2  -> {'OK' if abs(hbeta) < 0.2 else 'FAIL'}]")
        else:
            print("residual beta : n/a (not enough hedged rows yet)")
        print(f"[rank-ic] wrote {LS_CSV}")

    def _gate(st: dict) -> bool:
        return (st["mean"] >= 0.03 and st["tstat"] == st["tstat"] and st["tstat"] >= 2.0)

    print(f"\nStage-1 IC gate (full)     : {'PASS' if _gate(full) else 'NOT YET'}")
    print(f"Stage-1 IC gate (trailing) : {'PASS' if _gate(trail) else 'NOT YET'}")
    print(f"\n[rank-ic] wrote {OUT_CSV}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the workflow
        print(f"[rank-ic] non-fatal error: {e}")
        sys.exit(0)
