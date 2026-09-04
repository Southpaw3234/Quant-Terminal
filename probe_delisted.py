#!/usr/bin/env python3
"""Does the FREE data stack serve delisted tickers? — survivorship probe.

The universe is built from Finnhub's currently-listed symbols, so names that
delisted never enter the pipeline at all. Before concluding that is only
fixable by buying data, two questions are worth one API call each:

  1. Does yfinance return price history for a KNOWN-DELISTED ticker?
     If yes, the missing piece is only a historical ticker LIST, and those
     are obtainable. If no, no list helps and it is a purchase.

  2. Does Finnhub's symbol endpoint expose delisted names at all?

Availability only. Computes no return, spends no K.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

# Names that delisted or were acquired well before today. Mixed causes on
# purpose: bankruptcy, bank failure, and take-private — if only one class comes
# back, that itself is the finding, because bankruptcies are the ones that
# matter for survivorship.
DEAD = {
    "BBBYQ": "Bed Bath & Beyond — Ch.11, 2023",
    "FRCB":  "First Republic Bank — seized 2023",
    "SIVBQ": "SVB Financial — Ch.11, 2023",
    "WEWKQ": "WeWork — Ch.11, 2023",
    "ATVI":  "Activision Blizzard — acquired 2023",
    "VMW":   "VMware — acquired 2023",
}
ALIVE = {"AAPL": "control — must return data"}


def probe_yfinance() -> None:
    import yfinance as yf
    print("=" * 68)
    print("1. yfinance — does it serve DELISTED tickers?")
    print("=" * 68)
    hits = 0
    for tk, why in {**DEAD, **ALIVE}.items():
        try:
            df = yf.download(tk, start="2022-01-01", end="2024-06-30",
                             progress=False, auto_adjust=True, threads=False)
        except Exception as exc:
            print(f"  {tk:<7} ERROR {exc}")
            continue
        n = 0 if df is None else len(df)
        last = "-" if not n else str(df.index[-1].date())
        status = "DATA" if n else "none"
        if n and tk in DEAD:
            hits += 1
        print(f"  {tk:<7} {status:<5} rows={n:<5} last={last:<12} {why}")
    print(f"\n  -> {hits}/{len(DEAD)} delisted tickers returned history")
    if hits:
        print("  ✅ A historical ticker list would be USABLE — the price side works.")
    else:
        print("  🔴 No delisted history. A ticker list alone cannot fix this;")
        print("     recovering dead names requires a paid price source.")


def probe_finnhub(key: str) -> None:
    import requests
    print()
    print("=" * 68)
    print("2. Finnhub — are any delisted names in the symbol list?")
    print("=" * 68)
    r = requests.get("https://finnhub.io/api/v1/stock/symbol",
                     params={"exchange": "US", "token": key}, timeout=60)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
        return
    syms = {str(d.get("symbol", "")).upper() for d in (r.json() or [])
            if isinstance(d, dict)}
    print(f"  {len(syms):,} symbols returned")
    found = sorted(t for t in DEAD if t in syms)
    print(f"  delisted probes present: {found if found else 'NONE'}")
    if not found:
        print("  🔴 Confirms the symbol source is CURRENT-LISTINGS-ONLY:")
        print("     dead names never enter the pipeline to begin with.")


def main() -> None:
    probe_yfinance()
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if key:
        probe_finnhub(key)
    else:
        print("\n(no FINNHUB_API_KEY — skipping the symbol-list probe)")


if __name__ == "__main__":
    main()
