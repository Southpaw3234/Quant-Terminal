"""Append-only frozen ledgers — v28 Phase 0.

ONE implementation, replacing two.

`_freeze_first_write` currently exists **twice**: in `analyze_rank_ic.py`
(keyed on `date`, added `203d95c`) and in `event_study.py` (keyed on
`event_id`). They are the same algorithm written twice, which is how the two
copies came to differ in their logging and their handling of an unreadable
file. A third copy was one component away.

WHAT A FROZEN LEDGER IS FOR
---------------------------
Both analyzers are FULL OVERWRITES: they rebuild every historical row on every
run from freshly downloaded prices. Without a freeze, written rows move. That
is not hypothetical — on 2026-08-10 `2026-07-15` went `278,0.0959` ->
`279,0.0955` and all 13 rows of `cross_sectional_ls.csv` were rewritten, one of
them by 22% on a three-week-old row.

The consequence is the one that matters: **the number read on decision day was
not the series that had been accumulated.** It was whatever that morning's
price download implied. A pre-registered read against a moving series is not a
pre-registered read.

TWO DESIGN CHOICES CARRIED OVER DELIBERATELY
--------------------------------------------
1. **Drift is REPORTED, not discarded.** How far a recompute would have moved a
   written row is evidence about price-source stability. Silently dropping it
   trades one blind spot for another.
2. **First write wins, unconditionally.** Not "first write wins unless the new
   value looks better", which is the same thing as no freeze at all.

ONE DELIBERATE DIFFERENCE FROM v25
----------------------------------
The originals `print()` inside the merge. This returns a `FreezeReport` and
leaves logging to the caller. A function that both computes and narrates cannot
be tested without capturing stdout, and every assertion about *what* was frozen
becomes an assertion about a log line.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class FreezeReport:
    """What the freeze did. Returned rather than printed."""

    frozen: list = field(default_factory=list)   # "key.col old->new"
    appended: list = field(default_factory=list)  # keys of genuinely new rows
    existing: int = 0
    mutable: bool = False
    unreadable: str = ""

    @property
    def drifted(self) -> bool:
        return bool(self.frozen)

    def summary(self, label: str = "ledger") -> str:
        if self.mutable:
            return (f"[{label}] MUTABLE MODE — written rows MAY MOVE. "
                    f"Never set this on a scheduled run.")
        if self.unreadable:
            return f"[{label}] existing file unreadable ({self.unreadable}) — wrote fresh"
        bits = []
        if self.frozen:
            shown = "; ".join(self.frozen[:8])
            more = " ..." if len(self.frozen) > 8 else ""
            bits.append(f"FROZE {len(self.frozen)} recomputed value(s) that would "
                        f"have changed already-written rows — kept the originals: "
                        f"{shown}{more}")
        if self.appended:
            bits.append(f"appended {len(self.appended)} new row(s): "
                        f"{', '.join(str(k) for k in self.appended[:5])}")
        if not bits:
            bits.append(f"no change ({self.existing} row(s) already written)")
        return " | ".join(f"[{label}] {b}" for b in bits)


def _same(a, b) -> bool:
    """Numeric comparison where possible, string comparison otherwise."""
    try:
        if pd.isna(a) and pd.isna(b):
            return True
    except (TypeError, ValueError):
        pass
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def freeze_first_write(new_df: pd.DataFrame, path: Path, key: str = "date",
                       mutable_env: str = "QT_LEDGER_MUTABLE"):
    """Merge `new_df` into the ledger at `path`; written rows never move.

    Returns `(merged_df, FreezeReport)`. The caller writes the file and decides
    how to log — this function does neither.

    `mutable_env` names an environment variable that, set to "1", disables the
    freeze for debugging. It must never be set on a scheduled run; the report
    flags it so a caller can fail loudly if it appears in CI.
    """
    report = FreezeReport()

    if mutable_env and os.environ.get(mutable_env, "").strip() == "1":
        report.mutable = True
        return new_df, report

    path = Path(path)
    if not path.exists() or new_df is None or new_df.empty:
        return new_df, report

    try:
        old = pd.read_csv(path)
    except Exception as exc:
        report.unreadable = str(exc)
        return new_df, report

    if old.empty or key not in old.columns or key not in new_df.columns:
        return new_df, report

    report.existing = len(old)
    new_by_key = {str(r[key]): r for _, r in new_df.iterrows()}

    for _, orow in old.iterrows():
        nrow = new_by_key.get(str(orow[key]))
        if nrow is None:
            continue
        for col in old.columns:
            if col == key or col not in new_df.columns:
                continue
            if not _same(orow[col], nrow[col]):
                report.frozen.append(f"{orow[key]}.{col} {orow[col]}->{nrow[col]}")

    seen = set(old[key].astype(str))
    fresh = new_df[~new_df[key].astype(str).isin(seen)]
    report.appended = [str(k) for k in fresh[key].tolist()]

    merged = pd.concat([old, fresh], ignore_index=True) if len(fresh) else old
    return merged, report


def write_ledger(new_df: pd.DataFrame, path: Path, key: str = "date",
                 sort: bool = True, mutable_env: str = "QT_LEDGER_MUTABLE"):
    """freeze_first_write + write to disk. Returns the FreezeReport.

    Convenience for the common case. Kept separate so the merge stays a pure
    function testable without a filesystem.
    """
    merged, report = freeze_first_write(new_df, path, key, mutable_env)
    if merged is None or merged.empty:
        return report
    if sort and key in merged.columns:
        merged = merged.sort_values(key)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(path, index=False)
    return report
