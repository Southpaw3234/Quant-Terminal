"""Ratios for the S&P 500 lookup, and each sector's median. Pure logic.

Inputs: one company's SEC figures (qt/company_facts.extract, already
published by the weekly sweep) and its price statistics. Output: plain
numbers with None wherever a ratio would mislead -- a P/E on a loss, a
price-to-book on negative equity, a growth rate across mismatched periods.

Descriptive only. No score, no rank, no rating, no fair value: a lookup
shows what a company reports and how the market prices it, beside its
sector, and the page explains each figure in words.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

RATIOS = ("pe", "ps", "pb", "gross_margin", "op_margin", "net_margin", "roe",
          "fcf_yield", "debt_to_equity", "revenue_growth", "eps_growth",
          "dividend_yield", "payout_ratio", "vol_1y", "return_1y", "beta_1y",
          "off_52w_high", "market_cap")


def _v(f: dict, k: str):
    x = (f.get(k) or {}).get("val") if isinstance(f.get(k), dict) else None
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def _div(a, b, positive_b=True):
    if a is None or b is None or b == 0 or (positive_b and b <= 0):
        return None
    return a / b


def price_stats(closes: pd.Series, bench: pd.Series) -> dict:
    """1-year price figures from daily closes (USD, adjusted)."""
    px = pd.Series(closes, dtype="float64").dropna()
    if len(px) < 60:
        return {"price": float(px.iloc[-1]) if len(px) else None, "n_days": int(len(px))}
    yr = px[px.index >= px.index[-1] - pd.Timedelta(days=365)]
    r = np.log(yr).diff().dropna()
    out = {"price": round(float(px.iloc[-1]), 2), "last_session": px.index[-1].date().isoformat(),
           "high_52w": round(float(yr.max()), 2), "low_52w": round(float(yr.min()), 2),
           "off_52w_high": round(float(px.iloc[-1] / yr.max() - 1), 4),
           "return_1y": round(float(yr.iloc[-1] / yr.iloc[0] - 1), 4),
           "vol_1y": round(float(r.std(ddof=1) * math.sqrt(252)), 4),
           "n_days": int(len(yr))}
    if bench is not None and len(bench):
        b = np.log(pd.Series(bench, dtype="float64").dropna()).diff()
        j = pd.concat([r, b], axis=1, join="inner").dropna()
        if len(j) >= 120 and j.iloc[:, 1].var() > 0:
            out["beta_1y"] = round(float(j.cov().iloc[0, 1] / j.iloc[:, 1].var()), 2)
    return out


def ratios(f: dict, p: dict) -> dict:
    """Every ratio for one company. None where it would mislead."""
    price = p.get("price")
    sh = _v(f, "shares")
    mcap = price * sh if price and sh else None
    rev, ni, eq = _v(f, "revenue"), _v(f, "net_income"), _v(f, "equity")
    eps, dps = _v(f, "eps"), _v(f, "dividends_per_share")
    ocf, capex = _v(f, "op_cash_flow"), _v(f, "capex")
    debt = sum(x for x in (_v(f, "debt_long"), _v(f, "debt_current")) if x is not None) \
        if (_v(f, "debt_long") is not None or _v(f, "debt_current") is not None) else None
    fcf = ocf - (capex or 0) if ocf is not None else None
    ago = lambda k: _v(f, k + "_year_ago")    # noqa: E731

    def growth(k):
        a, b = _v(f, k), ago(k)
        return None if a is None or b is None or b <= 0 else a / b - 1

    out = {
        "market_cap": mcap,
        "pe": _div(price, eps) if eps and eps > 0 else None,
        "ps": _div(mcap, rev),
        "pb": _div(mcap, eq),
        "gross_margin": _div(_v(f, "gross_profit"), rev),
        "op_margin": _div(_v(f, "op_income"), rev),
        "net_margin": _div(ni, rev),
        "roe": _div(ni, eq),
        "fcf": fcf,
        "fcf_yield": _div(fcf, mcap),
        "debt_total": debt,
        "net_cash": (_v(f, "cash") - debt) if _v(f, "cash") is not None and debt is not None else None,
        "debt_to_equity": _div(debt, eq),
        "revenue_growth": growth("revenue"),
        "eps_growth": growth("eps"),
        "net_income_growth": growth("net_income"),
        # No dividend tag is "not reported", never 0%: plenty of payers tag
        # it differently, and a zero would claim something we did not see.
        "dividend_yield": _div(dps, price) if dps else None,
        "payout_ratio": _div(dps, eps) if dps and eps and eps > 0 else None,
    }
    for k in ("vol_1y", "return_1y", "beta_1y", "off_52w_high"):
        out[k] = p.get(k)
    return {k: (None if v is None else round(float(v), 4) if abs(v) < 1e6 else round(float(v)))
            for k, v in out.items()}


def sector_medians(rows: dict) -> dict:
    """{sector: {ratio: median}} over companies that HAVE the ratio."""
    by = {}
    for r in rows.values():
        by.setdefault(r.get("sector") or "Unknown", []).append(r.get("ratios") or {})
    out = {}
    for s, rs in by.items():
        med = {}
        for k in RATIOS:
            vals = [x[k] for x in rs if x.get(k) is not None]
            med[k] = round(float(np.median(vals)), 4) if len(vals) >= 5 else None
        med["n"] = len(rs)
        out[s] = med
    return out
