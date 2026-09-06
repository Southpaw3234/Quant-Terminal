"""Liquidity — estimating the bid-ask spread from daily bars.

WHY NOT JUST QUOTE THE MARKET. The first attempt at this (probe_spreads.py,
run 34050275750) asked the broker for live quotes and got a median of 3,336
basis points -- a 33% spread -- and reported roughly the SAME figure for every
liquidity bucket from $50k a day to $5M a day. Identical spreads across a
hundredfold range of liquidity is not a finding, it is a broken measurement:
the run was on a Sunday, and a closed-market quote is stale, one-sided, or
both.

Two deeper problems with quoting the market at all, which is why this module
exists rather than a retry:

  1. A live quote describes TODAY. The declared window runs 2015-2025 and
     spreads in this band have narrowed over it, so today's number -- even a
     correct one -- is the wrong number for the backtest's cost assumption.
  2. It requires the market to be open, which makes the measurement depend on
     when the job happens to run.

THE CORWIN-SCHULTZ ESTIMATOR solves both. It recovers the spread from daily
HIGH and LOW prices, on the reasoning that the high is usually a buy at the
ask and the low a sell at the bid, so the high-low range contains both the
true volatility and the spread. Volatility scales with the time interval and
the spread does not, so comparing one-day ranges against the two-day range
separates them.

    beta  = mean of (ln(H/L))^2 over two consecutive single days
    gamma = (ln(H2/L2))^2 for the two days combined
    alpha = (sqrt(2*beta) - sqrt(beta)) / (3 - 2*sqrt(2)) - sqrt(gamma / (3 - 2*sqrt(2)))
    S     = 2 * (e^alpha - 1) / (1 + e^alpha)

Corwin and Schultz (2012), "A Simple Way to Estimate Bid-Ask Spreads from
Daily High and Low Prices".

⚠️ ITS KNOWN WEAKNESS, stated rather than discovered: the estimator produces
NEGATIVE spreads on a meaningful fraction of days, which is nonsense as a
spread and is the estimator telling you the two-day range was smaller than the
one-day ranges implied. The published convention sets those to zero before
averaging. That convention BIASES THE RESULT DOWNWARD, so a spread estimated
this way is, if anything, optimistic -- which is the safe direction when the
number is being used to check whether a declared cost assumption is too low.

Pure functions. No network, no files.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

_K = 3.0 - 2.0 * math.sqrt(2.0)


def corwin_schultz_daily(high: pd.Series, low: pd.Series) -> pd.Series:
    """Per-pair spread estimates as a FRACTION of price, negatives kept.

    Negatives are preserved here and clipped by the caller, so a caller can
    see how often the estimator misbehaves rather than having it hidden.
    """
    h = pd.to_numeric(high, errors="coerce")
    l = pd.to_numeric(low, errors="coerce")
    df = pd.DataFrame({"h": h, "l": l}).dropna()
    df = df[(df["h"] > 0) & (df["l"] > 0) & (df["h"] >= df["l"])]
    if len(df) < 2:
        return pd.Series(dtype="float64")

    hv, lv = df["h"].to_numpy(dtype=float), df["l"].to_numpy(dtype=float)
    r1 = np.log(hv[:-1] / lv[:-1]) ** 2          # day t
    r2 = np.log(hv[1:] / lv[1:]) ** 2            # day t+1
    beta = r1 + r2

    h2 = np.maximum(hv[:-1], hv[1:])             # two-day high
    l2 = np.minimum(lv[:-1], lv[1:])             # two-day low
    gamma = np.log(h2 / l2) ** 2

    with np.errstate(invalid="ignore", divide="ignore"):
        alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _K - np.sqrt(gamma / _K)
        s = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    return pd.Series(s, index=df.index[1:], dtype="float64").replace(
        [np.inf, -np.inf], np.nan).dropna()


def spread_estimate(high: pd.Series, low: pd.Series,
                    clip_negative: bool = True) -> dict:
    """-> {spread_bps, n_pairs, pct_negative}. The round-trip cost of crossing.

    A round trip pays half the spread on entry and half on exit, so one full
    spread IS the cost of crossing both ways, and `spread_bps` is directly
    comparable with a declared round-trip assumption.
    """
    raw = corwin_schultz_daily(high, low)
    if raw.empty:
        return {"spread_bps": float("nan"), "n_pairs": 0, "pct_negative": float("nan")}
    neg = float((raw < 0).mean())
    used = raw.clip(lower=0.0) if clip_negative else raw
    return {"spread_bps": float(used.mean() * 10_000.0),
            "n_pairs": int(len(raw)),
            "pct_negative": neg}
