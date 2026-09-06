#!/usr/bin/env python3
"""Availability probe — is the declared 75 bps round trip anywhere near right?

`data/registry/v29_specifications.json` declares a 75 basis point round-trip
cost, applied to turnover inside the criterion. That number is a JUDGEMENT,
and it is the one I flagged as most likely wrong: for a stock trading $50,000
a day the quoted spread alone can be one to three percent, which would make
the assumption optimistic by a factor of three to eight at the thin end.

This measures the quoted spread directly, by ADV bucket, so the assumption can
be checked against evidence.

WHAT A ROUND TRIP COSTS, IN SPREAD TERMS. Buying at the offer and later
selling at the bid pays half the spread on each side, so one full quoted
spread is the round-trip cost of crossing. The declared 75 bps is therefore
compared against the QUOTED SPREAD in bps, not against half of it.

⚠️ THREE REASONS THIS IS AN ESTIMATE, NOT THE ANSWER
  1. The free feed is IEX only. IEX quotes are frequently WIDER than the
     consolidated national best bid and offer, so this OVERSTATES the spread
     for many names -- it is closer to an upper bound than a point estimate.
  2. It is measured today. The declared window runs 2015-2025, and spreads in
     this band have narrowed over that period, so today's number understates
     what the early years cost.
  3. A quoted spread is not an execution. Patient limit orders capture part
     of it; size beyond the top of book pays more than it.

⚠️ AND WHAT THIS PROBE CANNOT DO: change the declared parameter. 75 bps is
signed. If the evidence says the true figure is 300 bps then the specification
is likely to fail on costs, and that is the pre-registration working rather
than breaking. The measurement informs the NEXT specification.

Availability only. Computes no return, writes nothing under data/, spends no K.
"""
from __future__ import annotations

import collections
import os
import statistics
import sys
import time

import pandas as pd

PANEL = os.environ.get("QT_V29_PANEL", "data/universe/v29_universe_pit.csv")
N_PER_BUCKET = int(os.environ.get("QT_SPREAD_N", "60"))
SEED = 20260906
DECLARED_BPS = 75.0
QUOTES_URL = "https://data.alpaca.markets/v2/stocks/quotes/latest"
BATCH = 100

# ADV buckets, chosen to straddle the declared floor and ceiling so the
# question "is 75 bps right at the THIN end" is answerable separately from
# "is it right on average".
BUCKETS = [
    ("50k-100k", 50_000, 100_000),
    ("100k-250k", 100_000, 250_000),
    ("250k-1M", 250_000, 1_000_000),
    ("1M-5M", 1_000_000, 5_000_000),
]


def bucket_of(adv: float) -> "str | None":
    for name, lo, hi in BUCKETS:
        if lo <= adv < hi:
            return name
    return None


def spread_bps(bid: float, ask: float) -> "float | None":
    """Quoted spread in basis points of the mid. None on a crossed or empty book."""
    if bid is None or ask is None:
        return None
    if not (bid > 0 and ask > 0) or ask < bid:
        return None
    mid = 0.5 * (bid + ask)
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10_000.0


