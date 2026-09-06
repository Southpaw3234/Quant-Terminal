"""Calendar-time measurement — V29. The portfolio as a daily return series.

An event study averages independent event outcomes. A portfolio does not have
independent outcomes: it has one path, which is the thing an account actually
experiences. So the statistic here is a DAILY RETURN SERIES, and every
criterion in docs/V29_PREREGISTRATION.md is computed from it.

Pure functions. No prices fetched, no files read, no network.

THREE THINGS THAT ARE EASY TO GET WRONG AND ARE HANDLED EXPLICITLY

1. **Autocorrelation.** A book held for a quarter overlaps itself day to day,
   so daily returns are not independent and an ordinary standard error
   overstates t. Newey-West with a declared lag is the correction, and it is
   why the specification names the lag rather than leaving it to whoever runs
   the code.

2. **Names that stop trading mid-quarter.** Taking the mean of whatever names
   still have data silently DELETES the loser: a position that falls 90% and
   delists leaves the average, and the average improves. Weights are therefore
   fixed at the rebalance and a missing name earns `delisting_return` (default
   0) for the rest of the period, keeping its realised loss in the path.
   ⚠️ 0 is a placeholder, not a finding. The real assumption is a
   pre-registration choice the registry records as unmade.

3. **Costs inside the criterion, not beside it.** A gross return that a 75 bp
   round trip erases is not a result. `apply_costs` charges turnover on the
   rebalance day itself, so every downstream statistic is net.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ───────────────────────────────────────────────────────── the return series

def portfolio_daily_returns(schedule, returns: pd.DataFrame,
                            delisting_return: float = 0.0) -> pd.Series:
    """Equal-weighted daily returns from a rebalance schedule.

    `schedule` is [(effective_date, [tickers])], sorted; holdings apply from
    the effective date until the next one. `returns` is a date x ticker frame
    of simple daily returns.

    Weights are fixed at the rebalance. A held name with no return that day
    earns `delisting_return`, so it stays in the book at its declared
    assumption rather than vanishing from a mean of survivors.
    """
    if returns is None or returns.empty or not schedule:
        return pd.Series(dtype="float64")
    sched = sorted(schedule, key=lambda kv: pd.Timestamp(kv[0]))
    idx = returns.index
    out = pd.Series(0.0, index=idx, dtype="float64")
    live = None
    for d in idx:
        for eff, names in sched:
            if pd.Timestamp(eff) <= d:
                live = names
            else:
                break
        if not live:
            out.loc[d] = 0.0
            continue
        w = 1.0 / len(live)
        total = 0.0
        for tk in live:
            r = returns[tk].get(d, np.nan) if tk in returns.columns else np.nan
            total += w * (delisting_return if (r is None or not np.isfinite(r)) else float(r))
        out.loc[d] = total
    return out


def turnover(prev: list, new: list) -> float:
    """Fraction of the book replaced. Equal weights both sides.

    A full replacement is 1.0 and an unchanged book is 0.0, so charging
    `turnover * cost` charges a round trip on the part that actually traded.
    """
    if not new:
        return 0.0
    if not prev:
        return 1.0
    p, n = set(prev), set(new)
    return len(n - p) / len(n)


def apply_costs(returns: pd.Series, turnover_by_date: dict,
                round_trip_bps: float) -> pd.Series:
    """Charge turnover on the rebalance day. Net series in, net series out."""
    if returns is None or returns.empty:
        return returns
    out = returns.copy().astype("float64")
    for d, t in (turnover_by_date or {}).items():
        ts = pd.Timestamp(d)
        if ts in out.index:
            out.loc[ts] = out.loc[ts] - float(t) * (float(round_trip_bps) / 10_000.0)
    return out


# ─────────────────────────────────────────────────────────────── statistics

def newey_west_se(x, lag: int = 10) -> float:
    """Standard error of the MEAN, corrected for autocorrelation.

    Var(x̄) = S / n, with S the long-run variance
    S = γ₀ + 2·Σ_{l=1..L} (1 − l/(L+1))·γ_l  (Bartlett weights).

    With no autocorrelation this collapses to the ordinary standard error,
    which is the property the suite checks rather than assumes.
    """
    s = pd.Series(list(x), dtype="float64").dropna()
    n = len(s)
    if n < 3:
        return float("nan")
    v = s.to_numpy(dtype=float)
    dev = v - v.mean()
    g0 = float(np.dot(dev, dev) / n)
    S = g0
    for l in range(1, min(int(lag), n - 1) + 1):
        gl = float(np.dot(dev[l:], dev[:-l]) / n)
        S += 2.0 * (1.0 - l / (lag + 1.0)) * gl
    if not np.isfinite(S) or S <= 0:
        return float("nan")
    return math.sqrt(S / n)


def t_stat(x, lag: int = 10) -> float:
    s = pd.Series(list(x), dtype="float64").dropna()
    if len(s) < 3:
        return float("nan")
    se = newey_west_se(s, lag)
    if not np.isfinite(se) or se == 0:
        return float("nan")
    return float(s.mean() / se)


def annualized_return(daily) -> float:
    """Geometric, from compounded daily returns."""
    s = pd.Series(list(daily), dtype="float64").dropna()
    if len(s) == 0:
        return float("nan")
    total = float((1.0 + s).prod())
    if total <= 0:
        return -1.0
    return total ** (TRADING_DAYS / len(s)) - 1.0


def information_ratio(excess_daily) -> float:
    s = pd.Series(list(excess_daily), dtype="float64").dropna()
    if len(s) < 3:
        return float("nan")
    sd = float(s.std(ddof=1))
    if sd == 0:
        return float("nan")
    return float(s.mean() / sd) * math.sqrt(TRADING_DAYS)


def max_drawdown_from_returns(daily) -> float:
    """Worst peak-to-trough of the compounded path, POSITIVE."""
    s = pd.Series(list(daily), dtype="float64").dropna()
    if len(s) == 0:
        return float("nan")
    curve = (1.0 + s).cumprod().to_numpy(dtype=float)
    peak = np.maximum.accumulate(curve)
    return float(np.max((peak - curve) / peak))


def stability(excess_daily) -> "float | None":
    """Final third over first third of cumulative excess.

    None when the first third is <= 0: a ratio against a non-positive base
    flips sign for reasons that have nothing to do with persistence, and the
    specification counts undefined as a MISS rather than a pass.
    """
    s = pd.Series(list(excess_daily), dtype="float64").dropna()
    if len(s) < 6:
        return None
    k = len(s) // 3
    first = float(s.iloc[:k].sum())
    last = float(s.iloc[-k:].sum())
    if first <= 0:
        return None
    return last / first


# ──────────────────────────────────────────────────────────────── the read

def evaluate(port_daily, bench_daily, min_days: int = 1000,
             min_t: float = 2.0, min_ir: float = 0.5,
             min_stability: float = 0.5, lag: int = 10) -> dict:
    """Every criterion in one place. Returns the numbers AND the verdict.

    Reports each bar as cleared or missed rather than a bare pass/fail, so a
    near miss is legible as a near miss instead of collapsing into "no".
    """
    p = pd.Series(list(port_daily), dtype="float64")
    b = pd.Series(list(bench_daily), dtype="float64")
    n = min(len(p), len(b))
    p, b = p.iloc[:n], b.iloc[:n]
    excess = (p.to_numpy() - b.to_numpy()) if n else np.array([])

    res = {
        "n_days": int(n),
        "ann_return": annualized_return(p),
        "ann_bench": annualized_return(b),
        "ann_excess": annualized_return(p) - annualized_return(b) if n else float("nan"),
        "t_excess": t_stat(excess, lag),
        "information_ratio": information_ratio(excess),
        "max_dd": max_drawdown_from_returns(p),
        "max_dd_bench": max_drawdown_from_returns(b),
        "stability": stability(excess),
    }
    cleared, missed = [], []
    (cleared if res["n_days"] >= min_days else missed).append(
        f"N {res['n_days']} vs {min_days} days")
    (cleared if res["ann_excess"] > 0 else missed).append(
        f"excess {res['ann_excess']:+.2%} vs > 0")
    (cleared if res["t_excess"] >= min_t else missed).append(
        f"t {res['t_excess']:.2f} vs {min_t}")
    (cleared if res["information_ratio"] >= min_ir else missed).append(
        f"IR {res['information_ratio']:.2f} vs {min_ir}")
    (cleared if res["max_dd"] <= res["max_dd_bench"] else missed).append(
        f"maxDD {res['max_dd']:.1%} vs benchmark {res['max_dd_bench']:.1%}")
    st = res["stability"]
    if st is None:
        missed.append("stability UNDEFINED (first third <= 0)")
    else:
        (cleared if st >= min_stability else missed).append(
            f"stability {st:.2f} vs {min_stability}")
    res["cleared"] = cleared
    res["missed"] = missed
    res["verdict"] = "MET" if not missed else "NOT MET"
    return res
