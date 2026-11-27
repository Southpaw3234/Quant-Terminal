"""Volatility and loss bounds for ACTUARY's exposure section.

A RISK description, not a forecast of direction. Nothing here predicts a
return, ranks a name, or says what to do; it says how far a position has
tended to move, and how far it could plausibly move tomorrow and over the
next month, with the model and its limits stated on every line.

-----------------------------------------------------------------------------
WHY THIS SHAPE -- research/volatility_eval.py, run 2026-11-26

Four models, 90 stocks from the V29 universe plus SPY and IWM, 2021-2025,
refit only on past data:

  * GJR-GARCH beat a RiskMetrics EWMA on one-day variance for 82-84% of
    stocks that trade nearly every day -- and on SPY and IWM.
  * On thinly traded names (>= 6% of days with no price change, about half
    this universe) it beat EWMA on 53% and sometimes failed catastrophically:
    after a quiet stretch it forecast almost no volatility, just before a
    large move.
  * 23% of fits came out with persistence >= 1 (volatility that never
    settles back), and 73% of stocks had a Student-t nu below 4: tails so
    heavy that a standard deviation barely describes them.
  * Normal-quantile loss bounds were breached about twice as often as they
    claim (1.8% against 1%).

So, in code below:

  1. GJR-GARCH is used ONLY where it was reliable: >= MIN_OBS sessions of
     history, < MAX_ZERO_SHARE zero-return days, and a fit that passes the
     sanity gates (persistence < MAX_PERSIST, nu > MIN_NU, converged). Its
     variance is floored at FLOOR_FRAC of the trailing year's variance, so
     it cannot forecast near-zero volatility.
  2. Everywhere else: EWMA, lambda 0.94.
  3. Loss bounds use FILTERED HISTORICAL SIMULATION -- the empirical
     quantile of the name's own standardized returns -- never a normal or
     a fitted t quantile. Whatever model sets the scale, the shape of the
     tail comes from what the stock has actually done.

Every number carries `model`, `n` and the reason a fallback was taken. A
name with too little history gets `None` and says so, never a zero.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252
LAMBDA = 0.94                  # RiskMetrics
MIN_OBS = 1000                 # about four years of sessions, for GJR-GARCH
MIN_OBS_EWMA = 250             # about one year, for anything at all
MAX_ZERO_SHARE = 0.06          # share of no-change sessions above which GJR fails
MAX_PERSIST = 0.999            # alpha + beta + gamma/2
MIN_NU = 2.5                   # Student-t degrees of freedom
FLOOR_FRAC = 0.25              # variance floor, x the trailing year's variance
FHS_WINDOW = 1000              # standardized returns used for the tail quantile
HORIZON = 21                   # sessions, about a month
TAIL = 0.01                    # 99% one-day loss bound
BAND = 0.05                    # 95% one-month band (two-sided 90%: 5th and 95th)


# ── inputs ───────────────────────────────────────────────────────────────────

def log_returns_pct(closes: pd.Series) -> pd.Series:
    """100 x log return per session. Non-positive or missing closes dropped."""
    px = pd.Series(closes, dtype="float64").dropna()
    px = px[px > 0]
    return (100 * np.log(px).diff()).dropna()


def diagnostics(r: pd.Series) -> dict:
    """What this return history can support, and why."""
    n = int(len(r))
    zero = float((r == 0).mean()) if n else 1.0
    reasons = []
    if n < MIN_OBS:
        reasons.append(f"{n} sessions of history, fewer than the {MIN_OBS} "
                       f"GJR-GARCH needs")
    if zero >= MAX_ZERO_SHARE:
        reasons.append(f"{zero:.0%} of sessions had no price change — the "
                       f"stock trades too thinly for GARCH (limit "
                       f"{MAX_ZERO_SHARE:.0%})")
    return {"n": n, "zero_share": round(zero, 4),
            "garch_eligible": not reasons, "garch_reasons": reasons,
            "enough_for_anything": n >= MIN_OBS_EWMA}


# ── variance paths. h[t] is the variance of r[t], known at t-1. ──────────────

def ewma_var(r: pd.Series, lam: float = LAMBDA) -> pd.Series:
    """One-step-ahead EWMA variance, aligned so h[t] uses data to t-1."""
    return (r ** 2).ewm(alpha=1 - lam, adjust=False).mean().shift(1)


def ewma_next(r: pd.Series, lam: float = LAMBDA) -> float:
    """Variance forecast for the session AFTER the last one in `r`."""
    return float((r ** 2).ewm(alpha=1 - lam, adjust=False).mean().iloc[-1])


def floor_series(h: pd.Series, r: pd.Series) -> pd.Series:
    """max(h, FLOOR_FRAC x trailing-year variance known at t-1)."""
    base = (r ** 2).rolling(TRADING_DAYS, min_periods=60).mean().shift(1)
    # fmax, not maximum: where the trailing year is not yet available the
    # floor is MISSING, and np.maximum would turn the forecast into NaN --
    # erasing it rather than raising it. validate_volatility.py pins this.
    return pd.Series(np.fmax(h.values, FLOOR_FRAC * base.reindex(h.index).values),
                     index=h.index)


def fit_gjr(r: pd.Series) -> dict:
    """GJR-GARCH(1,1,1), Student-t, constant mean. -> {ok, params, reason}.

    🔑 A fit is REFUSED, not used, when it would mislead: persistence at or
    above MAX_PERSIST means volatility never settles, so a one-month figure
    is meaningless; nu at or below MIN_NU means tails so heavy that the
    variance it estimates barely exists.
    """
    from arch import arch_model                     # lazy: heavy import
    try:
        res = arch_model(r, mean="Constant", vol="GARCH", p=1, o=1, q=1,
                         dist="t").fit(disp="off", options={"maxiter": 300})
    except Exception as e:                          # noqa: BLE001
        return {"ok": False, "params": None, "reason": f"fit failed: {type(e).__name__}"}
    p = res.params
    pers = float(p["alpha[1]"] + p["beta[1]"] + 0.5 * p["gamma[1]"])
    nu = float(p["nu"])
    if res.convergence_flag != 0 or not math.isfinite(res.loglikelihood):
        return {"ok": False, "params": None, "reason": "fit did not converge"}
    if pers >= MAX_PERSIST:
        return {"ok": False, "params": None,
                "reason": f"persistence {pers:.3f} — volatility would never "
                          f"settle, so the fit is not used"}
    if nu <= MIN_NU:
        return {"ok": False, "params": None,
                "reason": f"tail parameter nu {nu:.2f} — too heavy-tailed for "
                          f"a variance model, so the fit is not used"}
    return {"ok": True, "params": p, "persistence": pers, "nu": nu,
            "reason": "ok"}


def gjr_paths(r: pd.Series, params, start: int, horizon: int = HORIZON):
    """With FIXED params: (h1, h_sum) where h1[t] is the one-step variance of
    r[t] and h_sum[t] the summed variance of r[t..t+horizon-1], both made at
    t-1, for t >= start."""
    from arch import arch_model
    fx = arch_model(r, mean="Constant", vol="GARCH", p=1, o=1, q=1,
                    dist="t").fix(params)
    f = fx.forecast(horizon=horizon, start=max(start - 1, 0), reindex=False)
    v = f.variance
    idx = r.index[start:start + len(v)]
    h1 = pd.Series(v.iloc[:len(idx), 0].values, index=idx)
    hs = pd.Series(v.iloc[:len(idx)].sum(axis=1).values, index=idx)
    return h1, hs


# ── the tail ────────────────────────────────────────────────────────────────

def fhs_quantile(z: pd.Series, q: float, window: int = FHS_WINDOW) -> float:
    """Empirical q-quantile of the last `window` standardized returns."""
    zz = pd.Series(z).dropna()
    zz = zz[np.isfinite(zz)].iloc[-window:]
    return float(np.quantile(zz, q)) if len(zz) >= 100 else float("nan")


# ── the one call ACTUARY makes ──────────────────────────────────────────────

def describe(closes: pd.Series) -> dict:
    """Volatility and loss bounds for one name, as of its last close.

    -> {status, model, n, zero_share, vol_1d_pct, vol_ann_pct,
        vol_21d_ann_pct, loss_1d_99_pct, band_21d_pct: [lo, hi],
        realized_63d_ann_pct, fallback_reason, how_to_read}

    Percent figures are of the position's value. `status` is "ok" or
    "insufficient_history"; in the second case every figure is None.
    """
    r = log_returns_pct(closes)
    d = diagnostics(r)
    base = {"n": d["n"], "zero_share": d["zero_share"]}
    if not d["enough_for_anything"]:
        return dict(base, status="insufficient_history", model=None,
                    vol_1d_pct=None, vol_ann_pct=None, vol_21d_ann_pct=None,
                    loss_1d_99_pct=None, band_21d_pct=None,
                    realized_63d_ann_pct=None,
                    fallback_reason=f"{d['n']} sessions of history, fewer than "
                                    f"the {MIN_OBS_EWMA} needed for any figure")

    model, reason = "EWMA", "; ".join(d["garch_reasons"])
    h_path = ewma_var(r)
    h1 = ewma_next(r)
    h21 = HORIZON * h1
    if d["garch_eligible"]:
        fit = fit_gjr(r)
        if fit["ok"]:
            from arch import arch_model
            fc = arch_model(r, mean="Constant", vol="GARCH", p=1, o=1, q=1,
                            dist="t").fix(fit["params"]).forecast(
                horizon=HORIZON, reindex=False)
            v = fc.variance.iloc[-1]
            flo = FLOOR_FRAC * float((r ** 2).iloc[-TRADING_DAYS:].mean())
            h1 = max(float(v.iloc[0]), flo)
            h21 = max(float(v.sum()), HORIZON * flo)
            fixed = arch_model(r, mean="Constant", vol="GARCH", p=1, o=1, q=1,
                               dist="t").fix(fit["params"])
            h_path = floor_series(
                pd.Series(fixed.conditional_volatility ** 2, index=r.index)
                .shift(0), r)
            model, reason = "GJR-GARCH", ""
        else:
            reason = fit["reason"]

    z = r / np.sqrt(h_path)
    q = fhs_quantile(z, TAIL)
    s1 = math.sqrt(h1)
    loss = None if not math.isfinite(q) else (math.exp(q * s1 / 100) - 1) * 100
    s21 = math.sqrt(h21)
    # One-month band: the summed log return is close to normal by the
    # central limit theorem even when single days are not. Stated as such.
    zb = 1.6448536269514722
    band = [(math.exp(-zb * s21 / 100) - 1) * 100,
            (math.exp(zb * s21 / 100) - 1) * 100]
    rv = float(r.iloc[-63:].std(ddof=1) * math.sqrt(TRADING_DAYS))
    return dict(
        base, status="ok", model=model,
        vol_1d_pct=round(s1, 3),
        vol_ann_pct=round(s1 * math.sqrt(TRADING_DAYS), 1),
        vol_21d_ann_pct=round(s21 * math.sqrt(TRADING_DAYS / HORIZON), 1),
        loss_1d_99_pct=None if loss is None else round(loss, 2),
        band_21d_pct=[round(band[0], 1), round(band[1], 1)],
        realized_63d_ann_pct=round(rv, 1),
        fallback_reason=reason or None,
        how_to_read=(
            f"How far this has tended to move, not which way. One day in a "
            f"hundred, the loss has been worse than the 1-day figure "
            f"({model} scale, the stock's own tail). The one-month band holds "
            f"about 90% of outcomes if conditions stay as they are; it is "
            f"not a target."))
