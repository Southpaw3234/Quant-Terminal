#!/usr/bin/env python3
"""Pure-logic validation for qt/volatility.py. No network: synthetic prices.

The class of wrongness this file exists to prevent: **a risk figure that
reads as safer than it is.** Three shapes, each seen in the evaluation that
chose this design:

  1. GARCH forecasting near-zero volatility for a thinly traded stock after
     a quiet stretch, just before it moved
  2. a fit whose volatility never settles, so a one-month figure is noise
  3. a normal-quantile loss bound, breached about twice as often as claimed

and one that is cheaper to ship: a number for a name with too little history
to support one.
"""

from __future__ import annotations

import ast
import inspect
import math
import sys
import warnings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from qt import volatility as vol

PASS, FAIL = [], []


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def gjr_prices(n=1800, seed=1, omega=0.05, alpha=0.05, gamma=0.08, beta=0.88,
               nu=6.0, start=50.0):
    """Prices from a known GJR-GARCH-t process (returns in percent)."""
    rng = np.random.default_rng(seed)
    z = rng.standard_t(nu, n) * math.sqrt((nu - 2) / nu)
    h = np.empty(n); r = np.empty(n)
    h[0] = omega / (1 - alpha - beta - gamma / 2)
    for t in range(n):
        if t:
            neg = 1.0 if r[t - 1] < 0 else 0.0
            h[t] = omega + (alpha + gamma * neg) * r[t - 1] ** 2 + beta * h[t - 1]
        r[t] = math.sqrt(h[t]) * z[t]
    idx = pd.bdate_range("2018-01-02", periods=n)
    return pd.Series(start * np.exp(np.cumsum(r) / 100), index=idx), r, h


def test_model_choice():
    print("\nthe model is chosen by what the history can support")
    px, _, _ = gjr_prices()
    d = vol.describe(px)
    check("a-liquid-long-history-uses-GJR", d["model"] == "GJR-GARCH"
          and d["status"] == "ok" and d["fallback_reason"] is None, f"{d['model']}")

    thin = px.copy()
    rng = np.random.default_rng(3)
    stale = rng.random(len(thin)) < 0.15          # 15% of days unchanged
    vals = thin.values.copy()
    for i in range(1, len(vals)):
        if stale[i]:
            vals[i] = vals[i - 1]
    d = vol.describe(pd.Series(vals, index=thin.index))
    check("🔑-a-thinly-traded-stock-falls-back-to-EWMA",
          d["model"] == "EWMA" and "trades too thinly" in (d["fallback_reason"] or ""),
          "about half this universe; GARCH blew up on these in the evaluation")

    d = vol.describe(px.iloc[-600:])
    check("a-short-history-falls-back-to-EWMA-and-says-why",
          d["model"] == "EWMA" and "fewer than the 1000" in (d["fallback_reason"] or ""))

    d = vol.describe(px.iloc[-120:])
    check("🔑-too-little-history-is-None-not-zero",
          d["status"] == "insufficient_history" and d["model"] is None
          and all(d[k] is None for k in ("vol_1d_pct", "vol_ann_pct",
                                         "loss_1d_99_pct", "band_21d_pct")),
          "a name with no risk figure must not read as a name with no risk")


def test_gates():
    print("\na fit that would mislead is refused")
    from arch import arch_model
    # A near-integrated series: persistence close to 1 in the generator.
    px, _, _ = gjr_prices(seed=5, omega=0.002, alpha=0.08, gamma=0.0, beta=0.9195)
    r = vol.log_returns_pct(px)
    g = vol.fit_gjr(r)
    pers = None
    try:
        p = arch_model(r, mean="Constant", vol="GARCH", p=1, o=1, q=1,
                       dist="t").fit(disp="off").params
        pers = p["alpha[1]"] + p["beta[1]"] + 0.5 * p["gamma[1]"]
    except Exception:                                 # noqa: BLE001
        pass
    if pers is not None and pers >= vol.MAX_PERSIST:
        check("🔑-persistence-at-or-above-the-gate-is-refused",
              not g["ok"] and "persistence" in g["reason"], g["reason"])
    else:
        check("the-gate-holds-for-whatever-this-fit-produced",
              g["ok"] == (pers is not None and pers < vol.MAX_PERSIST),
              f"persistence {pers}")
    src = inspect.getsource(vol.fit_gjr)
    check("the-gates-are-in-the-fit", "MAX_PERSIST" in src and "MIN_NU" in src
          and "convergence_flag" in src)
    check("gate-values-are-pinned",
          (vol.MAX_PERSIST, vol.MIN_NU, vol.MIN_OBS, vol.MAX_ZERO_SHARE,
           vol.FLOOR_FRAC, vol.LAMBDA) == (0.999, 2.5, 1000, 0.06, 0.25, 0.94),
          "these were chosen on run 1 and checked out of sample; changing one "
          "means re-running research/volatility_eval.py")


