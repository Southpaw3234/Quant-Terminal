#!/usr/bin/env python3
"""V29 point-in-time universe — widened floor, ten-year window.

`data/universe/v27_universe_pit.csv` screened the union of today's 907 names
and the delisted roster. That pool was itself drawn at ADV >= $250,000, so
lowering the floor to $50,000 cannot be done by re-screening it: the names the
old floor excluded were never in it. The candidate pool has to be rebuilt from
the full US symbol list.

WHAT CHANGES, AND WHY, per data/registry/v29_specifications.json:

  ADV floor   $250,000 -> $50,000. With $10,000 across 20 positions a position
              is $500; at 5% participation that needs $10,000 of daily volume.
              The old floor was 25x what the capital requires and excluded
              exactly the names large capital cannot reach.
  window      3 years -> 10. Three was a Finnhub artefact; nothing about the
              price or filing record required it.

WHAT DOES NOT CHANGE: the ADV ceiling ($5M), the price floor ($2), the history
minimum (200 bars), and the evaluation itself, which is delegated to
`qt.prices.screen_as_of` exactly as step 2 was. Two screens that drift apart
are worse than one screen that is wrong.

MEMORY. Ten years of daily bars for several thousand names does not fit
comfortably in a runner, so prices are STREAMED: download a chunk, screen it
at every month-end, keep only the eligible rows, discard the bars. Peak memory
is one chunk rather than the whole market.

⚠️ SURVIVORSHIP IS CORRECTED ONLY FROM 2023. The delisted roster covers
2023-2026 while this window opens in 2015, so the first eight years carry the
same upward bias every v27 read carried. Declared in the registry as the
largest known weakness of the specification, and repeated here because this is
the file that would otherwise hide it.

Writes data/universe/v29_universe_pit.csv. No return computed, no K.
"""
from __future__ import annotations

import collections
import os
import sys
import time

import pandas as pd

from qt import prices as qtp
from build_universe_pit import is_live, month_ends

OUT_CSV = os.environ.get("QT_V29_OUT", "data/universe/v29_universe_pit.csv")
ROSTER = os.environ.get("QT_ROSTER_FILE", "data/universe/delisted_roster.csv")
START = os.environ.get("QT_V29_START", "2015-09-01")
END = os.environ.get("QT_V29_END", "2025-09-01")
LIMIT = int(os.environ.get("QT_V29_LIMIT", "0"))
CHUNK = int(os.environ.get("QT_V29_CHUNK", "100"))
if LIMIT:
    OUT_CSV = OUT_CSV.replace(".csv", f"_SMOKE{LIMIT}.csv")

# The declared specification tuple. Only the floor moves.
ADV_MAX = float(os.environ.get("QT_V29_ADV_MAX", "5000000"))
ADV_MIN = float(os.environ.get("QT_V29_ADV_MIN", "50000"))
PRICE_MIN = float(os.environ.get("QT_V29_PRICE_MIN", "2.00"))
MIN_HISTORY = int(os.environ.get("QT_V29_MIN_HISTORY", "200"))
ADV_LOOKBACK = int(os.environ.get("QT_V29_ADV_LOOKBACK", "60"))

FINNHUB_SYMBOLS = "https://finnhub.io/api/v1/stock/symbol"
# Unchanged from build_universe.py. An unknown type is EXCLUDED rather than
# waved through: the 2026-09-01 funnel found 767 untyped rows that would
# otherwise have entered as "operating companies".
COMMON_TYPES = {"Common Stock", "COMMON STOCK", "EQS", "Equity"}
US_PRIMARY_MICS = {"XNAS", "XNYS", "ARCX", "BATS", "XASE", "IEXG", "EDGX"}
PRICE_START = "2014-06-01"          # 200 bars + 60 lookback before START


def fetch_symbols(key: str) -> list:
    import requests
    r = requests.get(FINNHUB_SYMBOLS, params={"exchange": "US", "token": key}, timeout=60)
    r.raise_for_status()
    rows = r.json() or []
    mics, types, out = collections.Counter(), collections.Counter(), []
    for d in rows:
        typ = str(d.get("type") or "").strip()
        mic = str(d.get("mic") or "").strip().upper()
        sym = str(d.get("symbol") or "").strip().upper()
        types[typ or "(blank)"] += 1
        if typ not in COMMON_TYPES:
            continue
        mics[mic] += 1
        if mic not in US_PRIMARY_MICS:
            continue
        if not sym or not sym.replace(".", "").replace("-", "").isalnum() or len(sym) > 6:
            continue
        out.append(sym)
    print(f"  raw rows {len(rows):,}; common-stock types kept "
          f"{sum(v for k, v in types.items() if k in COMMON_TYPES):,}")
    print(f"  MIC histogram (common stock only): {dict(mics.most_common(10))}")
    return sorted(set(out))


