#!/usr/bin/env python3
"""Point-in-time universe — step 2 of the survivorship fix.

`data/universe/v27_universe.csv` answers ONE question: which names pass the
screen TODAY. Every v27 study then applied that answer backwards across three
years, so a company that was liquid in 2024 and delisted in 2025 was never a
candidate for a 2024 event. That is the survivorship bias, stated exactly.

This answers a different question, once per month-end across the window:

    which names passed the screen AS OF THAT DATE, using only data that
    existed on that date

Step 1 (build_delisted_roster.py) supplied the names missing from today's
list. This screens the union of those and today's universe. Step 3 re-runs
event discovery over the result.

THE SCREEN IS THE DECLARED ONE, NOT A NEW ONE
----------------------------------------------
(ADV_MAX 5M, ADV_MIN 250k, PRICE_MIN $2.00, MIN_HISTORY 200 bars, ADV
lookback 60) is the specification tuple from build_universe.py, and it is
reused here unchanged. Changing a screen threshold while fixing survivorship
would confound the two, and the whole point of this exercise is to isolate
the effect of one of them.

The evaluation is delegated to `qt.prices.screen_as_of`, which was written for
exactly this and takes bars STRICTLY BEFORE the as-of date. Reimplementing it
here would mean two screens that could drift apart.

MONTH-END GRID, AND WHY THE LAG IS THE CONSERVATIVE DIRECTION
--------------------------------------------------------------
Membership is evaluated at month-ends. An event on any day then inherits the
membership decided at the PREVIOUS month-end, so eligibility is always known
strictly before the event -- up to a month stale, never forward-looking.
Staleness costs a little power; look-ahead would manufacture the result. The
lag is the safe error.

WHO IS IN THE CANDIDATE POOL, AND WHO IS DELIBERATELY LEFT OUT
---------------------------------------------------------------
  today's universe            all 907
  roster otc-continuation     237 — delisted but priced, the whole point
  roster still-listed          42 — never actually delisted; the screen, not
                                    the roster, decides whether they belong
  roster identity-mismatch      7 — EXCLUDED. A reused ticker would splice two
                                    companies into one series and manufacture
                                    a return belonging to neither (CBRG became
                                    a leveraged ETF). Excluded conservatively
                                    and counted, never silently resolved.
  roster no-data              148 — no prices anywhere, so unscreenable. They
                                    are the RESIDUAL HOLE and are reported as
                                    a number rather than dropped in silence.

Writes data/universe/v27_universe_pit.csv. Computes no return, spends no K.
"""
from __future__ import annotations

import collections
import os
import sys
import time

import numpy as np
import pandas as pd

from qt import prices as qtp

UNIVERSE = os.environ.get("QT_U_FILE", "data/universe/v27_universe.csv")
ROSTER = os.environ.get("QT_ROSTER_FILE", "data/universe/delisted_roster.csv")
OUT_CSV = os.environ.get("QT_PIT_OUT", "data/universe/v27_universe_pit.csv")
WINDOW_START = os.environ.get("QT_PIT_START", "2023-09-01")
WINDOW_END = os.environ.get("QT_PIT_END", "2026-09-05")
LIMIT = int(os.environ.get("QT_PIT_LIMIT", "0"))
if LIMIT:
    OUT_CSV = OUT_CSV.replace(".csv", f"_SMOKE{LIMIT}.csv")

# The declared specification tuple, unchanged from build_universe.py.
ADV_MAX = float(os.environ.get("QT_U_ADV_MAX", "5000000"))
ADV_MIN = float(os.environ.get("QT_U_ADV_MIN", "250000"))
PRICE_MIN = float(os.environ.get("QT_U_PRICE_MIN", "2.00"))
MIN_HISTORY = int(os.environ.get("QT_U_MIN_HISTORY", "200"))
ADV_LOOKBACK = int(os.environ.get("QT_U_ADV_LOOKBACK", "60"))

USABLE_STATUS = ("otc-continuation", "still-listed")


# ═══════════════════════════════════════════════════ pure logic (no network)

def month_ends(start: str, end: str) -> pd.DatetimeIndex:
    """Calendar month-ends inside the window, tz-naive."""
    return pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="ME")


def candidate_pool(universe_df: pd.DataFrame, roster_df: pd.DataFrame) -> tuple:
    """-> (pool, excluded_counts). Pool is [(ticker, cik_or_empty, source)]."""
    live = sorted(universe_df["ticker"].astype(str).str.upper().unique())
    pool = {t: ("", "current") for t in live}
    excluded = collections.Counter()
    for _, r in roster_df.iterrows():
        tk = str(r.get("ticker", "")).upper().strip()
        st = str(r.get("status", ""))
        if not tk:
            continue
        if st not in USABLE_STATUS:
            excluded[st] += 1
            continue
        if tk not in pool:
            pool[tk] = (str(r.get("cik", "")), "roster")
        excluded["added_from_roster"] += 0
    return sorted((t, c, s) for t, (c, s) in pool.items()), excluded


