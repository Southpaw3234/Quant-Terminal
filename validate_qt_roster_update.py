#!/usr/bin/env python3
"""Validation for qt.roster_update — append-only merge. No network, no files.

One property carries this: a scheduled job that writes to the repo must only
ever EXTEND. This repo has been damaged four times by crons that rewrote what
they should have appended to, once erasing 921 rows of prediction history.

So the suite is built around trying to make the merge lose or mutate a row,
and checking that it cannot.
"""
from __future__ import annotations

import sys

import pandas as pd

from qt import roster_update as ru

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


def frame(rows):
    return pd.DataFrame(rows)


EXISTING = frame([
    {"cik": 111, "ticker": "AAA", "form25_date": "2024-01-10", "status": "otc-continuation"},
    {"cik": 222, "ticker": "BBB", "form25_date": "2024-06-20", "status": "no-data"},
])


def test_watermark():
    print("\n--- where the next query starts ---")
    check("watermark-looks-back",
          ru.watermark(EXISTING) == "2024-06-06",
          "newest filing is 2024-06-20; the watermark is 14 days earlier, "
          "because EDGAR indexes filings LATE and starting at the newest date "
          "would miss them permanently")
    check("watermark-none-on-empty",
          ru.watermark(pd.DataFrame()) is None
          and ru.watermark(None) is None,
          "no roster yet means no watermark, and the caller sweeps from the start")
    check("watermark-tolerates-bad-dates",
          ru.watermark(frame([{"cik": 1, "form25_date": "not-a-date"},
                              {"cik": 2, "form25_date": "2025-01-01"}])) == "2024-12-18",
          "an unparseable date is skipped rather than poisoning the maximum")
    check("watermark-lookback-is-a-parameter",
          ru.watermark(EXISTING, lookback_days=0) == "2024-06-20",
          "the overlap window is explicit")


def test_append_only():
    print("\n--- THE property: existing rows are never touched ---")
    incoming = frame([
        {"cik": 222, "ticker": "BBB", "form25_date": "2024-06-20",
         "status": "otc-continuation"},                      # RECLASSIFIED
        {"cik": 333, "ticker": "CCC", "form25_date": "2025-02-02",
         "status": "otc-continuation"},                      # new
    ])
    merged, rep = ru.merge_append_only(EXISTING, incoming)
    check("adds-the-new-row",
          len(merged) == 3 and 333 in set(merged["cik"]),
          f"{rep['added']} added, {rep['already_present']} already present")
    old_bbb = merged[merged["cik"] == 222].iloc[0]
    check("does-not-reclassify-an-existing-row",
          old_bbb["status"] == "no-data",
          "BBB came back classified differently and KEPT its original value. A "
          "row that can change is a row that can silently disagree with the "
          "read that used it")
    check("records-what-it-refused-to-change",
          any("222" in s for s in rep["would_have_changed"]),
          f"the disagreement is REPORTED rather than hidden: "
          f"{rep['would_have_changed']}")
    check("never-loses-a-row",
          set(EXISTING["cik"]).issubset(set(merged["cik"])),
          "every pre-existing CIK survives the merge")
    check("no-duplicate-ciks",
          merged["cik"].duplicated().sum() == 0,
          "a CIK appears once, however many times it arrives")


def test_idempotent():
    print("\n--- running twice changes nothing ---")
    incoming = frame([{"cik": 333, "ticker": "CCC", "form25_date": "2025-02-02",
                       "status": "otc-continuation"}])
    once, _ = ru.merge_append_only(EXISTING, incoming)
    twice, rep2 = ru.merge_append_only(once, incoming)
    check("second-run-is-a-no-op",
          len(twice) == len(once) and rep2["added"] == 0,
          f"re-applying the same batch adds {rep2['added']} rows. A weekly cron "
          f"that fires twice — which catch-up scheduling has caused here three "
          f"times — must not double the file")
    check("no-op-is-reported-as-such",
          rep2["already_present"] == 1,
          "and it says the row was already present rather than looking like a "
          "run that failed silently")


def test_edges():
    print("\n--- edges ---")
    empty_in, rep = ru.merge_append_only(EXISTING, pd.DataFrame())
    check("empty-incoming-preserves-existing",
          len(empty_in) == 2 and rep["added"] == 0,
          "a week with no new delistings leaves the roster exactly as it was")
    first, rep2 = ru.merge_append_only(pd.DataFrame(), EXISTING)
    check("empty-existing-takes-everything",
          len(first) == 2 and rep2["added"] == 2,
          "the first ever run seeds the file")
    check("none-existing-is-safe",
          len(ru.merge_append_only(None, EXISTING)[0]) == 2,
          "a missing roster is treated as empty rather than raising")


def test_snapshot_and_summary():
    print("\n--- dated snapshots and the summary ---")
    n = ru.snapshot_name("data/universe/delisted_roster_2015.csv", "2026-10-01")
    check("snapshot-is-dated-and-separate",
          n == "data/universe/delisted_roster_2015_snap_2026-10-01.csv",
          f"{n} — a specification points at THIS, never at the live file, or a "
          f"read taken on Tuesday cannot be reproduced on Wednesday")
    check("snapshot-handles-a-bare-stem",
          ru.snapshot_name("roster", "2026-10-01") == "roster_snap_2026-10-01.csv",
          "a name without .csv still works")
    s = ru.summarize(EXISTING)
    check("summary-counts-buckets",
          s == {"otc-continuation": 1, "no-data": 1},
          f"{s}")
    check("summary-safe-on-empty",
          ru.summarize(pd.DataFrame()) == {},
          "an empty frame summarises to nothing rather than raising")


def main():
    print("=" * 70)
    print("qt.roster_update — append-only incremental merge (no network)")
    print("=" * 70)
    test_watermark()
    test_append_only()
    test_idempotent()
    test_edges()
    test_snapshot_and_summary()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("An existing row cannot be reclassified or lost, a repeated run is a")
    print("no-op, and a disagreement is recorded rather than applied.")


if __name__ == "__main__":
    main()
