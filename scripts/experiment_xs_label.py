#!/usr/bin/env python3
"""
S3 — the label/panel experiment.  HANDOFF §"STOPPING RULE", criterion S3.

ONE bounded attempt, pre-registered threshold:
    walk-forward mean OOS rank-IC >= +0.02
    AND consistent sign in >= 8 of 12 folds
Miss it and the conclusion is "the FEATURES are the binding constraint, stop
adding features".  There is no second attempt at re-specifying the label.

WHAT THIS TESTS
---------------
Production trains 307 PER-TICKER models on a PER-TICKER TIME-SERIES label:
    fwd_ret = c.shift(-5)/c - 1
    q80/q20 = fwd_ret.rolling(252).quantile(.80/.20)
    target  = 1 if fwd_ret>=q80, 0 if <=q20, else NaN     <- middle 60% dropped
i.e. "is this stock's 5-day return in the top fifth of ITS OWN trailing year?"

The strategy is CROSS-SECTIONAL: "which 30 of 279 names do I hold today?"
Those are different questions.  On a strong market day nearly every name clears
its own q80, so the label degenerates into a market-direction indicator with no
cross-sectional content -- which is consistent with every symptom on record:
residual beta +1.81, long leg +0.955% vs SPY +0.757%, rank-IC ~0 at AUC ~0.50.

ARMS (a ladder, so the delta is attributable to one change at a time)
--------------------------------------------------------------------
    A  time-series tails label   + per-ticker models     <- control ~= production
    B  time-series tails label   + pooled panel
    C  cross-sectional resid rank label + pooled panel
    D  C + within-day normalised features
    E  D + lambdarank objective (day = query group)

NOT A PRODUCTION CHANGE.  This script reads nothing from and writes nothing to
the live trading path.  It is a measurement.  If it passes, wiring it in is a
separate, later decision.

CONTROL CAVEAT, stated up front: arm A is NOT expected to reproduce production's
0.4957 exactly.  Production's feature set is much larger (macro joins, GARCH,
sentiment, insider, options).  This script uses a self-contained price/volume set
so that all five arms see IDENTICAL features.  What is being measured is the
A->E delta on identical inputs, not the absolute level.
"""
import ast, os, sys, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

H          = 5            # forecast horizon, matches FORECAST_DAYS
EMBARGO    = H + 1        # purge train/test overlap created by the forward label
N_FOLDS    = 12           # matches the production walk-forward
START      = "2022-06-01" # >=252 sessions of warm-up before the first usable label
MIN_NAMES  = 50           # a day needs this many names to carry a cross-section
SEED       = 7

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── universe ────────────────────────────────────────────────────────────────
# Crypto and ETFs are excluded for the same reason analyze_rank_ic.py excludes
# them (:125): one DOGE candle or a sector-ETF move must not masquerade as
# stock-picking alpha. SPY is fetched separately for beta, never in the panel.
_ETFS = {'ARKK', 'DIA', 'GLD', 'HYG', 'IWM', 'LQD', 'QQQ', 'SLV', 'SMH', 'SOXX',
         'SPY', 'TLT', 'VNQ', 'XLB', 'XLC', 'XLE', 'XLF', 'XLI', 'XLK', 'XLP',
         'XLRE', 'XLU', 'XLV', 'XLY'}


def _is_equity(tk):
    return not tk.endswith("-USD") and tk not in _ETFS and 1 <= len(tk) <= 6


def load_universe():
    """The live traded universe, taken from predictions.csv.

    NOT parsed out of quant_runner.py. The obvious-looking `SECTOR_MAP = {...}`
    at quant_runner.py:3037 is NOT module-level code — it lives inside the
    CELL_13_PREPATCH triple-quoted string opened at :2957, so it is injected
    text and ast.walk correctly finds no Assign node for it. A first pass here
    tried exactly that and came back empty. predictions.csv is the authoritative
    list anyway: it is what actually gets scored and traded.
    """
    p = os.path.join(REPO, "data", "predictions", "predictions.csv")
    if not os.path.exists(p):
        sys.exit(f"universe source missing: {p}")
    tk = pd.read_csv(p, usecols=["ticker"])["ticker"].astype(str).str.strip()
    out = sorted({t for t in tk.unique() if _is_equity(t)})
    if len(out) < 100:
        sys.exit(f"universe too small ({len(out)}) — refusing to run a "
                 f"cross-sectional experiment on it")
    return out