def evaluate(pool, prices: dict, grid) -> tuple:
    """Screen every candidate at every month-end. -> (rows, funnel)."""
    rows, funnel = [], collections.Counter()
    for tk, cik, source in pool:
        pair = prices.get(tk)
        if pair is None:
            funnel["no_series"] += 1
            continue
        close, volume = pair
        for d in grid:
            e = qtp.screen_as_of(close, volume, d, adv_max=ADV_MAX,
                                 adv_min=ADV_MIN, price_min=PRICE_MIN,
                                 min_history=MIN_HISTORY, lookback=ADV_LOOKBACK)
            funnel[e.reason] += 1
            if e.eligible:
                rows.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "ticker": tk,
                    "cik": cik,
                    "source": source,
                    "price": round(float(e.price), 4),
                    "adv": int(e.adv) if np.isfinite(e.adv) else 0,
                    "n_bars": int(e.n_bars),
                })
    return rows, funnel


# ═══════════════════════════════════════════════════════════ network shell

def fetch_prices(tickers: list) -> dict:
    """ticker -> (close, volume), back-adjusted, tz-naive daily."""
    import yfinance as yf
    out = {}
    for k in range(0, len(tickers), 100):
        chunk = tickers[k:k + 100]
        raw = yf.download(chunk, start="2022-06-01", end=None, auto_adjust=True,
                          progress=False, threads=True, group_by="column")
        if raw is None or len(raw) == 0:
            continue
        multi = isinstance(raw.columns, pd.MultiIndex)
        for tk in chunk:
            try:
                c = raw["Close"][tk] if multi else raw["Close"]
                v = raw["Volume"][tk] if multi else raw["Volume"]
            except (KeyError, TypeError):
                continue
            c = pd.to_numeric(c, errors="coerce").dropna()
            v = pd.to_numeric(v, errors="coerce").reindex(c.index).fillna(0)
            if len(c) == 0:
                continue
            idx = pd.DatetimeIndex(c.index).tz_localize(None).normalize()
            c.index, v.index = idx, idx
            out[tk] = (c, v)
        print(f"  prices {min(k+100, len(tickers))}/{len(tickers)} — {len(out)} with history")
    return out


def main() -> None:
    for p in (UNIVERSE, ROSTER):
        if not os.path.exists(p):
            print(f"[pit] missing {p}"); sys.exit(1)
    uni = pd.read_csv(UNIVERSE)
    roster = pd.read_csv(ROSTER)
    pool, excluded = candidate_pool(uni, roster)
    if LIMIT:
        pool = pool[:LIMIT]
    grid = month_ends(WINDOW_START, WINDOW_END)

    print(f"[pit] screen ADV {ADV_MIN:,.0f}-{ADV_MAX:,.0f}, price >= {PRICE_MIN}, "
          f">= {MIN_HISTORY} bars, lookback {ADV_LOOKBACK}")
    print(f"[pit] candidates {len(pool)} = {sum(1 for _,_,s in pool if s=='current')} current "
          f"+ {sum(1 for _,_,s in pool if s=='roster')} from the roster")
    print(f"[pit] roster rows NOT pooled: "
          f"{ {k: v for k, v in excluded.items() if v} }")
    print(f"[pit] {len(grid)} month-ends {grid[0].date()} .. {grid[-1].date()}\n")

    t0 = time.time()
    prices = fetch_prices([t for t, _, _ in pool])
    print(f"[pit] {len(prices)}/{len(pool)} priced in {time.time()-t0:.0f}s\n")

    rows, funnel = evaluate(pool, prices, grid)
    df = pd.DataFrame(rows)
    if df.empty:
        print("[pit] nothing eligible anywhere — refusing to write"); sys.exit(1)
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print("=" * 74); print("SCREEN FUNNEL (name-months)"); print("=" * 74)
    for reason, n in funnel.most_common():
        print(f"  {reason:<18} {n:>8,}")

    print("\n" + "=" * 74); print("MEMBERSHIP OVER TIME"); print("=" * 74)
    per = df.groupby("date").agg(eligible=("ticker", "size"),
                                 from_roster=("source", lambda s: int((s == "roster").sum())))
    for d, r in per.iterrows():
        bar = "#" * int(r["eligible"] / 25)
        print(f"  {d}  {int(r['eligible']):>4} eligible  "
              f"{int(r['from_roster']):>3} absent from today's list  {bar}")

    total_nm = len(df)
    dead_nm = int((df["source"] == "roster").sum())
    print("\n" + "=" * 74)
    print(f"  THE NUMBER THIS WAS BUILT FOR: {dead_nm:,} of {total_nm:,} eligible name-months "
          f"({100*dead_nm/max(1,total_nm):.1f}%)")
    print(f"  belong to companies ABSENT from today's universe file. Every v27 study so far")
    print(f"  drew its events from the other {100-100*dead_nm/max(1,total_nm):.1f}% and could not have")
    print(f"  sampled these at all. That is the survivorship bias, measured rather than asserted.")
    print(f"\n  ⚠️ THIS IS A LOWER BOUND. {int((roster['status'] == 'no-data').sum())} roster names have no "
          f"price data anywhere and cannot be")
    print(f"  screened, so they are absent here too. They need a delisting-return assumption,")
    print(f"  and that is a PRE-REGISTRATION choice, not a default this program may pick.")
    print(f"\n[pit] wrote {OUT_CSV} ({len(df):,} rows)")
    print("[pit] no return computed, no event discovered, K untouched.")


if __name__ == "__main__":
    main()
