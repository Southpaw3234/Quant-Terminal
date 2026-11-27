#!/usr/bin/env python3
"""Pure-logic checks for the described base rates. No network.

The classes of wrongness this guards:
  1. an entry on a close the filing had not yet reached (look-ahead)
  2. a historical "sale" that is not what OBSERVER calls a sale tonight
  3. outcomes computed for a type nobody forfeited
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, "research")

import pandas as pd

import extract_described_events as x

PASS, FAIL = [], []


def check(name, ok, why=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def test_entry_timing():
    print("\nthe entry is the first close after the filing was public")
    import event_study as es
    bars = pd.Series(1.0, index=pd.bdate_range("2024-03-04", periods=6))
    def entry(acc):
        return bars.index[es._entry_index(bars, x.event_ts_8k(acc))].date().isoformat()
    check("pre-market-enters-the-next-close",
          entry("2024-03-05T12:30:00.000Z") == "2024-03-06",
          "conservative: the first day's reaction is left out")
    check("intraday-enters-the-next-close",
          entry("2024-03-05T17:00:00.000Z") == "2024-03-06")
    check("🔑-after-the-close-enters-the-next-close",
          entry("2024-03-05T21:05:00.000Z") == "2024-03-06",
          "16:05 ET cannot trade at the 16:00 close")
    check("🔑-a-friday-evening-filing-enters-monday",
          entry("2024-03-08T22:00:00.000Z") == "2024-03-11")
    check("🔑-DST-is-respected",
          x.event_ts_8k("2024-01-05T20:30:00Z") == "2024-01-05T15:30:00"
          and x.event_ts_8k("2024-07-05T20:30:00Z") == "2024-07-05T16:30:00",
          "20:30Z is 15:30 ET in January and 16:30 ET in July")
    check("a-bad-timestamp-is-None", x.event_ts_8k("not a time") is None)


def test_item_events():
    print("\n8-K items: originals, one per name per day, items parsed exactly")
    rows = [{"form": "8-K", "filing_date": "2024-05-01", "items": "5.02,9.01",
             "acceptance": "2024-05-01T21:00:00.000Z"},
            {"form": "8-K", "filing_date": "2024-05-01", "items": "5.02",
             "acceptance": "2024-05-01T13:00:00.000Z"},
            {"form": "8-K/A", "filing_date": "2024-06-01", "items": "5.02",
             "acceptance": "2024-06-01T13:00:00.000Z"},
            {"form": "8-K", "filing_date": "2024-07-01", "items": "1.01,1.010",
             "acceptance": "2024-07-01T13:00:00.000Z"},
            {"form": "8-K", "filing_date": "2022-01-03", "items": "5.02",
             "acceptance": "2022-01-03T13:00:00.000Z"},
            {"form": "10-Q", "filing_date": "2024-05-01", "items": "",
             "acceptance": "2024-05-01T13:00:00.000Z"}]
    ev = x.item_events(rows, "AAA", ["5.02", "1.01", "3.01"])
    check("one-event-per-name-per-day-earliest-acceptance",
          len(ev["5.02"]) == 1
          and ev["5.02"][0]["acceptance_utc"].startswith("2024-05-01T13"))
    check("amendments-are-not-events", all(e["filing_date"] != "2024-06-01"
                                           for e in ev["5.02"]))
    check("outside-the-window-is-not-an-event",
          all(e["filing_date"] >= x.WINDOW_START for e in ev["5.02"]))
    check("items-match-exactly-not-by-substring", len(ev["1.01"]) == 1)
    check("no-filing-no-event", ev["3.01"] == [])
    check("event-type-is-OBSERVER's-label",
          ev["5.02"][0]["event_type"] == "8k_item_5.02")


def test_form4_sale():
    print("\nform4_sale is exactly what OBSERVER calls a sale")
    raw = pd.DataFrame([
        # sale only -> sale
        ("AAA", "S", False, "2024-05-01", "acc1"),
        # sale + purchase in one filing -> mixed, not a sale
        ("BBB", "S", False, "2024-05-01", "acc2"),
        ("BBB", "P", False, "2024-05-01", "acc2"),
        # derivative S only -> other
        ("CCC", "S", True, "2024-05-01", "acc3"),
        # tax withholding -> other
        ("DDD", "F", False, "2024-05-01", "acc4"),
        # two sale filings same day -> one event
        ("EEE", "S", False, "2024-05-02", "acc5"),
        ("EEE", "S", False, "2024-05-02", "acc6"),
        # not in universe
        ("ZZZ", "S", False, "2024-05-02", "acc7"),
        # outside window
        ("AAA", "S", False, "2022-05-02", "acc8")],
        columns=["ticker", "code", "is_derivative", "filing_date", "tx_id"])
    raw["is_derivative"] = raw["is_derivative"].astype(str)
    ev = x.form4_sale_events(raw, {"AAA", "BBB", "CCC", "DDD", "EEE"})
    got = dict(zip(ev["ticker"], ev["n_filings"]))
    check("🔑-only-OBSERVER-sales", set(got) == {"AAA", "EEE"}, f"{got}")
    check("a-mixed-filing-is-not-a-sale", "BBB" not in got)
    check("a-derivative-sale-is-not-a-sale", "CCC" not in got)
    check("same-day-filings-are-one-event", got.get("EEE") == 2 and len(ev) == 2)


def test_describe_refuses_unforfeited():
    print("\noutcomes are refused for a type nobody forfeited")
    tmp = tempfile.mkdtemp()
    pd.DataFrame([{"event_id": "a", "ticker": "AAA",
                   "event_ts": "2024-05-01", "event_type": "8k_item_2.02"}]
                 ).to_csv(os.path.join(tmp, "events_8k_item_2.02.csv"), index=False)
    env = dict(os.environ, QT_DESC_DIR=tmp, PYTHONPATH=os.getcwd())
    p = subprocess.run([sys.executable, "research/describe_events.py"],
                       capture_output=True, text=True, env=env)
    check("🔑-an-unforfeited-type-exits-3-before-any-price",
          p.returncode == 3 and "refusing" in p.stdout
          and "prices for" not in p.stdout, p.stdout.strip()[-120:])
    reg = json.load(open("data/registry/described.json", encoding="utf-8"))
    check("2.02-is-kept-out-of-description",
          "8k_item_2.02" in reg["kept_out"]
          and "8k_item_2.02" not in reg["described"])


def main():
    print("=" * 70)
    print("described base rates — pure logic (no network)")
    print("=" * 70)
    test_entry_timing()
    test_item_events()
    test_form4_sale()
    test_describe_refuses_unforfeited()
    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing tested.")


if __name__ == "__main__":
    main()
