"""Ticker-safe pandas entry points. Stdlib + pandas, no network.

WHY THIS MODULE EXISTS

`NA` is a real listed company (Nano Labs). pandas turns the string "NA" into
a missing value in at least TWO independent places, and the project has now
been bitten in both:

  1. `read_csv` -- the default `keep_default_na` list includes "NA", "NAN",
     "NULL", "N/A" and friends. This is what silently deleted 16 name-months
     from the V29 universe panel and got recorded in the registry as
     "16 panel rows dropped on an unusable ticker". The ticker was never
     unusable.

  2. `DataFrame(...)` CONSTRUCTION, under pandas 3's string dtype. Fixing (1)
     in extract_8k_deficiency.load_panel was not enough: the CIK diagnostic
     read the panel correctly, kept "NA" as a string, then built a DataFrame
     from the result and lost it again. It printed as a blank ticker.

Two different layers, one company, same disappearance. Fixing it at each call
site is how the third instance gets missed, so both entry points live here and
are tested together.

🔑 A ticker is a LABEL, never a quantity. Nothing in this module should ever
infer a numeric or nullable dtype for one.
"""

from __future__ import annotations

import pandas as pd

# Strings pandas would otherwise swallow that are, or could be, real tickers.
# NA  Nano Labs            NAN  (Nasdaq-eligible four-letter space)
# NULL/NONE/N/A            and the empty-ish forms callers do want dropped.
SWALLOWED = ("NA", "NAN", "NULL", "NONE", "N/A")


def read_csv(path: str, **kw) -> pd.DataFrame:
    """read_csv that treats NOTHING as missing by default.

    Emptiness is a question for the caller to ask explicitly with
    `drop_blank`, because "this field is empty" and "this field says NA" are
    different statements and only one of them is missing data.
    """
    kw.setdefault("keep_default_na", False)
    kw.setdefault("na_values", [])
    return pd.read_csv(path, **kw)


def as_labels(values) -> pd.Series:
    """A Series of ticker labels that pandas cannot reinterpret.

    dtype=object is the point: under pandas 3's string dtype, constructing
    from a Python list containing "NA" can yield a missing value instead.
    """
    return pd.Series(list(values), dtype=object)


def frame(rows: list, label_cols=("ticker",)) -> pd.DataFrame:
    """DataFrame from row dicts, with label columns forced back to object.

    The columns are rebuilt from the original Python values rather than
    repaired afterwards -- once pandas has turned "NA" into a missing value
    there is nothing left to repair.
    """
    df = pd.DataFrame(rows)
    for c in label_cols:
        if c in df.columns:
            df[c] = as_labels([r.get(c) for r in rows])
    return df


def normalise(s: pd.Series) -> pd.Series:
    """Strip and upper-case labels without letting pandas drop any."""
    return as_labels([str(v).strip().upper() for v in s])


def drop_blank(df: pd.DataFrame, col: str = "ticker"):
    """Drop GENUINELY empty labels. Returns (df, n_dropped).

    "NA" is not blank. "" is.
    """
    blank = as_labels([str(v).strip() == "" for v in df[col]]).astype(bool)
    n = int(blank.sum())
    return (df.loc[~blank.to_numpy()].copy() if n else df), n
