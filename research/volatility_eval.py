#!/usr/bin/env python3
"""Out-of-sample test of the volatility policy that qt/volatility.py ships.

Run 1 (2026-11-26, seed 20261126) compared EWMA, a 63-day rolling variance,
GARCH and GJR-GARCH on 90 stocks and chose the policy's thresholds from what
it found. This file tests that policy on a FRESH sample -- different seed,
the run-1 tickers excluded -- so the thresholds are checked rather than
reused on the data that suggested them.

Policies, each forecasting every session 2021-01-04 .. 2025-08-29 with
parameters estimated only on earlier data (GJR refit every 21 sessions):

  A  EWMA-N     EWMA variance, normal quantile               (the baseline)
  B  EWMA-FHS   EWMA variance, filtered-historical quantile
  C  GJR-t      GJR-GARCH, its own Student-t quantile, NO gates, NO floor
  P  POLICY     what ships: guarded + floored GJR with an FHS quantile where
                the stock is eligible, B everywhere else

Scores: 1-day QLIKE (median, win rates, count of catastrophic stocks),
21-day QLIKE, and the 99% one-day loss bound's breach rate with a Kupiec
test. A RISK model on price data: it predicts no return, is not a
pre-registered specification, and spends no K.

    python research/volatility_eval.py            # 150 drawn, seed 7
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
import warnings

import numpy as np
import pandas as pd
from scipy import stats

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

from qt import tickers as qtk
from qt import volatility as vol

SEED = int(os.environ.get("QT_VOL_SEED", "7"))
EXCLUDE_SEED = 20261126           # run 1's sample, excluded
N = int(os.environ.get("QT_VOL_N", "150"))
EVAL_START = pd.Timestamp("2021-01-04")
REFIT = 21
H = vol.HORIZON
OUT = os.environ.get("QT_VOL_OUT", "research/volatility_eval_results.json")
CATASTROPHE = 50.0                # a per-stock mean QLIKE this high is a blow-up


def kupiec(n, x, p=vol.TAIL):
    if n == 0:
        return float("nan")
    if x == 0:
        return float(stats.chi2.sf(-2 * n * math.log(1 - p), 1))
    ph = x / n
    lr = -2 * ((n - x) * math.log(1 - p) + x * math.log(p)
               - (n - x) * math.log(1 - ph) - x * math.log(ph))
    return float(stats.chi2.sf(lr, 1))


def evaluate(r: pd.Series) -> dict:
    idx = r.index
    e0 = int(idx.searchsorted(EVAL_START))
    r2 = r ** 2
    ew = vol.ewma_var(r)
    elig = vol.diagnostics(r.iloc[:e0])["garch_eligible"]

    f = {k: pd.Series(np.nan, index=idx) for k in
         ("A_h", "A_q", "B_q", "C_h", "C_q", "P_h", "P_q", "C_h21", "P_h21")}
    gates = {"blocks": 0, "gjr_used": 0, "refused": {}}
    for s in range(e0, len(idx), REFIT):
        end = min(s + REFIT, len(idx))
        blk = idx[s:end]
        gates["blocks"] += 1
        # A, B: EWMA scale. FHS tail from EWMA-standardized returns to s-1.
        zq_ew = vol.fhs_quantile((r / np.sqrt(ew)).iloc[:s], vol.TAIL)
        f["A_q"][blk] = stats.norm.ppf(vol.TAIL)
        f["B_q"][blk] = zq_ew
        # C: unguarded GJR, own t quantile.
        from arch import arch_model
        try:
            fc = arch_model(r.iloc[:s], mean="Constant", vol="GARCH", p=1,
                            o=1, q=1, dist="t").fit(disp="off",
                                                    options={"maxiter": 300})
            h1, hs = vol.gjr_paths(r.iloc[:end], fc.params, s)
            nu = float(fc.params["nu"])
            f["C_h"][h1.index] = h1
            f["C_h21"][hs.index] = hs
            f["C_q"][blk] = (stats.t.ppf(vol.TAIL, nu) * math.sqrt((nu - 2) / nu)
                             if nu > 2 else np.nan)
        except Exception:                                # noqa: BLE001
            pass
        # P: the shipping policy.
        used = False
        if elig:
            g = vol.fit_gjr(r.iloc[:s])
            if g["ok"]:
                h1, hs = vol.gjr_paths(r.iloc[:end], g["params"], s)
                h1 = vol.floor_series(h1, r.iloc[:end]).reindex(h1.index)
                base = (r2.rolling(vol.TRADING_DAYS, min_periods=60).mean()
                        .shift(1)).reindex(hs.index)
                hs = pd.Series(np.fmax(hs.values, (H * vol.FLOOR_FRAC * base).values), index=hs.index)
                fixed = arch_model(r.iloc[:s], mean="Constant", vol="GARCH",
                                   p=1, o=1, q=1, dist="t").fix(g["params"])
                hin = vol.floor_series(pd.Series(
                    fixed.conditional_volatility ** 2, index=idx[:s]), r.iloc[:s])
                f["P_h"][h1.index] = h1
                f["P_h21"][hs.index] = hs
                f["P_q"][blk] = vol.fhs_quantile(r.iloc[:s] / np.sqrt(hin),
                                                 vol.TAIL)
                used = True
                gates["gjr_used"] += 1
            else:
                k = g["reason"].split(" ")[0]
                gates["refused"][k] = gates["refused"].get(k, 0) + 1
        if not used:
            f["P_h"][blk] = ew[blk]
            f["P_h21"][blk] = H * ew[blk]
            f["P_q"][blk] = zq_ew

    ev = idx[e0:]
    real21 = r2[::-1].rolling(H).sum()[::-1]
    out = {"eligible": bool(elig), "gates": gates, "n_eval": int(len(ev)),
           "zero_share": round(float((r.iloc[:e0] == 0).mean()), 4), "m": {}}
    specs = {"A": (ew, f["A_q"], H * ew), "B": (ew, f["B_q"], H * ew),
             "C": (f["C_h"], f["C_q"], f["C_h21"]),
             "P": (f["P_h"], f["P_q"], f["P_h21"])}
    for name, (h, q, h21) in specs.items():
        h, q, h21 = h.reindex(ev), q.reindex(ev), h21.reindex(ev)
        y, y21 = r2.reindex(ev), real21.reindex(ev)
        ok = h.notna() & (h > 0)
        ql = float((np.log(h[ok]) + y[ok] / h[ok]).mean())
        m21 = ok & h21.notna() & (h21 > 0) & y21.notna()
        m21 &= pd.Series(np.arange(len(ev)) % 5 == 0, index=ev)
        ql21 = float((np.log(h21[m21]) + y21[m21] / h21[m21]).mean())
        okq = ok & q.notna()
        bound = q[okq] * np.sqrt(h[okq])
        x = int((r.reindex(ev)[okq] < bound).sum())
        n = int(okq.sum())
        out["m"][name] = {"qlike": ql, "qlike21": ql21, "n": n,
                          "exceed": x / n if n else None,
                          "kupiec_p": kupiec(n, x)}
    return out


def main() -> None:
    panel = qtk.read_csv("data/universe/v29_universe_pit.csv")
    panel["date"] = pd.to_datetime(panel["date"])
    pool = sorted(set(panel.loc[panel["date"] >= "2020-12-31", "ticker"]))
    random.seed(EXCLUDE_SEED)
    run1 = set(random.sample(pool, 150))
    fresh = [t for t in pool if t not in run1]
    random.seed(SEED)
    sample = random.sample(fresh, N) + ["SPY", "IWM"]
    print(f"[vol] pool {len(pool)}, run-1 excluded {len(run1)}, drawn {N} "
          f"(seed {SEED}) + SPY, IWM")

    import yfinance as yf
    raw = yf.download(sample, start="2015-06-01", end="2025-09-01",
                      auto_adjust=True, progress=False, threads=True,
                      group_by="column")
    close = raw["Close"]
    res, skipped, t0 = {}, {}, time.time()
    for i, tk in enumerate(sample, 1):
        if tk not in close.columns:
            skipped[tk] = "no price data"
            continue
        r = vol.log_returns_pct(close[tk])
        if len(r) < 1200 or r.index[0] > pd.Timestamp("2019-06-01"):
            skipped[tk] = "history too short to evaluate from 2021"
            continue
        try:
            res[tk] = evaluate(r)
        except Exception as e:                            # noqa: BLE001
            skipped[tk] = f"error {type(e).__name__}: {e}"
        if i % 25 == 0:
            print(f"[vol] {i}/{len(sample)}  {time.time() - t0:.0f}s")

    stocks = {k: v for k, v in res.items() if k not in ("SPY", "IWM")}
    print(f"\n[vol] evaluated {len(stocks)} stocks; skipped {len(skipped)}")

    def summary(group, label):
        if not group:
            return
        print(f"\n── {label}: {len(group)} stocks")
        for m in "ABCP":
            q = np.array([v["m"][m]["qlike"] for v in group.values()])
            q21 = np.array([v["m"][m]["qlike21"] for v in group.values()])
            ex = np.array([v["m"][m]["exceed"] for v in group.values()
                           if v["m"][m]["exceed"] is not None])
            kp = np.array([v["m"][m]["kupiec_p"] for v in group.values()
                           if v["m"][m]["exceed"] is not None])
            beatB = np.mean([v["m"][m]["qlike"] < v["m"]["B"]["qlike"]
                             for v in group.values()])
            print(f"  {m}  QLIKE median {np.nanmedian(q):7.4f}  "
                  f"blow-ups {int(np.sum(q > CATASTROPHE)):>2}  "
                  f"beats EWMA {beatB:4.0%}  | 21d median "
                  f"{np.nanmedian(q21):7.4f}  | 99% breach median "
                  f"{np.median(ex):.2%}  Kupiec-rejected {np.mean(kp < 0.05):4.0%}")
    summary(stocks, "ALL")
    summary({k: v for k, v in stocks.items() if v["eligible"]}, "GARCH-ELIGIBLE")
    summary({k: v for k, v in stocks.items() if not v["eligible"]},
            "NOT ELIGIBLE (policy = EWMA-FHS)")
    for tk in ("SPY", "IWM"):
        if tk in res:
            v = res[tk]["m"]
            print(f"  {tk}: " + "  ".join(
                f"{m} q{v[m]['qlike']:.3f} breach {v[m]['exceed']:.2%}"
                for m in "ABCP"))
    g = [v["gates"] for v in stocks.values() if v["eligible"]]
    if g:
        used = sum(x["gjr_used"] for x in g)
        blocks = sum(x["blocks"] for x in g)
        refused = {}
        for x in g:
            for k, n in x["refused"].items():
                refused[k] = refused.get(k, 0) + n
        print(f"\n  eligible stocks: GJR used in {used} of {blocks} refit "
              f"blocks ({used / blocks:.0%}); refused: {refused}")
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    json.dump({"seed": SEED, "results": res, "skipped": skipped},
              open(OUT, "w", encoding="utf-8"), indent=1)
    print(f"\n[vol] wrote {OUT}. No return predicted, K unchanged.")


if __name__ == "__main__":
    main()