def test_floor_and_paths():
    print("\nvariance paths, alignment and the floor")
    r = pd.Series([1.0, -2.0, 0.5, 3.0, -1.0] * 80,
                  index=pd.bdate_range("2020-01-01", periods=400))
    h = vol.ewma_var(r)
    check("ewma-is-one-step-ahead", math.isnan(h.iloc[0])
          and abs(h.iloc[1] - 1.0) < 1e-12,
          "h[t] may use returns only up to t-1")
    check("ewma-next-matches-the-recursion",
          abs(vol.ewma_next(r.iloc[:2]) - (0.94 * 1.0 + 0.06 * 4.0)) < 1e-12)
    tiny = pd.Series(1e-9, index=r.index)
    fl = vol.floor_series(tiny, r)
    base = (r ** 2).rolling(252, min_periods=60).mean().shift(1)
    check("🔑-the-floor-stops-a-near-zero-forecast",
          bool(((fl[base.notna()] - 0.25 * base[base.notna()]).abs() < 1e-9).all()),
          "GARCH forecast almost nothing after quiet stretches, then the stock moved")
    fh = vol.floor_series(h, r)
    ok = h.notna()
    check("the-floor-never-lowers-a-forecast",
          bool((fh[ok] >= h[ok] - 1e-12).all()))
    check("🔑-a-missing-floor-does-not-erase-a-forecast",
          bool(fh[ok].notna().all()) and abs(fh.iloc[5] - h.iloc[5]) < 1e-12,
          "np.maximum with a NaN floor returned NaN for the first 60 sessions")
    z = pd.Series(np.linspace(-3, 3, 1000))
    check("fhs-quantile-is-empirical",
          abs(vol.fhs_quantile(z, 0.01) - np.quantile(z, 0.01)) < 1e-12)
    check("fhs-needs-enough-history",
          math.isnan(vol.fhs_quantile(z.iloc[:50], 0.01)))
    check("fhs-uses-only-the-window",
          abs(vol.fhs_quantile(pd.concat([pd.Series([-100.0] * 50), z]), 0.01)
              - np.quantile(z, 0.01)) < 1e-12)


def test_outputs():
    print("\nwhat describe() returns")
    px, r_true, h_true = gjr_prices(seed=11)
    d = vol.describe(px)
    check("loss-bound-is-a-loss", d["loss_1d_99_pct"] < 0)
    lo, hi = d["band_21d_pct"]
    check("band-straddles-zero-and-is-log-asymmetric",
          lo < 0 < hi and hi > -lo, f"{lo} / {hi}")
    check("annualised-is-daily-times-root-252",
          abs(d["vol_ann_pct"] - d["vol_1d_pct"] * math.sqrt(252)) < 0.1)
    implied = math.sqrt(h_true[-1]) * math.sqrt(252)
    check("the-scale-is-in-the-right-place",
          0.4 * implied < d["vol_ann_pct"] < 2.5 * implied,
          f"{d['vol_ann_pct']}% vs the generator's {implied:.1f}%")
    check("realized-is-reported-beside-the-forecast",
          d["realized_63d_ann_pct"] > 0)
    check("every-figure-says-how-to-read-it",
          "not which way" in d["how_to_read"] and "not a target" in d["how_to_read"])
    banned = ("buy", "sell", "hold", "target_price", "score", "signal",
              "rating", "recommend", "confidence")
    check("🔑-no-key-is-a-verdict",
          not any(b in k.lower() for k in d for b in banned), f"{sorted(d)}")

    imported = set()
    for n in ast.walk(ast.parse(inspect.getsource(vol))):
        if isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module)
    for m in ("qt.referee", "qt.wrc", "qt.calendar_time", "qt.prices",
              "yfinance", "requests", "alpaca"):
        check(f"does-not-import-{m}", m not in imported,
              "a risk description reads prices it is given; it fetches none")


def main():
    print("=" * 70)
    print("qt.volatility — synthetic prices, no network")
    print("=" * 70)
    test_model_choice()
    test_gates()
    test_floor_and_paths()
    test_outputs()
    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, no direction predicted.")


if __name__ == "__main__":
    main()
