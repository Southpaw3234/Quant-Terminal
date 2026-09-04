"""Price layer — v28 / v27 component P.

One place that owns prices, so that "what did this trade at" and "was this
eligible" are answered by the same code with the same as-of semantics.

────────────────────────────────────────────────────────────────────────────
WHAT BACK-ADJUSTMENT DOES AND DOES NOT BREAK
────────────────────────────────────────────────────────────────────────────
`yfinance(auto_adjust=True)` returns a series back-adjusted with TODAY's split
and dividend factors. The usual warning is "back-adjusted series leak". That is
half right, and the half matters, so it is stated precisely here rather than
carried as folklore.

✅ **RETURNS ARE SAFE.** A return is `p[exit] / p[entry] - 1`. A constant
   back-adjustment factor appears in numerator and denominator and CANCELS
   EXACTLY — including when a split falls between entry and exit, which is
   the case adjustment exists to handle. A2's abnormal returns are computed
   on adjusted series and are correct.

🔴 **PRICE-LEVEL FILTERS ARE NOT SAFE, AND THE BIAS HAS A DIRECTION.**
   A floor like "price >= $2" evaluated on a back-adjusted series asks *"what
   is this worth in today's split-adjusted terms"*, not *"what did it trade at
   on the event date"*. After a 1-for-10 REVERSE split, a stock that actually
   traded at $0.50 appears in the back-adjusted series at $5.00 and clears a
   $2 floor it should have failed.

   🔑 **Reverse splits are overwhelmingly a consequence of distress**, and
   distress is concentrated in exactly the small, illiquid names v27 screens
   for. So back-adjustment does not scatter noise — it systematically ADMITS
   the distressed names the floor exists to exclude, and it admits them on the
   strength of what happened to them AFTER the event.

⚠️ **DOLLAR VOLUME LARGELY SURVIVES**, which is why this is a narrower problem
   than "throw out the adjusted series". Splits scale price and volume
   inversely, so `close x volume` is approximately invariant to the split
   factor. Dollar ADV computed on adjusted data is close to the truth; the
   price floor is where the leak actually lives.

**Conclusion, and it is not "use raw prices":** compute RETURNS on adjusted
series, and evaluate PRICE-LEVEL ELIGIBILITY as of the event date using the
level the series carried at that date. `screen_as_of()` below does the second.
Using unadjusted prices for returns would introduce split jumps and be strictly
worse.

────────────────────────────────────────────────────────────────────────────
POINT-IN-TIME ELIGIBILITY
────────────────────────────────────────────────────────────────────────────
`build_universe.py` builds TODAY's tradeable universe, which is the right
question for "what could I buy now" and the WRONG question for "what was
eligible when this event fired". Selecting a year of historical events by
membership in today's list is a point-in-time error and a survivorship one at
the same time.

⚠️ **THIS LAYER CANNOT FIX THE DELISTING HALF.** Names that left the exchange
are absent from the symbol source entirely, so no as-of computation can
resurrect them. That bias remains, remains upward, and must be stated with any
result. What this layer fixes is the part that IS fixable: evaluating a
surviving name's eligibility with the data available on the event date rather
than with today's.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Eligibility:
    eligible: bool
    reason: str
    price: float = float("nan")
    adv: float = float("nan")
    n_bars: int = 0


def as_of(series: pd.Series, asof) -> pd.Series:
    """The series as it was knowable on `asof` — bars STRICTLY BEFORE it.

    Strictly before, not up-to-and-including: the event-date bar is the
    session the event occurred in, and its close is not knowable when the
    decision is made. Same rule `event_study._entry_index` applies to entry,
    applied to eligibility, so the two cannot disagree.
    """
    if series is None or len(series) == 0:
        return pd.Series(dtype=float)
    if asof is None:
        return series
    return series[series.index < pd.Timestamp(asof)]


def price_as_of(close: pd.Series, asof) -> float:
    """Last close strictly before `asof`, or NaN."""
    s = as_of(close, asof)
    return float(s.iloc[-1]) if len(s) else float("nan")


def dollar_adv(close: pd.Series, volume: pd.Series, asof=None,
               lookback: int = 60) -> tuple:
    """Median daily dollar volume over `lookback` bars before `asof`.

    MEDIAN, not mean: one halt-and-resume day or an index-rebalance print can
    multiply a thin name's mean several times over, and a screen built on the
    mean admits names on the strength of a single day nobody can trade again.

    Approximately invariant to back-adjustment — see the module docstring.
    """
    if close is None or volume is None or len(close) == 0:
        return float("nan"), 0
    df = pd.DataFrame({"c": close, "v": volume}).dropna()
    if asof is not None:
        df = df[df.index < pd.Timestamp(asof)]
    n = len(df)
    if n == 0:
        return float("nan"), 0
    win = df.tail(lookback)
    return float(np.median(win["c"] * win["v"])), n


def screen_as_of(close: pd.Series, volume: pd.Series, asof,
                 adv_max: float, adv_min: float, price_min: float,
                 min_history: int, lookback: int = 60) -> Eligibility:
    """Was this name eligible ON `asof`? Every input is point-in-time.

    Returns a reason on refusal so a funnel can be built from the outcomes,
    rather than a bare boolean that makes "0 eligible" indistinguishable from
    "0 with price data".
    """
    px = price_as_of(close, asof)
    adv, n_bars = dollar_adv(close, volume, asof=asof, lookback=lookback)

    if not np.isfinite(px) or not np.isfinite(adv):
        return Eligibility(False, "no_price", px, adv, n_bars)
    if n_bars < min_history:
        return Eligibility(False, "short_history", px, adv, n_bars)
    if px < price_min:
        return Eligibility(False, "price_too_low", px, adv, n_bars)
    if adv > adv_max:
        return Eligibility(False, "too_liquid", px, adv, n_bars)
    if adv < adv_min:
        return Eligibility(False, "too_illiquid", px, adv, n_bars)
    return Eligibility(True, "eligible", px, adv, n_bars)


def forward_return(close: pd.Series, entry_i: int, horizon: int,
                   settled_only: bool = True):
    """Return over `horizon` bars from `entry_i`, or None if unsettled.

    Safe on a back-adjusted series: the adjustment factor cancels in the
    ratio. Settlement requires a bar AFTER the exit bar, so the exit is
    provably a completed session — a structural test, not a clock test.
    """
    if close is None or len(close) == 0:
        return None
    exit_i = entry_i + horizon
    last_usable = len(close) - 2 if settled_only else len(close) - 1
    if entry_i < 0 or exit_i > last_usable:
        return None
    return float(close.iloc[exit_i] / close.iloc[entry_i] - 1.0)
