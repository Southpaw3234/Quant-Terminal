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

OUTPUT
------
  data/shadow/rank_ic.csv   — per-day: date,n,rank_ic
  stdout                    — summary: mean rank-IC, std, t-stat, % days >0,
                              window length, and a decile long-short cross-check.

Safe to run anywhere with pandas + yfinance. Exits 0 even on partial data.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PRED_CSV = Path("data/predictions/predictions.csv")
OUT_CSV = Path("data/shadow/rank_ic.csv")
HORIZON_DEFAULT = 5      # trading steps; predictions.csv `horizon_days` overrides per row
MIN_NAMES = 10           # need a real cross-section before an IC is meaningful
DECILE = 30              # for the long-short cross-check (mirrors shadow harness)


def _load_predictions() -> pd.DataFrame:
    if not PRED_CSV.exists():
        print(f"[rank-ic] {PRED_CSV} not found — nothing to do.")
        sys.exit(0)
    df = pd.read_csv(PRED_CSV, low_memory=False)
    need = {"pred_ts", "ticker", "confidence"}
    if not need.issubset(df.columns):
        print(f"[rank-ic] predictions.csv missing {need - set(df.columns)} — abort.")
        sys.exit(0)
    df = df.dropna(subset=["pred_ts", "ticker", "confidence"]).copy()
    df["date"] = df["pred_ts"].astype(str).str.slice(0, 10)
    df = df[df["date"].str.match(r"\d{4}-\d{2}-\d{2}")]
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    df = df.dropna(subset=["confidence"])
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


def _fwd_ret(prices: dict[str, pd.Series], tk: str, entry_iso: str, h: int):
    s = prices.get(tk)
    if s is None or len(s) == 0:
        return None
    p = int(s.index.searchsorted(pd.Timestamp(entry_iso)))
    if p >= len(s) or p + h >= len(s):
        return None  # not matured yet
    return float(s.iloc[p + h] / s.iloc[p] - 1.0)


def main() -> None:
    df = _load_predictions()
    tickers = sorted(df["ticker"].unique().tolist())
    first_date = df["date"].min()
    print(f"[rank-ic] {len(df)} unique (date,ticker) preds | "
          f"{len(tickers)} tickers | from {first_date}")

    prices = _download_prices(tickers, first_date)
    if not prices:
        print("[rank-ic] no price data — cannot compute. Exiting 0.")
        sys.exit(0)

    rows, ls_rows = [], []
    for date, g in df.groupby("date"):
        h = HORIZON_DEFAULT
        if "horizon_days" in g.columns and g["horizon_days"].notna().any():
            h = int(g["horizon_days"].dropna().mode().iloc[0])
        pairs = []
        for tk, conf in zip(g["ticker"], g["confidence"]):
            r = _fwd_ret(prices, tk, date, h)
            if r is not None:
                pairs.append((float(conf), r, tk))
        if len(pairs) < MIN_NAMES:
            continue
        pdf = pd.DataFrame(pairs, columns=["conf", "ret", "tk"])
        ic = pdf["conf"].corr(pdf["ret"], method="spearman")
        if pd.isna(ic):
            continue
        rows.append({"date": date, "n": len(pdf), "rank_ic": round(float(ic), 4)})
        # Decile long-short cross-check (top vs bottom DECILE by confidence).
        if len(pdf) >= 2 * DECILE:
            srt = pdf.sort_values("conf", ascending=False)
            ls = srt.head(DECILE)["ret"].mean() - srt.tail(DECILE)["ret"].mean()
            ls_rows.append(float(ls))

    if not rows:
        print("[rank-ic] no matured days with enough names yet. Exiting 0.")
        sys.exit(0)

    res = pd.DataFrame(rows).sort_values("date")
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT_CSV, index=False)

    ics = res["rank_ic"]
    n = len(ics)
    mean = ics.mean()
    sd = ics.std(ddof=1) if n > 1 else float("nan")
    tstat = mean / (sd / (n ** 0.5)) if n > 1 and sd > 0 else float("nan")
    pos = 100.0 * (ics > 0).mean()
    weeks = (pd.Timestamp(res["date"].iloc[-1]) - pd.Timestamp(res["date"].iloc[0])).days / 7.0

    print("\n=== cross-sectional rank-IC ===")
    print(res.tail(12).to_string(index=False))
    print("\n--- summary ---")
    print(f"days (N)      : {n}")
    print(f"window        : {res['date'].iloc[0]} -> {res['date'].iloc[-1]}  (~{weeks:.1f} weeks)")
    print(f"mean rank-IC  : {mean:.4f}   [gate: >= 0.03]")
    print(f"std daily IC  : {sd:.4f}")
    print(f"t-stat        : {tstat:.2f}     [gate: >= 2.0]")
    print(f"% days IC > 0 : {pos:.0f}%")
    if ls_rows:
        print(f"decile L/S    : mean {sum(ls_rows)/len(ls_rows):+.4f} over {len(ls_rows)} days (cross-check)")
    gate = "PASS" if (mean >= 0.03 and (tstat == tstat) and tstat >= 2.0) else "NOT YET"
    print(f"Stage-1 IC gate: {gate}")
    print(f"\n[rank-ic] wrote {OUT_CSV}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the workflow
        print(f"[rank-ic] non-fatal error: {e}")
        sys.exit(0)
