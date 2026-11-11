"""The frozen point-in-time universe panel, read safely. Stdlib + pandas.

Membership is the one input every specification shares, and reading it wrong
is the one mistake that cannot be caught downstream: a name that silently
vanishes at load time is simply absent from the sample, and nothing later in
the pipeline can tell that it should have been there.

🔑 WHY THIS IS NOT IMPORTED FROM extract_8k_deficiency

That module has an equivalent loader, and it is the better-tested of the
two. It also belongs to specification #4 (listing deficiency), which was
ABANDONED on 2026-11-11 and is kept only as the evidence for that decision.
Importing live code out of an abandoned specification would make the
abandoned file load-bearing again and quietly un-abandon it. The logic is
restated here instead, and the duplication is deliberate.
"""

from __future__ import annotations

import pandas as pd

from qt import tickers as qtk


def load_pit_panel(path: str):
    """-> ({month_end: frozenset(tickers)}, month_ends index, all tickers).

    ⚠️ The ticker-safe read is LITERAL, not defensive. `NA` is a real listed
    company (Nano Labs) and pandas.read_csv turns the string "NA" into NaN by
    default -- which is exactly the "16 panel rows dropped on an unusable
    ticker" the V29 registry records. The ticker was never unusable; a
    parsing default ate it. "NAN", "NULL", "NONE" and "N/A" go the same way,
    so nothing is treated as missing and emptiness is tested explicitly.
    """
    df = qtk.read_csv(path, usecols=["date", "ticker"], dtype={"ticker": str})
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["ticker"] = qtk.normalise(df["ticker"])
    df, _ = qtk.drop_blank(df, "ticker")

    by_month = {}
    for d, grp in df.groupby("date"):
        by_month[d] = frozenset(grp["ticker"].tolist())
    month_ends = pd.DatetimeIndex(sorted(by_month))
    tickers = sorted({t for s in by_month.values() for t in s})
    return by_month, month_ends, tickers


def eligible_at(by_month: dict, month_ends, asof) -> frozenset:
    """The universe as of the last month-end ON OR BEFORE `asof`.

    The look-ahead guard for membership. A company that becomes eligible
    AFTER the formation date must not be selected on the strength of
    eligibility it did not yet have.
    """
    if asof is None or not len(month_ends):
        return frozenset()
    i = month_ends.searchsorted(pd.Timestamp(asof), side="right") - 1
    if i < 0:
        return frozenset()
    return by_month.get(month_ends[i], frozenset())
