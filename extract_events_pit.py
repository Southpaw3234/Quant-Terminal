#!/usr/bin/env python3
"""Point-in-time event discovery — step 3 of the survivorship fix.

Steps 1 and 2 produced a roster of companies that left, and a month-by-month
record of who actually passed the screen when. This is what those were for:
discover events over the universe that EXISTED at the time, instead of over
the universe that exists now.

Two corrections happen here at once, and they pull in opposite directions:

  ADDS   events on companies that were eligible then and are gone now. That
         is the survivorship fix.
  DROPS  events on companies that are eligible NOW but were not then --
         mostly names whose 2026 dollar volume put them in the static list
         while their 2023 volume did not. Step 2 measured this as the larger
         of the two biases: 413 names eligible at 2023-09-30 against 907 in
         the static file.

⚠️ THIS DOES NOT REPLACE SPEC #3'S INPUT AND MUST NOT BE POINTED AT IT.
`pead_8k_car_v3` declares `data/events/events_8k_car_1095d.csv`, that file is
frozen, and the read job's INPUT GUARD checks it against the registry. This
writes a DIFFERENT file. Using it for spec #3 would be a different
specification wearing spec #3's name, which is the exact substitution the
pre-registration exists to prevent. It is an input for a FUTURE spec.

WHAT IT REUSES RATHER THAN REIMPLEMENTS
----------------------------------------
The event definition is spec #3's and the pure logic is imported from
`extract_8k_car`: the reacting session, the two-session CAR, the +5.0% bar,
the A+2 entry. Those are covered by 32 tests and a second copy would drift
from them. The ONLY thing added here is the eligibility filter.

Prints the set difference against the frozen spec #3 event file, which is
free: comparing which events exist is not computing what they returned.

Writes data/events/events_8k_car_pit_1095d.csv. No return computed, no K.
"""
from __future__ import annotations

import collections
import os
import sys
import time

import pandas as pd

import extract_8k_car as base

PANEL = os.environ.get("QT_PIT_PANEL", "data/universe/v27_universe_pit.csv")
FROZEN = os.environ.get("QT_FROZEN_EVENTS", "data/events/events_8k_car_1095d.csv")
OUT_CSV = os.environ.get("QT_PIT_EVENTS_OUT", "data/events/events_8k_car_pit_1095d.csv")
LIMIT = int(os.environ.get("QT_PIT_EVENTS_LIMIT", "0"))
if LIMIT:
    OUT_CSV = OUT_CSV.replace(".csv", f"_SMOKE{LIMIT}.csv")
EVENT_TYPE = "8k_item202_car_ge5_pit"


# ═══════════════════════════════════════════════════ pure logic (no network)

def load_panel(path: str) -> tuple:
    """-> (eligible_pairs, grid, tickers). Membership as a set of (ticker, date)."""
    df = pd.read_csv(path)
    pairs = {(str(t).upper(), str(d)) for t, d in zip(df["ticker"], df["date"])}
    grid = sorted({str(d) for d in df["date"]})
    tickers = sorted({str(t).upper() for t in df["ticker"]})
    return pairs, grid, tickers


def governing_month_end(day, grid) -> "str | None":
    """The last month-end STRICTLY BEFORE `day`.

    Strictly before is the whole point: membership decided ON the same day an
    event happens would let the event's own month inform its eligibility. A
    month of staleness is the safe error; look-ahead is not.
    """
    d = str(day)[:10]
    prior = [g for g in grid if g < d]
    return prior[-1] if prior else None


def eligible_at(pairs, grid, ticker: str, day) -> bool:
    m = governing_month_end(day, grid)
    return bool(m) and (str(ticker).upper(), m) in pairs


def filter_events(rows, pairs, grid) -> tuple:
    """Keep only events whose name was eligible at the governing month-end."""
    kept, stats = [], collections.Counter()
    for r in rows:
        stats["in"] += 1
        if eligible_at(pairs, grid, r["ticker"], r["day_A"]):
            r = dict(r); r["event_type"] = EVENT_TYPE
            kept.append(r); stats["kept"] += 1
        else:
            stats["dropped_not_eligible_then"] += 1
    return kept, stats


def compare_to_frozen(pit_rows, frozen_path: str) -> dict:
    """Set difference on (ticker, day_A). Which events exist, not what they did."""
    out = {"frozen_rows": 0, "pit_rows": len(pit_rows), "common": 0,
           "only_frozen": 0, "only_pit": 0, "only_pit_tickers": []}
    if not os.path.exists(frozen_path):
        return out
    fz = pd.read_csv(frozen_path)
    fkeys = {(str(t).upper(), str(d)[:10]) for t, d in zip(fz["ticker"], fz["day_A"])}
    pkeys = {(str(r["ticker"]).upper(), str(r["day_A"])[:10]) for r in pit_rows}
    out["frozen_rows"] = len(fkeys)
    out["common"] = len(fkeys & pkeys)
    out["only_frozen"] = len(fkeys - pkeys)
    out["only_pit"] = len(pkeys - fkeys)
    out["only_pit_tickers"] = sorted({t for t, _ in (pkeys - fkeys)})[:20]
    return out