# ── data ────────────────────────────────────────────────────────────────────
def download(tickers):
    import yfinance as yf
    frames, chunk = {}, 50
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = yf.download(part, start=START, auto_adjust=True, progress=False,
                         group_by="ticker", threads=True)
        for tk in part:
            try:
                d = df[tk] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            d = d.dropna(subset=["Close"])
            if len(d) > 400:
                frames[tk] = d
        print(f"  downloaded {min(i+chunk, len(tickers))}/{len(tickers)}"
              f"  kept={len(frames)}", flush=True)
    return frames


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


FEATS = ["ret_1", "ret_5", "ret_10", "ret_20", "ret_60", "vol_20", "vol_60",
         "vol_ratio", "rsi_14", "ma_dev_20", "ma_dev_50", "ma_dev_200",
         "volume_z", "range_20", "gap_5", "amihud", "skew_20", "beta_60"]


def build(frames, spy):
    """Per-ticker causal features.  Everything is shifted by 1 at the end, so a
    row dated T contains only information observable at T-1 close."""
    spy_ret = spy["Close"].pct_change()
    spy_fwd = spy["Close"].shift(-H) / spy["Close"] - 1
    rows = []
    for tk, d in frames.items():
        c, v, h, l = d["Close"], d["Volume"], d["High"], d["Low"]
        r = c.pct_change()
        f = pd.DataFrame(index=d.index)
        for n in (1, 5, 10, 20, 60):
            f[f"ret_{n}"] = c / c.shift(n) - 1
        f["vol_20"]     = r.rolling(20).std()
        f["vol_60"]     = r.rolling(60).std()
        f["vol_ratio"]  = f["vol_20"] / f["vol_60"].replace(0, np.nan)
        f["rsi_14"]     = rsi(c)
        for n in (20, 50, 200):
            f[f"ma_dev_{n}"] = c / c.rolling(n).mean() - 1
        f["volume_z"]   = (v - v.rolling(20).mean()) / v.rolling(20).std().replace(0, np.nan)
        f["range_20"]   = ((h - l) / c.replace(0, np.nan)).rolling(20).mean()
        f["gap_5"]      = (d["Open"] / c.shift(1) - 1).rolling(5).mean()
        f["amihud"]     = (r.abs() / (c * v).replace(0, np.nan)).rolling(20).mean() * 1e9
        f["skew_20"]    = r.rolling(20).skew()
        al              = r.align(spy_ret, join="left")
        f["beta_60"]    = (al[0].rolling(60).cov(al[1])
                           / al[1].rolling(60).var().replace(0, np.nan))

        f = f.shift(1)                                   # <- no lookahead
        f["fwd_ret"] = c.shift(-H) / c - 1               # label horizon
        f["mkt_fwd"] = spy_fwd.reindex(f.index)
        f["ticker"]  = tk
        f["date"]    = f.index
        rows.append(f)

    p = pd.concat(rows, ignore_index=True)
    p = p.dropna(subset=FEATS + ["fwd_ret"])
    p = p[np.isfinite(p[FEATS]).all(axis=1)]
    return p.sort_values(["date", "ticker"]).reset_index(drop=True)


def add_labels(p):
    g = p.groupby("ticker", group_keys=False)

    # ── BASELINE: production's per-ticker time-series tails label ───────────
    q80 = g["fwd_ret"].apply(lambda s: s.rolling(252, min_periods=63).quantile(.80))
    q20 = g["fwd_ret"].apply(lambda s: s.rolling(252, min_periods=63).quantile(.20))
    p["y_ts"] = np.where(p["fwd_ret"] >= q80, 1.0,
                np.where(p["fwd_ret"] <= q20, 0.0, np.nan))

    # ── NEW: beta-residualised, ranked WITHIN each day ──────────────────────
    # resid is what a beta-hedged book actually earns, so predict that.
    # Ranking within day removes any common additive move by construction;
    # subtracting beta*mkt additionally removes the high-beta/low-beta tilt.
    p["resid"] = p["fwd_ret"] - p["beta_60"] * p["mkt_fwd"]
    day = p.groupby("date")
    p["y_xs"]  = day["resid"].rank(pct=True)
    p["n_day"] = day["resid"].transform("size")
    p = p[p["n_day"] >= MIN_NAMES].copy()

    # within-day cross-sectional z-scores of every feature (arms D/E)
    for c in FEATS:
        m = day[c].transform("mean")
        s = day[c].transform("std").replace(0, np.nan)
        p[c + "_xs"] = ((p[c] - m) / s).clip(-5, 5)
    p = p.dropna(subset=[c + "_xs" for c in FEATS] + ["y_xs", "resid"])
    return p


