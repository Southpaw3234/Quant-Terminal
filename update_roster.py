#!/usr/bin/env python3
"""Weekly incremental roster update — append-only, watermarked.

The full sweep is five queries across ten years plus a classification pass over
1,840 names: about 25 minutes. This asks only what has appeared since the last
run, so it finishes in seconds however much history has accumulated.

⚠️ THIS WRITES TO THE REPOSITORY ON A SCHEDULE, which is the exact shape that
has damaged this repo four times -- duplicate retrains from catch-up crons,
twice more from wake-triggered tasks, and a stale-base checkout that erased 921
rows of prediction history. So:

  * the merge is APPEND-ONLY and proven so by validate_qt_roster_update.py;
  * a repeated run is a no-op rather than a doubling;
  * an existing row is never reclassified, and a disagreement is REPORTED
    instead of applied;
  * the live file is never what a specification reads. Specifications name a
    DATED SNAPSHOT, taken deliberately, because a read taken on Tuesday must
    still be reproducible on Wednesday.

Reuses the tested enumeration and classification from build_delisted_roster,
rather than a second copy that could drift from it.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

import pandas as pd

import build_delisted_roster as base
from qt import roster_update as ru

ROSTER = os.environ.get("QT_ROSTER_FILE", "data/universe/delisted_roster_2015.csv")
SNAPSHOT = os.environ.get("QT_ROSTER_SNAPSHOT", "").strip() == "1"
TODAY = os.environ.get("QT_ROSTER_TODAY", time.strftime("%Y-%m-%d"))


def main() -> None:
    if not os.path.exists(ROSTER):
        print(f"[roster-update] {ROSTER} does not exist — run the full sweep first")
        sys.exit(1)
    existing = pd.read_csv(ROSTER)
    since = ru.watermark(existing)
    print(f"[roster-update] {ROSTER}: {len(existing):,} rows")
    print(f"[roster-update] buckets: {ru.summarize(existing)}")
    print(f"[roster-update] querying from {since} (newest recorded filing minus "
          f"{ru.LOOKBACK_DAYS}d, because EDGAR indexes late)\n")

    # Narrow the module's window to the incremental slice, then reuse its
    # enumeration wholesale. Rewriting it here is how two copies drift.
    base.WINDOW_START = since or base.WINDOW_START
    base.WINDOW_END = TODAY
    sess = __import__("requests").Session()
    sess.headers.update({"User-Agent": base.UA, "Accept-Encoding": "gzip, deflate"})

    cands = base.enumerate_candidates(sess)
    known = {str(c) for c in existing["cik"].tolist()}
    fresh = {cik: c for cik, c in cands.items() if str(cik) not in known}
    print(f"\n[roster-update] {len(cands)} issuers in the window; "
          f"{len(fresh)} not already on the roster")
    if not fresh:
        print("[roster-update] nothing new. The roster is unchanged, and that is a")
        print("                RESULT rather than a failure — most weeks will look "
              "like this.")
        if SNAPSHOT:
            _snapshot(existing)
        return

    rows = []
    for cik, c in sorted(fresh.items(), key=lambda kv: kv[1]["date"]):
        sym = c["tickers"][0]
        pf = base.price_facts(sess, sym)
        time.sleep(base.SLEEP_YF)
        matched = base.name_matches(c["issuer"], pf["name"])
        st = base.classify(pf["exchange"], pf["n"] > 0, matched)
        after = 0
        if c["date"] and pf["ts"]:
            cut = pd.Timestamp(c["date"]).timestamp()
            after = sum(1 for t in pf["ts"] if t > cut)
        rows.append({
            "cik": cik, "ticker": sym, "all_tickers": "|".join(c["tickers"]),
            "issuer_edgar": c["issuer"], "name_source": pf["name"],
            "name_match": bool(matched), "form": c["form"], "form25_date": c["date"],
            "exchange_now": pf["exchange"], "first_bar": pf["first"],
            "last_bar": pf["last"], "bars_total": pf["n"],
            "bars_after_form25": after, "status": st,
        })

    merged, rep = ru.merge_append_only(existing, pd.DataFrame(rows))
    print("\n" + "=" * 70)
    print(f"  existing        {rep['existing']:,}")
    print(f"  added           {rep['added']}")
    print(f"  already present {rep['already_present']}")
    if rep["would_have_changed"]:
        print(f"  ⚠️ REFUSED to reclassify {len(rep['would_have_changed'])} existing "
              f"row(s): {rep['would_have_changed'][:5]}")
        print("     Recorded, not applied. A row that can change is a row that can")
        print("     silently disagree with the read that used it.")
    if rep["added_ciks"]:
        print(f"  new CIKs: {', '.join(rep['added_ciks'])}")
    merged.to_csv(ROSTER, index=False)
    print(f"\n[roster-update] wrote {ROSTER} ({len(merged):,} rows)")
    print(f"[roster-update] buckets now: {ru.summarize(merged)}")
    if SNAPSHOT:
        _snapshot(merged)


def _snapshot(df: pd.DataFrame) -> None:
    path = ru.snapshot_name(ROSTER, TODAY)
    if os.path.exists(path):
        print(f"[roster-update] snapshot {path} already exists — NOT overwriting. "
              f"A dated snapshot is immutable by definition.")
        return
    df.to_csv(path, index=False)
    print(f"[roster-update] SNAPSHOT {path} ({len(df):,} rows) — this is what a "
          f"specification names, never the live file.")


if __name__ == "__main__":
    main()
