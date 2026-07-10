"""
analyze_shadow_intraday.py — Frame 2 shadow P1 + P2 (defaults LOCKED 7/9).
P1: balanced top/bottom-DECILE long-short series from the P0 harness's
    matured predictions (equities only, equal-weight, next-session open->close)
    with the same causal rolling-beta SPY overlay as Frame 1 (ls_hedged) —
    SPY's own open->close is the hedge leg, matching the book's horizon.
P2: gate scorecard — rank-IC (full + trailing-20d) vs the blind Stage-1 gates,
    L/S max-DD and beta-vs-SPY gates, hedged block, >=30-obs window gate;
    Sharpe/%win REPORT-ONLY (no threshold was committed blind).

Reads  data/shadow_intraday/predictions.csv + rank_ic.csv (written by
shadow_intraday.py). Rewrites the derived data/shadow_intraday/
cross_sectional_ls.csv wholesale each run (idempotent by construction).
Measurement-only: no orders, no live-model state. Prints an armed notice and
exits 0 until the P0 clock has data (~Aug 2026).
"""
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

DIR = Path("data/shadow_intraday")
PRED_CSV = DIR / "predictions.csv"
IC_CSV = DIR / "rank_ic.csv"
LS_CSV = DIR / "cross_sectional_ls.csv"

MIN_NAMES = 30       # matured equities needed before a date gets an L/S row
MIN_OBS = 30         # Stage-1 window gate (~6 weeks of daily obs)
IC_GATE, T_GATE = 0.03, 2.0
BETA_GATE, MAXDD_GATE = 0.2, -0.15
TRAIL = 20

_ETF_TICKERS = {
    'ARKK', 'DIA', 'GLD', 'HYG', 'IWM', 'LQD', 'QQQ', 'SLV', 'SMH', 'SOXX',
    'SPY', 'TLT', 'VNQ', 'XLB', 'XLC', 'XLE', 'XLF', 'XLI', 'XLK', 'XLP',
    'XLRE', 'XLU', 'XLV', 'XLY',
}


def _is_equity(tk) -> bool:
    tk = str(tk)
    return not tk.endswith('-USD') and tk not in _ETF_TICKERS


