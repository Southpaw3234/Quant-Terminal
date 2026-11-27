#!/usr/bin/env python3
"""Outcome ledgers for the event types in data/registry/described.json.

DESCRIPTION, NOT A TEST. No QT_SPEC_ID, no Referee, no criterion, no K. It
refuses any event type that the registry does not list as forfeited, so it
cannot be used to look at outcomes for a type that might still be tested.

    data/events/described/events_<type>.csv
      -> data/events/described/outcomes_<type>_<h>.csv     h in 21, 63

The engine is event_study.py's own (`_load_prices`, `_dedupe_overlaps`,
`run_study`), imported rather than copied, so a described base rate and a
pre-registered read are computed by identical code. Ledgers are frozen on
first write through qt.ledger, keyed on event_id.

    python research/describe_events.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("QT_SPEC_ID", None)      # nothing here is a read

import pandas as pd

REGISTRY = "data/registry/described.json"
DIR = Path(os.environ.get("QT_DESC_DIR", "data/events/described"))


def allowed_types(registry: dict) -> list:
    return sorted(set((registry or {}).get("described") or {})
                  | set((registry or {}).get("reference") or {}))


def main() -> None:
    import event_study as es
    from qt import ledger as qt_ledger

    reg = json.load(open(REGISTRY, encoding="utf-8"))
    horizons = [int(h) for h in reg["common"]["horizons_bars"]]
    files = sorted(DIR.glob("events_*.csv"))
    types = {f.stem[len("events_"):]: f for f in files}
    for t in types:
        if t not in allowed_types(reg):
            print(f"::error::{t} is not in {REGISTRY} -- refusing to compute "
                  f"outcomes for an event type that has not been forfeited")
            sys.exit(3)

    events = {t: pd.read_csv(f, keep_default_na=False, dtype={"ticker": str})
              for t, f in types.items()}
    tickers = sorted(set().union(*(set(e["ticker"]) for e in events.values()))
                     | {es.BENCH})
    start = min(str(e["event_ts"].min())[:10] for e in events.values())
    print(f"[describe] {len(types)} type(s), {len(tickers)} tickers, from {start}")
    prices = es._load_prices(tickers, start)
    print(f"[describe] prices for {len(prices)} of {len(tickers)}")

    for t, ev in sorted(events.items()):
        ev = ev.copy()
        ev["event_ts"] = pd.to_datetime(ev["event_ts"])
        ev = ev.sort_values("event_ts").reset_index(drop=True)
        for h in horizons:
            kept = es._dedupe_overlaps(ev, prices, h)
            res = es.run_study(kept, prices, h) if len(kept) else pd.DataFrame()
            out = DIR / f"outcomes_{t}_{h}.csv"
            if res.empty:
                print(f"[describe] {t} h={h}: no settled events")
                continue
            rep = qt_ledger.write_ledger(res, out, key="event_id")
            if getattr(rep, "frozen", None):
                print(f"[describe] {t} h={h}: FROZE {len(rep.frozen)} "
                      f"recomputed value(s) -- written rows do not move")
            print(f"[describe] {t} h={h}: {len(ev)} events -> {len(kept)} "
                  f"independent -> {len(res)} settled -> {out}")
    print("[describe] described, not tested. K unchanged.")


if __name__ == "__main__":
    main()
