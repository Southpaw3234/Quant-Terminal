#!/usr/bin/env python3
"""Is the declared 75 bps round trip anywhere near right? Measured historically.

`data/registry/v29_specifications.json` declares a 75 basis point round-trip
cost. That number is a judgement and it is the one most likely to be wrong: in
a stock trading $50,000 a day the spread alone can be a large multiple of it.

⚠️ THIS IS THE SECOND VERSION. The first asked the broker for live quotes and
reported a median of 3,336 bps -- a 33% spread -- with essentially the SAME
figure for every liquidity bucket from $50k a day to $5M a day. Identical
spreads across a hundredfold range of liquidity is not a finding, it is a
broken measurement: the run landed on a Sunday and a closed-market quote is
stale, one-sided, or both. That number is void and is not used anywhere.

Quoting the market was the wrong instrument regardless of the day, for two
reasons that a retry would not have fixed:

  * a live quote describes TODAY, while the declared window runs 2015-2025 and
    spreads in this band have narrowed over it; and
  * it requires the market to be open, making the answer depend on when the
    job happens to run.

So the spread is estimated from daily HIGH and LOW bars using Corwin-Schultz,
which works on history, needs no quote feed, and can be computed for the
actual window the backtest covers. `qt.liquidity` holds the estimator and
`validate_qt_liquidity.py` proves it recovers a known spread on synthetic
bars -- the test the first version did not have, which is why nothing caught
3,336 bps.

⚠️ AND WHAT THIS CANNOT DO: change the declared parameter. 75 bps is signed.
If the evidence says the true figure is far higher, the specification is
likely to fail on costs, and that is the pre-registration working rather than
breaking. The measurement is evidence for the NEXT specification.

Availability only. Computes no return, writes nothing under data/, spends no K.
"""
from __future__ import annotations

import collections
import os
import statistics
import sys
import time

import numpy as np
import pandas as pd

from qt import liquidity as lq

PANEL = os.environ.get("QT_V29_PANEL", "data/universe/v29_universe_pit.csv")
N_PER_BUCKET = int(os.environ.get("QT_SPREAD_N", "40"))
SEED = 20260906
DECLARED_BPS = 75.0
# The declared window. Spreads are estimated over it, not over today.
HIST_START = os.environ.get("QT_SPREAD_START", "2015-09-01")
HIST_END = os.environ.get("QT_SPREAD_END", "2025-09-01")

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