# ═══════════════════════════════════════════════════════════ network shell

def main() -> None:
    for p in (PANEL,):
        if not os.path.exists(p):
            print(f"[pit-events] missing {p} — run step 2 first"); sys.exit(1)
    pairs, grid, tickers = load_panel(PANEL)
    if LIMIT:
        tickers = tickers[:LIMIT]
    print(f"[pit-events] panel {PANEL}: {len(pairs):,} eligible name-months, "
          f"{len(grid)} month-ends, {len(tickers)} distinct names")
    print(f"[pit-events] event definition imported from extract_8k_car "
          f"(CAR >= {base.CAR_THRESHOLD:+.1%}, entry A+2, horizon set by the spec)\n")

    sess = base._session()
    t0 = time.time()
    anns, resolved = [], 0
    for k, tk in enumerate(tickers):
        cik = base.resolve_cik(sess, tk)
        if cik is None:
            continue
        resolved += 1
        anns.extend(base.fetch_announcements(sess, tk, cik))
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(tickers)} names, {resolved} resolved, "
                  f"{len(anns)} announcements, {time.time()-t0:.0f}s")
    print(f"\n[pit-events] {resolved}/{len(tickers)} resolved; {len(anns)} announcements "
          f"in {time.time()-t0:.0f}s")
    if not anns:
        print("[pit-events] no announcements — refusing to write"); sys.exit(1)

    with_events = sorted({a["ticker"] for a in anns})
    print(f"[pit-events] pricing {len(with_events)} names + {base.BENCHMARK}")
    prices = base.fetch_prices(with_events)
    if base.BENCHMARK not in prices:
        print(f"[pit-events] no {base.BENCHMARK} history"); sys.exit(1)
    sessions = prices[base.BENCHMARK].index

    rows, stats, cars = base.build_rows(anns, prices, sessions)
    print(f"\n[pit-events] {len(rows)} events clear the bar BEFORE the eligibility filter")
    kept, fstats = filter_events(rows, pairs, grid)
    print(f"[pit-events] {fstats['kept']} remain after it; "
          f"{fstats['dropped_not_eligible_then']} dropped as NOT ELIGIBLE at the time")
    if not kept:
        print("[pit-events] nothing survived — refusing to write"); sys.exit(1)

    df = pd.DataFrame(kept).sort_values(["event_ts", "ticker"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    cmp = compare_to_frozen(kept, FROZEN)
    print("\n" + "=" * 74)
    print("WHAT THE POINT-IN-TIME UNIVERSE CHANGES ABOUT THE EVENT SET")
    print("=" * 74)
    print(f"  frozen spec #3 events   {cmp['frozen_rows']:>6}")
    print(f"  point-in-time events    {cmp['pit_rows']:>6}")
    print(f"  in both                 {cmp['common']:>6}")
    print(f"  ONLY in the frozen set  {cmp['only_frozen']:>6}  <- names not screen-eligible "
          f"when their event happened")
    print(f"  ONLY point-in-time      {cmp['only_pit']:>6}  <- events the static universe "
          f"could not have seen")
    if cmp["only_pit_tickers"]:
        print(f"  examples added: {', '.join(cmp['only_pit_tickers'][:14])}")
    if cmp["frozen_rows"]:
        churn = 100 * (cmp["only_frozen"] + cmp["only_pit"]) / cmp["frozen_rows"]
        print(f"\n  {churn:.0f}% of spec #3's event set changes identity under a point-in-time "
              f"universe.")
        print(f"  That is a statement about WHICH EVENTS EXIST. It says nothing about what they")
        print(f"  returned, and nothing here computes that.")
    by_year = collections.Counter(str(r["event_ts"])[:4] for r in kept)
    print(f"\n  by year: {dict(sorted(by_year.items()))}; "
          f"{df['ticker'].nunique()} distinct tickers")
    print(f"\n[pit-events] wrote {OUT_CSV} ({len(df)} rows)")
    print("  ⚠️ NOT spec #3's declared input. That file is frozen and the read job's INPUT")
    print("  GUARD checks it against the registry. This is an input for a FUTURE spec.")
    print("[pit-events] SELECTION only — no forward return computed, K untouched.")


if __name__ == "__main__":
    main()