def spy_oc_map(sessions):
    """{session date str: SPY open->close return} for the given sessions."""
    import yfinance as yf
    lo = (pd.Timestamp(min(sessions)) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    hi = (pd.Timestamp(max(sessions)) + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    px = yf.download("SPY", start=lo, end=hi, progress=False, auto_adjust=True)
    out = {}
    if px is None or px.empty:
        return out
    if isinstance(px.columns, pd.MultiIndex):
        px.columns = px.columns.get_level_values(0)
    for d, row in px.iterrows():
        o, c = float(row["Open"]), float(row["Close"])
        if o > 0:
            out[pd.Timestamp(d).strftime("%Y-%m-%d")] = c / o - 1.0
    return out


def stats(series):
    s = series.dropna().astype(float)
    n = len(s)
    mean = float(s.mean()) if n else float("nan")
    t = float(mean / (s.std(ddof=1) / np.sqrt(n))) if n >= 2 and s.std(ddof=1) > 0 else float("nan")
    return n, mean, t


def main():
    if not PRED_CSV.exists():
        print("[frame2-scorecard] harness armed, no predictions yet — nothing to analyze.")
        return

    preds = pd.read_csv(PRED_CSV, dtype={"date": str, "ticker": str,
                                         "target_session": str})
    matured = preds.dropna(subset=["realized_oc"])
    matured = matured[matured["ticker"].map(_is_equity)]
    if matured.empty:
        print("[frame2-scorecard] no matured equity predictions yet — clock too young.")
        return

    # ── P1: balanced decile long-short per matured date ─────────────────────
    recs = []
    for d, grp in sorted(matured.groupby("date")):
        if len(grp) < MIN_NAMES:
            continue
        k = max(3, len(grp) // 10)
        g = grp.sort_values("score", ascending=False)
        ls = float(g.head(k)["realized_oc"].astype(float).mean()
                   - g.tail(k)["realized_oc"].astype(float).mean())
        sess = g["target_session"].mode()
        recs.append({"date": d, "n": len(grp), "decile": k,
                     "target_session": sess.iloc[0] if len(sess) else np.nan,
                     "long_short": round(ls, 5)})
    if not recs:
        print(f"[frame2-scorecard] no dates with >= {MIN_NAMES} matured equities yet "
              f"(have {matured.groupby('date').size().max()} max) — L/S series not started.")
        return

    lsdf = pd.DataFrame(recs).sort_values("date").reset_index(drop=True)
    try:
        smap = spy_oc_map(lsdf["target_session"].dropna())
    except Exception as e:
        print(f"  SPY fetch failed ({type(e).__name__}) — hedge columns will be NaN/unhedged.")
        smap = {}
    lsdf["spy_oc"] = [smap.get(s, np.nan) for s in lsdf["target_session"]]

    # Causal rolling beta (Frame 1's exact recipe): trailing <=20 PRIOR rows,
    # min 5 obs, shift(1) = no look-ahead, clamped +/-3, warm-up unhedged.
    _ls = lsdf["long_short"].astype(float)
    _sp = pd.to_numeric(lsdf["spy_oc"], errors="coerce")
    beta_roll = (_ls.rolling(TRAIL, min_periods=5).cov(_sp)
                 / _sp.rolling(TRAIL, min_periods=5).var()
                 ).shift(1).replace([np.inf, -np.inf], np.nan).clip(-3.0, 3.0)
    lsdf["beta_roll"] = beta_roll.round(4)
    lsdf["ls_hedged"] = (_ls - (beta_roll * _sp).fillna(0.0)).round(5)
    lsdf.to_csv(LS_CSV, index=False)
    print(f"[frame2] wrote {LS_CSV}")

    # ── P2: gate scorecard ───────────────────────────────────────────────────
    print("\n=== Frame-2 gate scorecard (intraday shadow, open->close) ===")
    ic = pd.read_csv(IC_CSV, dtype={"date": str}) if IC_CSV.exists() else \
        pd.DataFrame(columns=["date", "n", "rank_ic"])
    n_ic, m_ic, t_ic = stats(ic["rank_ic"]) if len(ic) else (0, float("nan"), float("nan"))
    print(f"days (N)      : {n_ic} IC obs / {len(lsdf)} L/S rows"
          + ("" if n_ic >= MIN_OBS else
             f"   [window gate: >= {MIN_OBS} -> NOT YET decision-grade]"))
    if n_ic:
        print(f"mean rank-IC  : {m_ic:+.4f}   t-stat {t_ic:+.2f}   "
              f"[gate: >= {IC_GATE} & t >= {T_GATE}  -> "
              f"{'OK' if (m_ic >= IC_GATE and t_ic >= T_GATE) else 'NOT YET'}]")
        nt, mt, tt = stats(ic["rank_ic"].tail(TRAIL))
        print(f"trailing-{TRAIL}d  : {mt:+.4f}   t-stat {tt:+.2f}   (n={nt})")

    eq = (1.0 + _ls).cumprod()
    maxdd = float((eq / eq.cummax() - 1.0).min())
    print(f"mean L/S ret  : {_ls.mean():+.4f}   cumulative {(eq.iloc[-1] - 1):+.2%}")
    print(f"max drawdown  : {maxdd * 100:.1f}%      [gate: > -15%  -> "
          f"{'OK' if maxdd > MAXDD_GATE else 'FAIL'}]")
    sd = float(_ls.std(ddof=1)) if len(_ls) >= 2 else 0.0
    if sd > 0:
        print(f"Sharpe (ann.) : {_ls.mean() / sd * np.sqrt(252):+.2f}   [report-only]")
    wins = int((_ls > 0).sum())
    print(f"%win days     : {wins}/{len(_ls)} = {wins / len(_ls):.0%}   [report-only]")

    beta = None
    fit = pd.DataFrame({"ls": _ls, "spy": _sp}).dropna()
    if len(fit) >= 5 and float(fit["spy"].std()) > 0:
        beta = float(np.polyfit(fit["spy"].values, fit["ls"].values, 1)[0])
        corr = float(fit["ls"].corr(fit["spy"]))
        print(f"beta vs SPY   : {beta:+.2f}   (corr {corr:+.2f}, n={len(fit)}, "
              f"open->close)   [gate: |beta| < {BETA_GATE}  -> "
              f"{'OK' if abs(beta) < BETA_GATE else 'FAIL'}]")
    else:
        print(f"beta vs SPY   : n/a (need >=5 aligned sessions, have {len(fit)})")

    hs = lsdf["ls_hedged"].astype(float)
    n_hedged = int(lsdf["beta_roll"].notna().sum())
    heq = (1.0 + hs).cumprod()
    hdd = float((heq / heq.cummax() - 1.0).min())
    print(f"--- beta-HEDGED (same picks + causal SPY open->close overlay) ---")
    print(f"days hedged   : {n_hedged}/{len(lsdf)}  (warm-up unhedged)")
    print(f"mean ret      : {hs.mean():+.4f}   cumulative {(heq.iloc[-1] - 1):+.2%}   "
          f"max-DD {hdd * 100:.1f}%")

    if n_ic < MIN_OBS:
        verdict = "NOT YET decision-grade"
    else:
        fails = [g for g, bad in (
            ("rank-IC", not (m_ic >= IC_GATE and t_ic >= T_GATE)),
            ("max-DD", maxdd <= MAXDD_GATE),
            ("beta", beta is not None and abs(beta) >= BETA_GATE)) if bad]
        verdict = "FAIL (" + ", ".join(fails) + ")" if fails else "PASS on committed gates"
    print(f"Frame 2 gate  : {verdict} — full read at >= {MIN_OBS} IC obs "
          f"(~{max(0, MIN_OBS - n_ic)} trading days away)")
    print("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