# ── evaluation ──────────────────────────────────────────────────────────────
def rank_ic(df):
    """Mean daily Spearman(prediction, realised residual) — the quantity the
    Stage-1 gate is written against and the thing an L/S book monetises."""
    out = []
    for _, d in df.groupby("date"):
        if len(d) >= MIN_NAMES and d["pred"].nunique() > 1:
            out.append(d["pred"].rank().corr(d["resid"].rank()))
    return pd.Series(out, dtype=float).dropna()


def folds(dates):
    u = np.array(sorted(dates.unique()))
    start = int(len(u) * 0.35)                    # first 35% is train-only
    edges = np.linspace(start, len(u), N_FOLDS + 1).astype(int)
    return [(u[edges[i]], u[edges[i + 1] - 1]) for i in range(N_FOLDS)]


def fit_predict(tr, te, cols, mode):
    import lightgbm as lgb
    common = dict(n_estimators=400, learning_rate=0.05, num_leaves=31,
                  min_child_samples=40, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, verbose=-1, random_state=SEED, n_jobs=-1)

    if mode == "per_ticker":                       # production's architecture
        pred = pd.Series(np.nan, index=te.index)
        for tk, te_t in te.groupby("ticker"):
            tr_t = tr[tr["ticker"] == tk]
            tr_t = tr_t[tr_t["y_ts"].notna()]
            if len(tr_t) < 80 or tr_t["y_ts"].nunique() < 2:
                continue
            m = lgb.LGBMClassifier(**{**common, "n_estimators": 200})
            m.fit(tr_t[cols], tr_t["y_ts"].astype(int))
            pred.loc[te_t.index] = m.predict_proba(te_t[cols])[:, 1]
        return pred

    if mode == "panel_ts":                         # same label, pooled
        t = tr[tr["y_ts"].notna()]
        if len(t) < 500 or t["y_ts"].nunique() < 2:
            return pd.Series(np.nan, index=te.index)
        m = lgb.LGBMClassifier(**common)
        m.fit(t[cols], t["y_ts"].astype(int))
        return pd.Series(m.predict_proba(te[cols])[:, 1], index=te.index)

    if mode == "panel_xs":                         # cross-sectional rank target
        m = lgb.LGBMRegressor(**common)
        m.fit(tr[cols], tr["y_xs"])
        return pd.Series(m.predict(te[cols]), index=te.index)

    if mode == "panel_rank":                       # lambdarank, day = query group
        t = tr.sort_values("date")
        grp = t.groupby("date").size().values
        rel = np.clip((t["y_xs"] * 8).astype(int), 0, 7)
        m = lgb.LGBMRanker(**{**common, "objective": "lambdarank"})
        m.fit(t[cols], rel, group=grp)
        return pd.Series(m.predict(te[cols]), index=te.index)

    raise ValueError(mode)


