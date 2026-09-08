"""Scoring — V29 value model. Value, risk, and the tier the operator turns.

Pure functions only: no network, no files, no prices fetched. Everything here
takes numbers already known point-in-time and returns numbers. That is what
makes it testable against hand-computed answers, and it is why the layer that
FETCHES data (extract_fundamentals.py) is deliberately somewhere else.

THE TWO HALVES
--------------
VALUE decides what to buy: EBIT / enterprise value, with a quality gate that
removes companies losing money or carrying leverage that makes the ratio
meaningless.

RISK decides how much of the eligible set the operator is willing to touch.
Every name gets a composite score built from five components, and the score is
turned into a tercile — low, moderate, high. The tier is a KNOB, not a
discovery.

⚠️ THE KNOB IS ALSO A MULTIPLE-TESTING PROBLEM, and pretending otherwise
would undo the point of the pre-registration. Three tiers are three
strategies. docs/V29_PREREGISTRATION.md declares MODERATE as the primary and
the only one that can pass; low and high are reported as description. Reading
all three as candidates means charging all three in the correction.

WHY THESE FIVE RISK COMPONENTS
-------------------------------
Not fitted, not optimised, and deliberately boring. Each is a different way a
small-cap position hurts you, so that a name has to be safe in several
distinct senses to score low:

  volatility     how violently it moves at all
  drawdown       how far it has actually fallen, which volatility understates
                 for names that fall and stay down
  leverage       whether the balance sheet survives a bad year
  illiquidity    whether you can leave — the risk a backtest never shows,
                 because a backtest always fills
  thin equity    equity over assets: how little cushion sits under the debt

Each is converted to a cross-sectional percentile at the rebalance date and
averaged. Percentiles, not raw units, because volatility in percent and
leverage as a ratio cannot be added and there is no defensible way to weight
them in raw form. Equal weights across five components is a choice, and it is
declared rather than tuned.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

RISK_COMPONENTS = ("volatility", "drawdown", "leverage", "illiquidity", "thin_equity")
TIERS = ("low", "moderate", "high")


# ─────────────────────────────────────────────────────────────────── value

def enterprise_value(price, shares, debt, cash) -> float:
    """Market cap + debt - cash.

    ⚠️ `debt` is LONG-TERM debt only, because that is what the XBRL tags
    reliably supply across this universe. Short-term borrowings are therefore
    missing, which UNDERSTATES enterprise value and so OVERSTATES EBIT/EV for
    companies funding themselves on revolvers. Declared as an approximation
    rather than silently absorbed.
    """
    for v in (price, shares):
        if v is None or not np.isfinite(v) or v <= 0:
            return float("nan")
    d = 0.0 if debt is None or not np.isfinite(debt) else float(debt)
    c = 0.0 if cash is None or not np.isfinite(cash) else float(cash)
    return float(price) * float(shares) + d - c


def ebit_ev(op_income, ev) -> float:
    """The primary value metric. Higher is cheaper.

    Returns NaN on a non-positive enterprise value: a company whose cash
    exceeds its market cap plus debt produces a negative denominator, which
    would flip the sign and rank it as the most attractive name in the
    universe. That is an arithmetic artefact, not a bargain.
    """
    if op_income is None or ev is None:
        return float("nan")
    if not np.isfinite(op_income) or not np.isfinite(ev) or ev <= 0:
        return float("nan")
    return float(op_income) / float(ev)


def passes_quality(op_income, equity, debt, max_debt_to_equity: float = 2.0) -> tuple:
    """-> (ok, reason). The gate, applied BEFORE ranking on value.

    A cheap company that loses money is not cheap, it is dying, and EBIT/EV is
    undefined in spirit when EBIT is negative. Negative book equity is the
    same story from the balance-sheet side.
    """
    if op_income is None or not np.isfinite(op_income):
        return False, "no_op_income"
    if op_income <= 0:
        return False, "unprofitable"
    if equity is None or not np.isfinite(equity):
        return False, "no_equity"
    if equity <= 0:
        return False, "negative_equity"
    d = 0.0 if debt is None or not np.isfinite(debt) else float(debt)
    if d / float(equity) > max_debt_to_equity:
        return False, "over_levered"
    return True, "ok"


# ──────────────────────────────────────────────────────────────────── risk

def realized_vol(closes: pd.Series, window: int = 252) -> float:
    """Annualised standard deviation of daily log returns over `window` bars."""
    if closes is None or len(closes) < 20:
        return float("nan")
    s = pd.to_numeric(closes, errors="coerce").dropna().tail(window + 1)
    if len(s) < 20 or (s <= 0).any():
        return float("nan")
    r = np.diff(np.log(s.to_numpy(dtype=float)))
    if len(r) < 19:
        return float("nan")
    return float(np.std(r, ddof=1) * math.sqrt(252))


def max_drawdown(closes: pd.Series, window: int = 252) -> float:
    """Worst peak-to-trough fall over `window` bars, as a POSITIVE fraction.

    Reported positive so that larger always means riskier, matching every
    other component. A component that ran the other way would silently invert
    its contribution to the composite.
    """
    if closes is None or len(closes) < 20:
        return float("nan")
    s = pd.to_numeric(closes, errors="coerce").dropna().tail(window)
    if len(s) < 20 or (s <= 0).any():
        return float("nan")
    v = s.to_numpy(dtype=float)
    peak = np.maximum.accumulate(v)
    return float(np.max((peak - v) / peak))


def leverage_ratio(debt, equity) -> float:
    if equity is None or not np.isfinite(equity) or equity <= 0:
        return float("nan")
    d = 0.0 if debt is None or not np.isfinite(debt) else float(debt)
    return d / float(equity)


def illiquidity(adv) -> float:
    """Negative log dollar volume: larger is thinner, so larger is riskier."""
    if adv is None or not np.isfinite(adv) or adv <= 0:
        return float("nan")
    return -math.log(float(adv))


def thin_equity(equity, assets) -> float:
    """1 - equity/assets. Larger means less cushion under the liabilities."""
    if assets is None or equity is None:
        return float("nan")
    if not np.isfinite(assets) or not np.isfinite(equity) or assets <= 0:
        return float("nan")
    return 1.0 - (float(equity) / float(assets))


def percentile_rank(values) -> list:
    """Cross-sectional rank in [0, 1]; NaN stays NaN. Ties share a rank.

    Averaging raw components would let volatility in percent and leverage as a
    ratio compete on scale rather than on information. Percentiles put every
    component on the same footing before they are combined.
    """
    v = pd.Series(list(values), dtype="float64")
    ranked = v.rank(pct=True, method="average")
    return [float(x) if pd.notna(x) else float("nan") for x in ranked]


def composite_risk(component_ranks: dict, min_components: int = 3) -> float:
    """Mean of available component percentiles.

    A name missing most of its inputs is not low risk, it is UNKNOWN risk, so
    fewer than `min_components` present yields NaN rather than an average of
    whatever happened to be there. Unknown risk must not sort to the safe end.
    """
    vals = [component_ranks.get(c) for c in RISK_COMPONENTS]
    present = [v for v in vals if v is not None and np.isfinite(v)]
    if len(present) < min_components:
        return float("nan")
    return float(np.mean(present))


def assign_tiers(scores) -> list:
    """Terciles of the composite: low / moderate / high. NaN -> None.

    Cut at the 33rd and 67th percentiles OF THE NAMES SCORED THAT DAY, so the
    tiers are relative to the opportunity set at that rebalance rather than to
    a fixed threshold that would drift with the market.
    """
    s = pd.Series(list(scores), dtype="float64")
    ok = s.dropna()
    if len(ok) < 3:
        return [None] * len(s)
    lo, hi = ok.quantile(1 / 3), ok.quantile(2 / 3)
    out = []
    for v in s:
        if pd.isna(v):
            out.append(None)
        elif v <= lo:
            out.append("low")
        elif v <= hi:
            out.append("moderate")
        else:
            out.append("high")
    return out


# ────────────────────────────────────────────────────────── portfolio

def select_portfolio(rows, tier: str, n: int = 20) -> list:
    """Top `n` by EBIT/EV within one risk tier, equal weight.

    `rows` are dicts carrying at least ticker / ebit_ev / risk_tier / quality_ok.
    Order of operations is fixed and matters: quality gate, then tier, then
    rank on value. Ranking first and filtering after would produce a different
    portfolio and is not what the pre-registration declares.
    """
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")
    pool = [r for r in rows
            if r.get("quality_ok")
            and r.get("risk_tier") == tier
            and r.get("ebit_ev") is not None
            and np.isfinite(r.get("ebit_ev", float("nan")))]
    pool.sort(key=lambda r: (-r["ebit_ev"], str(r.get("ticker", ""))))
    picked = pool[:n]
    w = 1.0 / len(picked) if picked else 0.0
    return [dict(r, weight=w) for r in picked]