def main() -> None:
    import requests
    _BOM = "﻿"
    key = (os.environ.get("ALPACA_API_KEY") or "").replace(_BOM, "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").replace(_BOM, "").strip()
    if not (key and secret):
        print("[spreads] ALPACA_API_KEY / ALPACA_SECRET_KEY not set"); sys.exit(2)
    if not os.path.exists(PANEL):
        print(f"[spreads] missing {PANEL}"); sys.exit(1)

    panel = pd.read_csv(PANEL)
    latest = panel[panel["date"] == panel["date"].max()]
    print(f"[spreads] {PANEL}: {len(latest)} names eligible at {panel['date'].max()}")
    print(f"[spreads] declared round-trip assumption: {DECLARED_BPS:.0f} bps\n")

    import random
    rnd = random.Random(SEED)
    by_bucket: dict = collections.defaultdict(list)
    for _, r in latest.iterrows():
        b = bucket_of(float(r["adv"]))
        if b:
            by_bucket[b].append(str(r["ticker"]).upper())
    sample: dict = {}
    for name, _, _ in BUCKETS:
        names = sorted(set(by_bucket.get(name, [])))
        sample[name] = sorted(rnd.sample(names, min(N_PER_BUCKET, len(names))))
        print(f"  {name:<11} {len(names):>4} eligible, sampling {len(sample[name])}")

    all_syms = sorted({s for v in sample.values() for s in v})
    quotes: dict = {}
    sess = requests.Session()
    sess.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    print(f"\n[spreads] quoting {len(all_syms)} symbols")
    for i in range(0, len(all_syms), BATCH):
        chunk = all_syms[i:i + BATCH]
        try:
            r = sess.get(QUOTES_URL, params={"symbols": ",".join(chunk), "feed": "iex"},
                         timeout=30)
        except requests.RequestException as exc:
            print(f"  batch {i//BATCH+1}: {type(exc).__name__}"); continue
        if r.status_code != 200:
            print(f"  batch {i//BATCH+1}: HTTP {r.status_code} {r.text[:120]}"); continue
        quotes.update((r.json() or {}).get("quotes", {}) or {})
        time.sleep(0.3)
    print(f"[spreads] {len(quotes)} quotes returned\n")

    print("=" * 74)
    print(f"  {'bucket':<12} {'n':<5} {'median':<10} {'p75':<10} {'p90':<10} vs 75 bps")
    print("=" * 74)
    verdicts = {}
    for name, _, _ in BUCKETS:
        vals = []
        for sym in sample[name]:
            q = quotes.get(sym) or {}
            s = spread_bps(q.get("bp"), q.get("ap"))
            if s is not None and s < 5000:
                vals.append(s)
        if not vals:
            print(f"  {name:<12} {'0':<5} no quotes returned")
            continue
        vals.sort()
        med = statistics.median(vals)
        p75 = vals[int(0.75 * (len(vals) - 1))]
        p90 = vals[int(0.90 * (len(vals) - 1))]
        verdicts[name] = med
        ratio = med / DECLARED_BPS
        print(f"  {name:<12} {len(vals):<5} {med:>7.0f}    {p75:>7.0f}    {p90:>7.0f}    "
              f"{ratio:>5.1f}x")

    print("\n" + "=" * 74)
    thin = verdicts.get("50k-100k")
    if thin is None:
        print("🟡 NO QUOTES for the thinnest bucket — the question this probe exists to")
        print("   answer is unanswered, and the declared assumption stands untested there.")
    elif thin > 3 * DECLARED_BPS:
        print(f"🔴 THE DECLARED 75 bps IS OPTIMISTIC AT THE THIN END: median {thin:.0f} bps in")
        print(f"   the $50k-100k bucket, {thin/DECLARED_BPS:.1f}x the assumption. A strategy")
        print(f"   rebalancing quarterly into these names pays that on every round trip, and")
        print(f"   the specification's cost term is too small by roughly that factor.")
        print(f"   THE PARAMETER IS SIGNED AND DOES NOT MOVE. If costs are what sinks this")
        print(f"   read, that is the pre-registration working, and the measured number is")
        print(f"   evidence for the NEXT specification -- a higher floor, or fewer names, or")
        print(f"   a longer holding period.")
    elif thin > DECLARED_BPS:
        print(f"🟡 SOMEWHAT OPTIMISTIC: median {thin:.0f} bps at the thin end vs 75 declared.")
    else:
        print(f"✅ THE ASSUMPTION HOLDS at the thin end: median {thin:.0f} bps vs 75 declared.")
    print("\n   Read all of this as an UPPER BOUND on today's spreads: the free feed is IEX")
    print("   only and IEX quotes are frequently wider than the consolidated NBBO. Read it")
    print("   as a LOWER bound on 2015 spreads, which were wider than today's.")
    print("[spreads] done. No return computed, nothing written, K untouched.")


if __name__ == "__main__":
    main()