def main() -> None:
    if not os.path.exists(PANEL):
        print(f"[spreads] missing {PANEL}"); sys.exit(1)
    panel = pd.read_csv(PANEL)
    print(f"[spreads] {PANEL}: {len(panel):,} eligible name-months")
    print(f"[spreads] declared round-trip assumption: {DECLARED_BPS:.0f} bps")
    print(f"[spreads] estimating over {HIST_START}..{HIST_END} — the DECLARED window,")
    print(f"          not today, because spreads in this band have narrowed\n")

    # Bucket each name by its MEDIAN adv across the window, so a name is
    # classified by what it typically traded rather than by one month.
    med_adv = panel.groupby("ticker")["adv"].median()
    by_bucket = collections.defaultdict(list)
    for tk, adv in med_adv.items():
        b = bucket_of(float(adv))
        if b:
            by_bucket[b].append(str(tk).upper())

    import random
    rnd = random.Random(SEED)
    sample = {}
    for name, _, _ in BUCKETS:
        names = sorted(set(by_bucket.get(name, [])))
        sample[name] = sorted(rnd.sample(names, min(N_PER_BUCKET, len(names))))
        print(f"  {name:<11} {len(names):>5} names, sampling {len(sample[name])}")

    import yfinance as yf
    all_syms = sorted({s for v in sample.values() for s in v})
    print(f"\n[spreads] downloading H/L bars for {len(all_syms)} names")
    bars = {}
    for i in range(0, len(all_syms), 100):
        chunk = all_syms[i:i + 100]
        try:
            raw = yf.download(chunk, start=HIST_START, end=HIST_END, auto_adjust=True,
                              progress=False, threads=True, group_by="column")
        except Exception as exc:
            print(f"  chunk {i//100+1}: {type(exc).__name__}"); continue
        if raw is None or len(raw) == 0:
            continue
        multi = isinstance(raw.columns, pd.MultiIndex)
        for tk in chunk:
            try:
                h = raw["High"][tk] if multi else raw["High"]
                l = raw["Low"][tk] if multi else raw["Low"]
            except (KeyError, TypeError):
                continue
            h, l = pd.to_numeric(h, errors="coerce"), pd.to_numeric(l, errors="coerce")
            if h.notna().sum() >= 100:
                bars[tk] = (h, l)
        time.sleep(0.2)
    print(f"[spreads] {len(bars)} names with usable bars\n")

    print("=" * 78)
    print(f"  {'bucket':<12} {'n':<5} {'median':<10} {'p25':<9} {'p75':<9} "
          f"{'neg%':<7} vs 75 bps")
    print("=" * 78)
    medians = {}
    for name, _, _ in BUCKETS:
        vals, negs = [], []
        for sym in sample[name]:
            if sym not in bars:
                continue
            est = lq.spread_estimate(*bars[sym])
            if np.isfinite(est["spread_bps"]):
                vals.append(est["spread_bps"]); negs.append(est["pct_negative"])
        if not vals:
            print(f"  {name:<12} {'0':<5} no usable bars")
            continue
        vals.sort()
        med = statistics.median(vals)
        medians[name] = med
        print(f"  {name:<12} {len(vals):<5} {med:>7.0f}    {vals[len(vals)//4]:>6.0f}   "
              f"{vals[3*len(vals)//4]:>6.0f}   {statistics.mean(negs):>5.0%}   "
              f"{med/DECLARED_BPS:>5.1f}x")

    print("\n" + "=" * 78)
    thin = medians.get("50k-100k")
    thick = medians.get("1M-5M")
    if thin is None:
        print("🟡 NO ESTIMATE for the thinnest bucket — the question is unanswered.")
    else:
        if thick is not None and thin > thick:
            print(f"  Sanity: thin names estimate WIDER than liquid ones "
                  f"({thin:.0f} vs {thick:.0f} bps), which is the direction a real")
            print(f"  spread runs. The live-quote version failed exactly here.")
        if thin > 3 * DECLARED_BPS:
            print(f"\n🔴 THE DECLARED 75 bps IS TOO LOW AT THE THIN END: median "
                  f"{thin:.0f} bps, {thin/DECLARED_BPS:.1f}x the assumption.")
            print(f"   A quarterly round trip in those names costs multiples of what the")
            print(f"   specification charges, so the cost term is understated for exactly")
            print(f"   the part of the universe the widened floor was meant to reach.")
            print(f"   THE PARAMETER IS SIGNED AND DOES NOT MOVE. If costs sink the read,")
            print(f"   that is the pre-registration working, and this number is evidence")
            print(f"   for the NEXT specification — a higher floor, fewer names, or a")
            print(f"   longer holding period.")
        elif thin > DECLARED_BPS:
            print(f"\n🟡 SOMEWHAT LOW: median {thin:.0f} bps at the thin end vs 75 declared.")
        else:
            print(f"\n✅ THE ASSUMPTION HOLDS: median {thin:.0f} bps at the thin end.")
    print("\n   ⚠️ THE ESTIMATOR HAS A FLOOR OF ROUGHLY 60 bps. Clipping the negative half")
    print("   of a distribution centred on zero leaves a positive mean, so any estimate")
    print("   near 60 is indistinguishable from no spread at all. Only the EXCESS above")
    print("   that floor carries information, and at this resolution the ordering between")
    print("   adjacent buckets is not reliable.")
    print("[spreads] done. No return computed, nothing written, K untouched.")


if __name__ == "__main__":
    main()
