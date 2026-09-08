"""Reconciliation — what the backtest assumed against what trading cost. V29.

A backtest fills everything, instantly, at the close. Live does neither. The
gap between those two facts is the single largest source of divergence for an
illiquid book, and it is not a bug: it is the price of trading things nobody
else wants. This module measures it.

THREE COMPARISONS, ANSWERING THREE DIFFERENT QUESTIONS

  COST          implementation shortfall -- decision price to fill price.
                Captures spread, market impact and delay together, which is
                why it is the right measure rather than commission plus a
                spread estimate.
  COMPLETION    the fraction of intended positions never established. A
                backtest cannot see this at all, because a backtest always
                fills, and for thin names it can be the largest cost of the
                three.
  RETURN        the live daily series against what the backtest would have
                produced holding the same names on the same days. Divergence
                beyond costs means a bug, and this is the check that would
                have caught the stale-signal defect in months rather than
                never.

⚠️ WHAT RECONCILIATION MAY NOT DO: retune the declared cost assumption. 75 bps
is signed. If the measured figure is three times that, the specification is
likely to fail on costs, and that is the pre-registration working. The number
measured here is evidence for the NEXT specification, not an adjustment to
this one.

Pure functions. No network, no files, no broker.
"""
from __future__ import annotations

import collections

import numpy as np
import pandas as pd

BUY, SELL = "buy", "sell"


def implementation_shortfall_bps(decision_price: float, fill_price: float,
                                 side: str) -> float:
    """Cost in basis points, POSITIVE when the fill was worse than the decision.

    Signed by side: paying above the decision price hurts a buy, receiving
    below it hurts a sell. Reporting both as positive costs means they can be
    averaged together without one cancelling the other -- which is exactly the
    mistake that would make a book look cheap to trade.
    """
    if decision_price is None or fill_price is None:
        return float("nan")
    if not (np.isfinite(decision_price) and np.isfinite(fill_price)) or decision_price <= 0:
        return float("nan")
    raw = (float(fill_price) - float(decision_price)) / float(decision_price)
    if side == BUY:
        return raw * 10_000.0
    if side == SELL:
        return -raw * 10_000.0
    raise ValueError(f"side must be {BUY!r} or {SELL!r}, got {side!r}")


def summarize_fills(fills) -> dict:
    """-> {n, mean_bps, median_bps, worst_bps, by_side}. One number per fill.

    `fills` are dicts with decision_price, fill_price, side and optionally qty.
    """
    rows, by_side = [], collections.defaultdict(list)
    for f in fills or []:
        bps = implementation_shortfall_bps(
            f.get("decision_price"), f.get("fill_price"), f.get("side"))
        if np.isfinite(bps):
            rows.append(bps)
            by_side[f.get("side")].append(bps)
    if not rows:
        return {"n": 0, "mean_bps": float("nan"), "median_bps": float("nan"),
                "worst_bps": float("nan"), "by_side": {}}
    s = pd.Series(rows, dtype="float64")
    return {"n": len(rows), "mean_bps": float(s.mean()),
            "median_bps": float(s.median()), "worst_bps": float(s.max()),
            "by_side": {k: float(pd.Series(v).mean()) for k, v in by_side.items()}}


def completion_rate(intended: dict, filled: dict) -> dict:
    """-> {n_intended, n_filled, share_filled, unfilled_names}.

    Share of SHARES filled, not of names touched: a name that fills 5% of its
    intended size is a name the strategy does not really own, and counting it
    as a filled name would hide that.
    """
    want = {k: abs(int(v)) for k, v in (intended or {}).items() if int(v) != 0}
    got = {k: abs(int(v)) for k, v in (filled or {}).items()}
    total_want = sum(want.values())
    total_got = sum(min(got.get(k, 0), v) for k, v in want.items())
    unfilled = sorted(k for k, v in want.items() if got.get(k, 0) < v)
    return {"n_intended": len(want), "n_filled": sum(1 for k, v in want.items()
                                                     if got.get(k, 0) >= v),
            "share_filled": (total_got / total_want) if total_want else float("nan"),
            "unfilled_names": unfilled}


def cost_vs_assumption(measured_bps: float, assumed_bps: float = 75.0) -> dict:
    """How far the declared assumption is from what trading actually cost."""
    if measured_bps is None or not np.isfinite(measured_bps) or assumed_bps <= 0:
        return {"ratio": float("nan"), "verdict": "unmeasured"}
    ratio = float(measured_bps) / float(assumed_bps)
    if ratio <= 1.25:
        v = "assumption holds"
    elif ratio <= 3.0:
        v = "assumption optimistic"
    else:
        v = "assumption wrong by a multiple"
    return {"ratio": ratio, "verdict": v,
            "note": "The declared parameter is SIGNED and does not move. This is "
                    "evidence for the next specification, not an adjustment to this one."}


def return_divergence(live_daily, backtest_daily, tolerance_bps: float = 25.0) -> dict:
    """Daily live-minus-backtest difference, and whether it exceeds tolerance.

    Costs explain a divergence on rebalance days. A persistent gap on days the
    book did not trade does not have that excuse, and is the shape a bug
    makes: the stale-signal defect looked exactly like this for months.
    """
    a = pd.Series(list(live_daily), dtype="float64")
    b = pd.Series(list(backtest_daily), dtype="float64")
    n = min(len(a), len(b))
    if n == 0:
        return {"n": 0, "mean_bps": float("nan"), "max_abs_bps": float("nan"),
                "days_over_tolerance": 0, "within_tolerance": True}
    d = (a.iloc[:n].to_numpy() - b.iloc[:n].to_numpy()) * 10_000.0
    d = d[np.isfinite(d)]
    if len(d) == 0:
        return {"n": 0, "mean_bps": float("nan"), "max_abs_bps": float("nan"),
                "days_over_tolerance": 0, "within_tolerance": True}
    over = int((np.abs(d) > float(tolerance_bps)).sum())
    return {"n": int(len(d)), "mean_bps": float(np.mean(d)),
            "max_abs_bps": float(np.max(np.abs(d))),
            "days_over_tolerance": over,
            "within_tolerance": over == 0}
