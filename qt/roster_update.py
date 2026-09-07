"""Incremental roster maintenance — append-only, watermarked. V29.

The roster is built by sweeping ten years of EDGAR filings, which takes about
25 minutes. Doing that weekly to discover the handful of new delistings is
waste, and doing it hourly would burn twelve hours of compute a day. The
incremental version asks only "what has appeared since last time", so the run
is seconds regardless of how much history has accumulated.

APPEND-ONLY, AND WHY THAT IS NOT A STYLE CHOICE. This repo has been damaged
four separate times by scheduled jobs writing to it: duplicate retrains from
catch-up crons, twice more from wake-triggered tasks, and a stale-base
checkout that ERASED 921 rows of prediction history before anyone noticed.
Every one was a cron that rewrote a file it should only have extended.

So an existing row is never modified and never removed. A CIK already in the
roster keeps the classification it was given, even if a later run would judge
it differently -- because a row that can change is a row that can silently
disagree with the read that used it.

⚠️ AND THE ROSTER IS A RESEARCH INPUT, WHICH MOVES SLOWLY BY DESIGN. A
specification names a DATED SNAPSHOT, not the live file. The live file
accumulates; the snapshot does not. Without that split a read taken on Tuesday
cannot be reproduced on Wednesday, and "the universe" stops being something
you can point at.

Pure functions. No network, no files, no clock.
"""
from __future__ import annotations

import pandas as pd

# Late indexing is real: a filing can appear in EDGAR's index days after its
# filing date. Re-querying a window BEFORE the watermark is how those get
# caught, and append-only semantics make the overlap free -- a row already
# present is simply not re-added.
LOOKBACK_DAYS = 14

KEY = "cik"


def watermark(existing: pd.DataFrame, lookback_days: int = LOOKBACK_DAYS) -> "str | None":
    """Where the next incremental query should start.

    The newest filing date already recorded, MINUS a lookback. Starting
    exactly at the newest date would miss anything indexed late, and this data
    is indexed late often enough to matter.
    """
    if existing is None or existing.empty or "form25_date" not in existing.columns:
        return None
    dates = pd.to_datetime(existing["form25_date"], errors="coerce").dropna()
    if dates.empty:
        return None
    return (dates.max() - pd.Timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")


def merge_append_only(existing: pd.DataFrame, incoming: pd.DataFrame) -> tuple:
    """-> (merged, report). Existing rows are NEVER touched.

    `report` carries what happened, so a run that changes nothing says so
    rather than looking identical to one that failed silently.
    """
    report = {"existing": 0, "added": 0, "already_present": 0,
              "would_have_changed": [], "added_ciks": []}
    if incoming is None or incoming.empty:
        return (existing if existing is not None else pd.DataFrame()), report
    if existing is None or existing.empty:
        report["added"] = len(incoming)
        report["added_ciks"] = [str(c) for c in incoming[KEY].tolist()][:20]
        return incoming.reset_index(drop=True), report

    report["existing"] = len(existing)
    have = {str(c) for c in existing[KEY].tolist()}
    fresh_rows, dupes = [], 0
    old_by_cik = {str(r[KEY]): r for _, r in existing.iterrows()}
    for _, row in incoming.iterrows():
        cik = str(row[KEY])
        if cik in have:
            dupes += 1
            old = old_by_cik.get(cik)
            if old is not None and "status" in row and "status" in old:
                if str(row["status"]) != str(old["status"]):
                    # Recorded, NOT applied. A row that can change is a row
                    # that can silently disagree with the read that used it.
                    report["would_have_changed"].append(
                        f"{cik}: {old['status']} -> {row['status']}")
            continue
        fresh_rows.append(row)
        have.add(cik)
    report["already_present"] = dupes
    report["added"] = len(fresh_rows)
    report["added_ciks"] = [str(r[KEY]) for r in fresh_rows][:20]
    if not fresh_rows:
        return existing.reset_index(drop=True), report
    merged = pd.concat([existing, pd.DataFrame(fresh_rows)], ignore_index=True)
    return merged.reset_index(drop=True), report


def snapshot_name(base: str, asof: str) -> str:
    """`delisted_roster_2015.csv` + 2026-10-01 -> `..._snap_2026-10-01.csv`.

    A specification points at one of these, never at the live file.
    """
    stem = base[:-4] if base.endswith(".csv") else base
    return f"{stem}_snap_{str(asof)[:10]}.csv"


def summarize(df: pd.DataFrame) -> dict:
    """Bucket counts, for a report that is legible without opening the file."""
    if df is None or df.empty or "status" not in df.columns:
        return {}
    return {str(k): int(v) for k, v in df["status"].value_counts().items()}
