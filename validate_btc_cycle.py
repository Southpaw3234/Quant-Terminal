#!/usr/bin/env python3
"""Pure-logic checks for qt/btc_cycle.py. Synthetic prices, no network.

What this guards: a cycle figure that misplaces the peak (the run-up into
the NEXT halving read as this cycle's top), a partial cycle compared as if
whole, and wording that slides from "past cycles did" into "price will".
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from qt import btc_cycle as b

PASS, FAIL = [], []


def check(name, ok, why=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def synthetic(start="2014-09-17", end="2026-11-27"):
    """Each cycle: rise to a top ~520 days after its halving, fall to a low
    ~900 days after, then a run-up into the next halving that is HIGHER than
    the top -- the shape that fooled the first version."""
    idx = pd.date_range(start, end, freq="D")
    v = np.full(len(idx), 100.0)
    for i, d in enumerate(idx):
        h = max(pd.Timestamp(x) for x in b.HALVINGS if pd.Timestamp(x) <= d) \
            if d >= pd.Timestamp(b.HALVINGS[0]) else pd.Timestamp(b.HALVINGS[0])
        k = (d - h).days
        if k <= 520:
            v[i] = 100 + k
        elif k <= 900:
            v[i] = 620 - (k - 520)
        else:
            v[i] = 240 + 2 * (k - 900)       # ends above 620 before ~1,431 days
    return pd.Series(v, index=idx)


def main():
    print("=" * 70)
    print("qt.btc_cycle — synthetic prices, no network")
    print("=" * 70)
    check("halvings-are-the-block-height-dates",
          b.HALVINGS == ("2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20"))
    check("the-next-halving-is-labelled-an-estimate",
          b.next_halving_estimate() == "2028-04-20" and b.CYCLE_DAYS_EST == 1461)

    d = b.describe(synthetic())
    c16 = next(c for c in d["cycles"] if c["halving"] == "2016-07-09")
    check("🔑-the-peak-is-the-top-not-the-run-into-the-next-halving",
          c16["peak_days_after"] == 520 and c16["peak_multiple"] > 1,
          f"peak at day {c16['peak_days_after']}")
    check("the-low-follows-the-peak", c16["low_days_after"] == 900)
    c12 = d["cycles"][0]
    check("🔑-a-cycle-the-data-starts-inside-is-marked-partial",
          c12["complete_data"] is False and c12["overlay"] == [])
    check("partial-cycles-are-not-used-for-the-windows",
          all(x["halving"] != "2012-11-28" for x in d["same_stage_past"]))
    check("days-since-halving-counts-from-the-last-halving",
          d["days_since_halving"] == (pd.Timestamp("2026-11-27") - pd.Timestamp("2024-04-20")).days)
    check("the-phase-windows-come-from-past-cycles",
          d["phase"]["past_peak_days"] == [520, 520]
          and d["phase"]["low_window"] == [840, 960])
    check("today-in-the-low-window-is-labelled-so",
          d["phase"]["label"] == "low window", d["phase"]["label"])

    ph = b.phase(100, d["cycles"][1:3])
    check("early-in-a-cycle-is-before-past-peaks", ph["label"] == "rising")
    check("no-complete-cycle-means-unknown",
          b.phase(100, [c12])["label"] == "unknown")

    ov = d["cycles"][1]["overlay"]
    check("overlay-starts-at-1x-on-halving-day", ov[0] == [0, 1.0])
    check("overlay-ends-on-the-last-day", ov[-1][0] == c16["n_days"] - 1)
    check("empty-input-is-no_data-not-zero", b.describe(pd.Series(dtype=float))["status"] == "no_data")

    text = d["how_to_read"].lower() + " " + " ".join(
        str(d["phase"]["text"]) for _ in [0]).lower()
    check("🔑-it-says-it-is-a-pattern-not-a-law",
          "not a law" in text and "never been tested" in text)
    banned = ("buy", "sell", "hold", "target", "signal", "will rise", "will fall")
    keys = set()
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                keys.add(k.lower()); walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(d)
    check("🔑-no-key-is-a-verdict", not any(x in k for k in keys for x in banned), "")
    check("phase-text-describes-the-past",
          all("past" in b.phase(k, d["cycles"][1:3])["text"] or "earlier" in b.phase(k, d["cycles"][1:3])["text"]
              for k in (100, 520, 700, 900, 1200)))

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing forecast.")


if __name__ == "__main__":
    main()
