"""
shadow_intraday.py — Frame 2 (intraday model) shadow evidence engine, P0.
Scoped 2026-07-09 (HANDOFF.md ledger (7), defaults LOCKED). Forward-only,
measurement-only: reads the intraday model's raw scores PRE-BLEND (Cell 11
mixes them into the live ensemble once the model trains — this harness must
measure Frame 2's own skill, not the blend), matures them against the
realized next-session OPEN->CLOSE return (exactly the label the model
predicts), and appends a daily cross-sectional Spearman rank-IC.

Daily flow (morning runs only; idempotent per date):
  1. mature: any logged prediction whose NEXT trading session has completed
     gets realized_oc = close/open - 1 of that session (yfinance daily OHLC).
  2. score:  each fully-matured prediction date gets one Spearman rank-IC row
     (equities only — crypto/ETFs excluded, the 6/26 contamination lesson).
  3. log:    today's data/predictions/intraday_signals.json rows are appended
     — but ONLY if their per-ticker "generated" stamp is today (UTC), so a
     stale committed file can never be re-logged as fresh predictions.

Before the model first trains (~Aug 2026) the signals file doesn't exist:
prints an "armed" notice and exits 0. The clock starts organically on the
first morning the model emits scores. NEVER backfilled from
data/intraday_history/ — the models were Optuna-tuned on that window.

Files (data/shadow_intraday/):
  predictions.csv  date,ticker,score,signal,confidence,target_session,
                   realized_oc,matured_on
  rank_ic.csv      date,n,rank_ic

Writes nothing else; places no orders; touches no live-model state.
"""
import argparse
import datetime
import json
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

SIGNALS_FILE = Path("data/predictions/intraday_signals.json")
OUT_DIR = Path("data/shadow_intraday")
PRED_CSV = OUT_DIR / "predictions.csv"
IC_CSV = OUT_DIR / "rank_ic.csv"

PRED_COLS = ["date", "ticker", "score", "signal", "confidence",
             "target_session", "realized_oc", "matured_on"]
MIN_IC_N = 10        # need >= this many matured equities to print an IC row
STRAGGLER_DAYS = 7   # after this, score a date with whatever matured
TODAY = datetime.datetime.utcnow().strftime("%Y-%m-%d")

# Same exclusion set as analyze_rank_ic.py: crypto (-USD) and ETFs must not
# masquerade as stock-picking skill in a cross-sectional read.
_ETF_TICKERS = {
    'ARKK', 'DIA', 'GLD', 'HYG', 'IWM', 'LQD', 'QQQ', 'SLV', 'SMH', 'SOXX',
    'SPY', 'TLT', 'VNQ', 'XLB', 'XLC', 'XLE', 'XLF', 'XLI', 'XLK', 'XLP',
    'XLRE', 'XLU', 'XLV', 'XLY',
}


def _is_equity(tk) -> bool:
    tk = str(tk)
    return not tk.endswith('-USD') and tk not in _ETF_TICKERS


def load_preds():
    if PRED_CSV.exists():
        df = pd.read_csv(PRED_CSV, dtype={"date": str, "ticker": str,
                                          "target_session": str,
                                          "matured_on": str})
        for c in PRED_COLS:
            if c not in df.columns:
                df[c] = np.nan
        return df[PRED_COLS]
    return pd.DataFrame(columns=PRED_COLS)


def fetch_ohlc(tickers, start, end):
    """{ticker: DataFrame[Open, Close] indexed by YYYY-MM-DD str} via yfinance."""
    import yfinance as yf
    raw = yf.download(sorted(set(tickers)), start=start, end=end,
                      progress=False, auto_adjust=True, group_by="ticker")
    out = {}
    if raw is None or raw.empty:
        return out
    if not isinstance(raw.columns, pd.MultiIndex):   # single-ticker shape
        raw = pd.concat({list(set(tickers))[0]: raw}, axis=1)
    for tk in set(tickers):
        try:
            sub = raw[tk][["Open", "Close"]].dropna()
        except KeyError:
            continue
        if len(sub):
            sub.index = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in sub.index]
            out[tk] = sub
    return out


