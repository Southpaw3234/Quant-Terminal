"""SEC industry code (SIC) -> a broad, GICS-style sector name. Pure.

The S&P 500 list carries each company's official GICS sector. The watched
small-cap universe doesn't, and GICS itself is licensed. Every SEC filer,
though, has a four-digit SIC code, so peers are grouped by this mapping.
It is APPROXIMATE -- SIC predates GICS and splits industries differently --
and the page labels it as such.
"""

from __future__ import annotations

# (low, high, sector), first match wins, so narrower ranges come first.
RANGES = [
    (1311, 1389, "Energy"), (1200, 1299, "Energy"), (2900, 2999, "Energy"),
    (100, 999, "Consumer Staples"),
    (1000, 1499, "Materials"),
    (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"),
    (2200, 2399, "Consumer Discretionary"),
    (2500, 2599, "Consumer Discretionary"),
    (2700, 2749, "Communication Services"),   # newspapers, periodicals, books
    (2750, 2799, "Industrials"),              # commercial printing, business forms
    (2830, 2836, "Health Care"),
    (2840, 2844, "Consumer Staples"),
    (2400, 2899, "Materials"),
    (3100, 3199, "Consumer Discretionary"),
    (3000, 3399, "Materials"),
    (3570, 3579, "Information Technology"),
    (3400, 3599, "Industrials"),
    (3630, 3639, "Consumer Discretionary"),
    (3600, 3699, "Information Technology"),
    (3711, 3716, "Consumer Discretionary"),
    (3751, 3751, "Consumer Discretionary"),
    (3700, 3799, "Industrials"),
    (3840, 3851, "Health Care"),
    (3800, 3899, "Information Technology"),
    (3900, 3999, "Consumer Discretionary"),
    (4000, 4799, "Industrials"),
    (4800, 4899, "Communication Services"),
    (4950, 4959, "Industrials"),
    (4900, 4999, "Utilities"),
    (5122, 5122, "Health Care"),
    (5140, 5149, "Consumer Staples"),
    (5000, 5199, "Industrials"),
    (5400, 5499, "Consumer Staples"),
    (5912, 5912, "Consumer Staples"),
    (5200, 5999, "Consumer Discretionary"),
    (6798, 6798, "Real Estate"),
    (6500, 6553, "Real Estate"),
    (6000, 6799, "Financials"),
    (7370, 7379, "Information Technology"),
    (7000, 7299, "Consumer Discretionary"),
    (7300, 7399, "Industrials"),
    (7500, 7599, "Consumer Discretionary"),
    (7800, 7899, "Communication Services"),
    (7900, 7999, "Consumer Discretionary"),
    (8000, 8099, "Health Care"),
    (8731, 8731, "Health Care"),
    (8200, 8299, "Consumer Discretionary"),
    (8000, 8999, "Industrials"),
]


def sector(sic) -> str:
    try:
        s = int(str(sic).strip())
    except (TypeError, ValueError):
        return "Other"
    for lo, hi, name in RANGES:
        if lo <= s <= hi:
            return name
    return "Other"