def run_arm(name, desc, panel, cols, mode):
    print(f"\n{'='*74}\n{name}  {desc}\n{'='*74}", flush=True)
    per_fold, all_ic = [], []
    for i, (t0, t1) in enumerate(folds(panel["date"]), 1):
        te = panel[(panel["date"] >= t0) & (panel["date"] <= t1)]
        cut = t0 - pd.Timedelta(days=EMBARGO * 2)      # calendar pad >= EMBARGO sessions
        tr = panel[panel["date"] < cut]
        if len(tr) < 2000 or len(te) < 200:
            continue
        try:
            pr = fit_predict(tr, te, cols, mode)
        except Exception as e:
            print(f"  fold {i:2d}: FAILED {type(e).__name__}: {e}", flush=True)
            continue
        d = te.assign(pred=pr).dropna(subset=["pred"])
        ic = rank_ic(d)
        if not len(ic):
            continue
        per_fold.append(ic.mean())
        all_ic.append(ic)
        print(f"  fold {i:2d}: {str(t0)[:10]}->{str(t1)[:10]}  n={len(d):6d}  "
              f"rank-IC={ic.mean():+.4f}", flush=True)

    if not all_ic:
        print("  no usable folds")
        return dict(arm=name, mean=np.nan, t=np.nan, pos=0, folds=0)

    ic = pd.concat(all_ic)
    mean = ic.mean()
    tstat = mean / (ic.std() / np.sqrt(len(ic))) if ic.std() else np.nan
    pos = sum(1 for f in per_fold if f > 0)
    print(f"  --> mean rank-IC={mean:+.4f}  t={tstat:+.2f}  "
          f"days={len(ic)}  folds +ve={pos}/{len(per_fold)}")
    return dict(arm=name, mean=mean, t=tstat, pos=pos, folds=len(per_fold))


def main():
    tickers = load_universe()
    print(f"universe: {len(tickers)} equities (crypto/ETFs excluded)", flush=True)

    import yfinance as yf
    spy = yf.download("SPY", start=START, auto_adjust=True, progress=False)
    if isinstance(spy.columns, pd.MultiIndex):
        spy.columns = spy.columns.droplevel(1)

    frames = download(tickers)
    print(f"\nbuilding features for {len(frames)} tickers ...", flush=True)
    panel = add_labels(build(frames, spy))
    print(f"panel: {len(panel):,} rows | {panel['ticker'].nunique()} tickers | "
          f"{str(panel['date'].min())[:10]} -> {str(panel['date'].max())[:10]} | "
          f"tails-label rows {panel['y_ts'].notna().sum():,} "
          f"({panel['y_ts'].notna().mean():.0%} of panel)", flush=True)

    xs = [c + "_xs" for c in FEATS]
    res = [
        run_arm("A", "time-series tails label + PER-TICKER  (control ~ production)",
                panel, FEATS, "per_ticker"),
        run_arm("B", "time-series tails label + pooled panel",
                panel, FEATS, "panel_ts"),
        run_arm("C", "cross-sectional resid-rank label + pooled panel",
                panel, FEATS, "panel_xs"),
        run_arm("D", "C + within-day normalised features",
                panel, xs, "panel_xs"),
        run_arm("E", "D + lambdarank (day = query group)",
                panel, xs, "panel_rank"),
    ]

    print(f"\n{'='*74}\nS3 RESULT — vs the PRE-REGISTERED threshold\n{'='*74}")
    print(f"{'arm':<4}{'mean rank-IC':>14}{'t':>9}{'folds +ve':>12}")
    for r in res:
        print(f"{r['arm']:<4}{r['mean']:>+14.4f}{r['t']:>9.2f}"
              f"{str(r['pos'])+'/'+str(r['folds']):>12}")

    best = max((r for r in res if np.isfinite(r["mean"])),
               key=lambda r: r["mean"], default=None)
    print(f"\nthreshold: mean rank-IC >= +0.02 AND >= 8 of 12 folds positive")
    if best is None:
        print("VERDICT: INCONCLUSIVE — no arm produced a usable read.")
        return 1
    ok = best["mean"] >= 0.02 and best["pos"] >= 8
    print(f"best arm: {best['arm']}  mean={best['mean']:+.4f}  "
          f"folds +ve={best['pos']}/{best['folds']}")
    if ok:
        print("\nVERDICT: S3 PASSES. The label/architecture was a real constraint.\n"
              "         Next step is wiring it into production -- a SEPARATE decision.\n"
              "         Do NOT treat this as evidence of tradeable alpha on its own:\n"
              "         it still has to clear WRC/SPA, which currently read 0.505/0.950.")
    else:
        print("\nVERDICT: S3 FAILS. Per HANDOFF §STOPPING RULE the conclusion is\n"
              "         THE FEATURES ARE THE BINDING CONSTRAINT -- stop adding features.\n"
              "         No second attempt at re-specifying the label.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