def mature(preds):
    """Fill realized_oc for predictions whose next session has completed."""
    open_mask = preds["realized_oc"].isna() & (preds["date"] < TODAY)
    if not open_mask.any():
        return preds, 0
    todo = preds[open_mask]
    start = (pd.Timestamp(todo["date"].min()) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    ohlc = fetch_ohlc(todo["ticker"].tolist(), start, TODAY)
    n_new = 0
    for i, row in todo.iterrows():
        px = ohlc.get(row["ticker"])
        if px is None:
            continue
        after = [d for d in px.index if row["date"] < d < TODAY]
        if not after:            # next session not complete yet (or no data)
            continue
        tgt = min(after)
        o, c = float(px.loc[tgt, "Open"]), float(px.loc[tgt, "Close"])
        if o <= 0:
            continue
        preds.loc[i, "target_session"] = tgt
        preds.loc[i, "realized_oc"] = round(c / o - 1.0, 6)
        preds.loc[i, "matured_on"] = TODAY
        n_new += 1
    return preds, n_new


def score(preds):
    """One Spearman rank-IC row per fully-matured prediction date."""
    ic = pd.read_csv(IC_CSV, dtype={"date": str}) if IC_CSV.exists() \
        else pd.DataFrame(columns=["date", "n", "rank_ic"])
    done = set(ic["date"])
    new_rows = []
    for d, grp in preds.groupby("date"):
        if d in done or d >= TODAY:
            continue
        eq = grp[grp["ticker"].map(_is_equity)]
        matured = eq.dropna(subset=["realized_oc"])
        age = (pd.Timestamp(TODAY) - pd.Timestamp(d)).days
        # wait for >=80% of the cohort unless the date has gone stale
        if len(eq) and len(matured) / len(eq) < 0.8 and age <= STRAGGLER_DAYS:
            continue
        if len(matured) < MIN_IC_N:
            if age > STRAGGLER_DAYS:   # give up quietly on hopeless dates
                new_rows.append({"date": d, "n": len(matured), "rank_ic": np.nan})
            continue
        r = matured["score"].astype(float).rank().corr(
            matured["realized_oc"].astype(float).rank())
        new_rows.append({"date": d, "n": len(matured), "rank_ic": round(float(r), 5)})
        print(f"  scored {d}: rank-IC {r:+.4f} (n={len(matured)} equities)")
    if new_rows:
        ic = pd.concat([ic, pd.DataFrame(new_rows)], ignore_index=True)
        ic = ic.sort_values("date").reset_index(drop=True)
    return ic, len(new_rows)


def log_today(preds):
    """Append today's freshly-generated signals; refuse stale files."""
    if not SIGNALS_FILE.exists():
        print("  no intraday signals yet (model untrained) — harness armed, clock not started.")
        return preds, 0
    try:
        signals = json.loads(SIGNALS_FILE.read_text())
    except Exception as e:
        print(f"  intraday_signals.json unreadable ({e}) — skipping log.")
        return preds, 0
    if not isinstance(signals, dict) or not signals:
        print("  intraday_signals.json empty — harness armed, clock not started.")
        return preds, 0
    if TODAY in set(preds["date"]):
        print(f"  {TODAY} already logged — idempotent skip.")
        return preds, 0
    rows = []
    n_stale = n_noscore = 0
    for tk, s in signals.items():
        if not isinstance(s, dict):
            continue
        # model_intraday.py writes "intraday_score" (its docstring's "score"
        # never shipped); accept both keys. The old score-only read silently
        # skipped every signal on the 7/10 first training day, and the
        # "stale file" print below misattributed it. Cell 11's live blend
        # reads intraday_score, so the producer key is the canonical one.
        sc = s.get("intraday_score", s.get("score"))
        if sc is None:
            n_noscore += 1
            continue
        gen = str(s.get("generated", ""))[:10]
        if gen != TODAY:
            n_stale += 1
            continue   # stale committed file from an earlier day — never re-log
        rows.append({"date": TODAY, "ticker": str(tk),
                     "score": float(sc),
                     "signal": s.get("signal"),
                     "confidence": s.get("confidence"),
                     "target_session": np.nan, "realized_oc": np.nan,
                     "matured_on": np.nan})
    if not rows:
        print(f"  no loggable signals for {TODAY} "
              f"({n_stale} stale-dated, {n_noscore} without a score key) — not logging.")
        return preds, 0
    preds = pd.concat([preds, pd.DataFrame(rows)], ignore_index=True)
    n_eq = sum(_is_equity(r["ticker"]) for r in rows)
    print(f"  logged {len(rows)} signals for {TODAY} ({n_eq} equities).")
    return preds, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"\n{'=' * 55}")
    print(f"FRAME-2 SHADOW HARNESS -- {TODAY}")
    print(f"{'=' * 55}")

    preds = load_preds()
    print(f"  state: {len(preds)} logged predictions, "
          f"{preds['realized_oc'].notna().sum()} matured")

    preds, n_mat = mature(preds)
    if n_mat:
        print(f"  matured {n_mat} predictions vs next-session open->close")
    ic, n_ic = score(preds)
    preds, n_log = log_today(preds)

    if args.dry_run:
        print("  [dry-run] nothing written.")
        return
    if n_mat or n_ic or n_log:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        preds.to_csv(PRED_CSV, index=False)
        if n_ic or IC_CSV.exists():
            ic.to_csv(IC_CSV, index=False)
        print(f"  wrote {PRED_CSV.name}" + (f", {IC_CSV.name}" if n_ic else ""))
    else:
        print("  nothing to write.")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