def screen_chunk(prices: dict, grid) -> tuple:
    rows, funnel = [], collections.Counter()
    for tk, (close, volume) in prices.items():
        for d in grid:
            e = qtp.screen_as_of(close, volume, d, adv_max=ADV_MAX, adv_min=ADV_MIN,
                                 price_min=PRICE_MIN, min_history=MIN_HISTORY,
                                 lookback=ADV_LOOKBACK)
            live = is_live(close, d)
            funnel[e.reason if live else "stale_no_longer_trading"] += 1
            if e.eligible and live:
                rows.append({"date": d.strftime("%Y-%m-%d"), "ticker": tk,
                             "price": round(float(e.price), 4),
                             "adv": int(e.adv), "n_bars": int(e.n_bars)})
    return rows, funnel


def fetch_chunk(chunk: list) -> dict:
    import yfinance as yf
    out = {}
    raw = yf.download(chunk, start=PRICE_START, end=None, auto_adjust=True,
                      progress=False, threads=True, group_by="column")
    if raw is None or len(raw) == 0:
        return out
    multi = isinstance(raw.columns, pd.MultiIndex)
    for tk in chunk:
        try:
            c = raw["Close"][tk] if multi else raw["Close"]
            v = raw["Volume"][tk] if multi else raw["Volume"]
        except (KeyError, TypeError):
            continue
        c = pd.to_numeric(c, errors="coerce").dropna()
        if len(c) < MIN_HISTORY:
            continue
        v = pd.to_numeric(v, errors="coerce").reindex(c.index).fillna(0)
        idx = pd.DatetimeIndex(c.index).tz_localize(None).normalize()
        c.index, v.index = idx, idx
        out[tk] = (c, v)
    return out


def main() -> None:
    key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not key:
        print("[v29-universe] FINNHUB_API_KEY not set"); sys.exit(2)
    print(f"[v29-universe] screen ADV {ADV_MIN:,.0f}-{ADV_MAX:,.0f}, price >= {PRICE_MIN}, "
          f">= {MIN_HISTORY} bars, lookback {ADV_LOOKBACK}")
    print(f"[v29-universe] window {START}..{END}\n")

    print("=" * 74); print("1. CANDIDATE POOL"); print("=" * 74)
    symbols = fetch_symbols(key)
    print(f"  {len(symbols):,} US primary-exchange common stocks")
    added = 0
    if os.path.exists(ROSTER):
        roster = pd.read_csv(ROSTER)
        usable = roster[roster["status"].isin(["otc-continuation", "still-listed"])]
        extra = sorted({str(t).upper() for t in usable["ticker"]} - set(symbols))
        symbols = sorted(set(symbols) | set(extra))
        added = len(extra)
        print(f"  + {added} delisted names from the roster (2023-2026 only — the")
        print(f"    first eight years of this window are NOT survivorship-corrected)")
    if LIMIT:
        symbols = symbols[:LIMIT]
    grid = month_ends(START, END)
    print(f"  {len(symbols):,} candidates; {len(grid)} month-ends "
          f"{grid[0].date()}..{grid[-1].date()}\n")

    print("=" * 74); print("2. STREAMING SCREEN"); print("=" * 74)
    t0 = time.time()
    all_rows, funnel, priced = [], collections.Counter(), 0
    for k in range(0, len(symbols), CHUNK):
        chunk = symbols[k:k + CHUNK]
        try:
            prices = fetch_chunk(chunk)
        except Exception as exc:
            print(f"  chunk {k//CHUNK+1}: download failed ({type(exc).__name__}), skipping")
            continue
        priced += len(prices)
        rows, f = screen_chunk(prices, grid)
        all_rows.extend(rows)
        funnel.update(f)
        del prices
        if (k // CHUNK + 1) % 5 == 0:
            print(f"  ...{min(k+CHUNK, len(symbols)):,}/{len(symbols):,} symbols, "
                  f"{priced:,} priced, {len(all_rows):,} eligible rows, "
                  f"{time.time()-t0:.0f}s")
    print(f"\n  {priced:,}/{len(symbols):,} had usable price history in {time.time()-t0:.0f}s")
    if not all_rows:
        print("[v29-universe] nothing eligible — refusing to write"); sys.exit(1)

    df = pd.DataFrame(all_rows).sort_values(["date", "ticker"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print("\n" + "=" * 74); print("SCREEN FUNNEL (name-months)"); print("=" * 74)
    for reason, n in funnel.most_common():
        print(f"  {reason:<26} {n:>10,}")

    per = df.groupby("date").size()
    print("\n" + "=" * 74); print("MEMBERSHIP OVER TIME (every 12th month)"); print("=" * 74)
    for d, n in list(per.items())[::12]:
        print(f"  {d}  {n:>5} eligible  {'#' * int(n / 60)}")
    print(f"\n  median {int(per.median())} names per month; min {int(per.min())}, "
          f"max {int(per.max())}")
    print(f"  {df['ticker'].nunique():,} distinct names across the window")
    print(f"\n  vs the v27 static universe of 907 screened at a $250k floor, and vs")
    print(f"  the v27 point-in-time panel which had 413 eligible at 2023-09-30.")
    print(f"\n[v29-universe] wrote {OUT_CSV} ({len(df):,} rows)")
    print("[v29-universe] no return computed, no fundamentals fetched, K untouched.")


if __name__ == "__main__":
    main()
