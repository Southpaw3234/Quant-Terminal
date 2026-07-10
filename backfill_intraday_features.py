"""
backfill_intraday_features.py — one-off remediation for the tz-mismatch bug
(HANDOFF.md 7/9 ledger (8), CI-confirmed run 29066101841): every
data/intraday_history/ snapshot since 5/18 carries 100% nulls in the five
intraday feature columns, because the Cell-6 merge reindexed a tz-aware
15-minute-bar index against the tz-naive daily index.

These five columns are RAW market-derived aggregates (open/close/VWAP/range
of the session), recomputable from yfinance's rolling 60-day 15m window —
backfilling them is legitimate; the no-backfill lock covers predictions and
shadow books, not raw data. This tool:

  1. fetches 60d x 15m bars per snapshot ticker (parallel, like the morning
     run) and recomputes the five features with the FIXED tz-naive logic,
     including the shift(1): the value stored for snapshot date D is computed
     from session D-1's bars — exactly what the live merge would have written;
  2. fills ONLY null cells of those five columns in the existing snapshots —
     non-null values and all other columns are never touched (idempotent);
  3. reports per-column null rates before/after, per-date fill coverage, and
     range sanity checks.

BACKFILL_MODE=dry-run (default) computes and reports, writes NOTHING.
BACKFILL_MODE=execute rewrites the snapshot CSVs in place (the workflow
commits them). Never touches models, predictions, orders, or live state.
"""
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HIST_DIR = Path("data/intraday_history")
COLS = ["intraday_mom", "overnight_gap", "vwap_dev",
        "intraday_range", "close_to_high"]
MODE = os.environ.get("BACKFILL_MODE", "dry-run").strip().lower()
SANITY = {"intraday_mom": 0.30, "overnight_gap": 0.30, "vwap_dev": 0.30,
          "intraday_range": 0.50, "close_to_high": 1.0 + 1e-9}


def fetch_features(ticker):
    """Fixed replica of quant_runner's _fetch_intraday_features (incl. shift)."""
    try:
        import yfinance as yf
        bars = yf.download(ticker, period="60d", interval="15m",
                           progress=False, auto_adjust=True)
        if bars is None or bars.empty or len(bars) < 10:
            return ticker, None
        if isinstance(bars.columns, pd.MultiIndex):
            bars.columns = bars.columns.get_level_values(0)
        bars.index = pd.to_datetime(bars.index)
        if bars.index.tz is not None:
            bars.index = bars.index.tz_localize(None)
        bars["_date"] = bars.index.normalize()
        grp = bars.groupby("_date")

        d = pd.DataFrame(index=list(grp.groups.keys()))
        d.index = pd.to_datetime(d.index)
        d["_vwap"] = grp.apply(
            lambda g: (g["Close"] * g["Volume"]).sum() / max(g["Volume"].sum(), 1))
        d["_open"] = grp["Open"].first()
        d["_close"] = grp["Close"].last()
        d["_high"] = grp["High"].max()
        d["_low"] = grp["Low"].min()
        pc = d["_close"].shift(1)
        hl = (d["_high"] - d["_low"]).clip(1e-8)
        d["intraday_mom"] = (d["_close"] - d["_open"]) / d["_open"].clip(1e-8)
        d["overnight_gap"] = (d["_open"] - pc) / pc.clip(1e-8)
        d["vwap_dev"] = (d["_close"] - d["_vwap"]) / d["_vwap"].clip(1e-8)
        d["intraday_range"] = hl / d["_close"].clip(1e-8)
        d["close_to_high"] = (d["_high"] - d["_close"]) / hl
        out = d[COLS].shift(1)          # snapshot D holds session D-1's features
        out.index = [ts.strftime("%Y-%m-%d") for ts in out.index]
        return ticker, out
    except Exception:
        return ticker, None


def main():
    print(f"MODE={MODE}")
    files = sorted(HIST_DIR.glob("*.csv"))
    snaps = {f: pd.read_csv(f) for f in files}
    tickers = sorted({t for df in snaps.values() for t in df["ticker"].astype(str)})
    total = sum(len(df) for df in snaps.values())
    null_before = sum(df[c].isna().sum() for df in snaps.values() for c in COLS)
    print(f"snapshots: {len(files)} ({files[0].stem} -> {files[-1].stem})  "
          f"rows {total}  tickers {len(tickers)}")
    print(f"null cells in {COLS} before: {null_before}")

    feats, failed = {}, 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for tk, df in ex.map(fetch_features, tickers):
            if df is None:
                failed += 1
            else:
                feats[tk] = df
    print(f"fetched 15m-derived features for {len(feats)}/{len(tickers)} tickers "
          f"({failed} failed/rate-limited)")

    filled = insane = 0
    per_date = {}
    for f, df in snaps.items():
        day = f.stem
        n_day = 0
        for i, row in df.iterrows():
            src = feats.get(str(row["ticker"]))
            if src is None or day not in src.index:
                continue
            for c in COLS:
                if pd.notna(row.get(c)):
                    continue                      # never overwrite real data
                v = src.loc[day, c]
                if pd.isna(v):
                    continue
                if abs(float(v)) > SANITY[c]:
                    insane += 1
                    continue
                df.loc[i, c] = round(float(v), 8)
                filled += 1
                n_day += 1
        per_date[day] = n_day
    null_after = sum(df[c].isna().sum() for df in snaps.values() for c in COLS)

    print(f"\nfilled {filled} cells  (skipped {insane} failing range sanity)")
    print(f"null cells after: {null_after}  "
          f"({(1 - null_after / max(null_before, 1)):.0%} recovered)")
    thin = [d for d, n in per_date.items() if n == 0]
    if thin:
        print(f"dates with ZERO fills (outside the 15m window / fetch gaps): {thin}")
    print("per-date fills: " + "  ".join(f"{d[5:]}:{n}" for d, n in sorted(per_date.items())))

    if MODE != "execute":
        print("\nDRY-RUN — nothing written. Re-dispatch with mode=execute.")
        return 0
    for f, df in snaps.items():
        df.to_csv(f, index=False)
    print(f"\nEXECUTE — rewrote {len(files)} snapshot files in place.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
