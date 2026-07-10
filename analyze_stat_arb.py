"""
analyze_stat_arb.py — Frame 3 (stat-arb shadow book) gate scorecard.
Measurement-only: reads data/stat_arb/stat_arb_ls.csv + shadow_trades.csv,
prints the P2 scorecard (analog of analyze_rank_ic.py's rank-IC block).
Writes NOTHING; submits NOTHING.

Gates (from the REAL-MONEY DEPLOYMENT GATE table, committed blind 2026-06-07):
    beta vs SPY   |beta| < 0.2   (market-neutral by construction — this is the test)
    max drawdown  > -15%
    window        >= 30 daily obs (~6 weeks) before any reading is decision-grade

Sharpe and %win are REPORT-ONLY: no threshold was committed blind for them,
and inventing one after seeing the data is the snooping the gate forbids.

Beta alignment note: the book has calendar gaps (rows are only days the shadow
harness ran — e.g. 7/1-7/3 were lost to the Drive-sync incident), and each
row's P&L spans from the PREVIOUS book date's close. So SPY returns are
computed over the same consecutive-book-date spans, not naive 1-day returns;
the first row (no prior span) is excluded from the beta fit.
"""
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

LS_CSV = Path("data/stat_arb/stat_arb_ls.csv")
TRADES_CSV = Path("data/stat_arb/shadow_trades.csv")
MIN_OBS = 30          # ~6 weeks of daily obs, mirroring the Stage-1 window gate
BETA_GATE = 0.2
MAXDD_GATE = -0.15


def spy_span_returns(dates):
    """SPY return over each consecutive pair of book dates (first row -> NaN)."""
    import yfinance as yf
    start = (pd.Timestamp(dates.min()) - pd.Timedelta(days=7)).date()
    end = (pd.Timestamp(dates.max()) + pd.Timedelta(days=3)).date()
    px = yf.download("SPY", start=str(start), end=str(end),
                     progress=False, auto_adjust=True)["Close"]
    if hasattr(px, "columns"):          # yfinance >=0.2.31 returns a frame
        px = px.iloc[:, 0]
    px.index = [pd.Timestamp(d).date() for d in px.index]
    closes = []
    for d in dates:
        dd = pd.Timestamp(d).date()
        avail = [k for k in px.index if k <= dd]
        closes.append(float(px.loc[max(avail)]) if avail else np.nan)
    closes = pd.Series(closes, dtype=float)
    return closes / closes.shift(1) - 1.0


def main():
    if not LS_CSV.exists():
        print("[stat-arb scorecard] no stat_arb_ls.csv yet — nothing to score.")
        return

    df = pd.read_csv(LS_CSV)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    ret = df["book_return"].astype(float)
    n = len(df)

    print("\n=== stat-arb gate scorecard (Frame 3 shadow book) ===")
    print(f"days (N)      : {n}  ({df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()})"
          + ("" if n >= MIN_OBS else f"   [window gate: >= {MIN_OBS} -> NOT YET decision-grade]"))
    active = int(((df["n_open"] > 0) | (df["n_entries"] > 0) | (df["n_exits"] > 0)).sum())
    print(f"active days   : {active}/{n}   avg open pairs {df['n_open'].mean():.1f} (cap 8)")

    eq = (1.0 + ret).cumprod()
    maxdd = float((eq / eq.cummax() - 1.0).min())
    print(f"mean daily    : {ret.mean():+.4%}   cumulative {(eq.iloc[-1] - 1):+.2%}   "
          f"net P&L ${df['net_pnl'].sum():+,.0f} (costs ${df['cost'].sum():,.0f})")
    print(f"max drawdown  : {maxdd * 100:.1f}%      [gate: > -15%  -> "
          f"{'OK' if maxdd > MAXDD_GATE else 'FAIL'}]")

    sd = float(ret.std(ddof=1)) if n >= 2 else float("nan")
    if sd == sd and sd > 0:
        sharpe = float(ret.mean() / sd * np.sqrt(252))
        print(f"Sharpe (ann.) : {sharpe:+.2f}   [report-only — no blind threshold committed]")
    else:
        print("Sharpe (ann.) : n/a (need >=2 obs with variance)")
    wins = int((ret > 0).sum())
    print(f"%win days     : {wins}/{n} = {wins / n:.0%}   [report-only]")

    beta = None
    try:
        spy = spy_span_returns(df["date"])
        fit = pd.DataFrame({"book": ret, "spy": spy}).dropna()
        if len(fit) >= 5 and float(fit["spy"].std()) > 0:
            beta = float(np.polyfit(fit["spy"].values, fit["book"].values, 1)[0])
            corr = float(fit["book"].corr(fit["spy"]))
            print(f"beta vs SPY   : {beta:+.2f}   (corr {corr:+.2f}, n={len(fit)}, "
                  f"span-aligned)   [gate: |beta| < {BETA_GATE}  -> "
                  f"{'OK' if abs(beta) < BETA_GATE else 'FAIL'}]")
        else:
            print(f"beta vs SPY   : n/a (need >=5 aligned spans, have {len(fit)})")
    except Exception as e:
        print(f"beta vs SPY   : n/a (SPY fetch failed: {type(e).__name__}: {e})")

    if TRADES_CSV.exists():
        tr = pd.read_csv(TRADES_CSV)
        if len(tr):
            w = int((tr["cum_pnl"] > 0).sum())
            print(f"closed trades : {len(tr)}  %win {w}/{len(tr)} = {w / len(tr):.0%}   "
                  f"avg P&L ${tr['cum_pnl'].mean():+,.0f}   "
                  f"avg hold {tr['days_held'].mean():.1f}d   [report-only]")
            by = tr.groupby("exit_reason")["cum_pnl"].agg(["size", "sum"])
            reasons = "  ".join(f"{r}:{int(s)} (${p:+,.0f})"
                                for r, (s, p) in by.iterrows())
            print(f"exit reasons  : {reasons}")

    if n < MIN_OBS:
        verdict = "NOT YET decision-grade"
    else:
        fails = [g for g, bad in (("max-DD", maxdd <= MAXDD_GATE),
                                  ("beta", beta is not None and abs(beta) >= BETA_GATE))
                 if bad]
        if fails:
            verdict = "FAIL (" + ", ".join(fails) + ")"
        elif beta is None:
            verdict = "PASS on max-DD; beta unreadable"
        else:
            verdict = "PASS on committed gates"
    print(f"Frame 3 gate  : {verdict} — full read at >= {MIN_OBS} obs "
          f"(~{max(0, MIN_OBS - n)} trading days away)")
    print("=" * 54)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
