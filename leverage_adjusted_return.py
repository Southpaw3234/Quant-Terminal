"""
leverage_adjusted_return.py — measurement-only: what the +18.7% would look like
without the accidental leverage (duplicate-retrain incident, ledgers 7/6-7/7).

Reconstructs the account's TRUE daily gross exposure from Alpaca FILL
activities (broker ground truth — the dashboard trades ledger records
submission-poll status, not fills) and daily closing prices, then de-levers
each day's equity return by that day's leverage:

    lambda_t  = max(1, gross_{t-1} / equity_{t-1})
    adj_ret_t = raw_ret_t / lambda_t

i.e. days traded at 3x gross get credited 1/3 of the P&L they printed, days
at or under 1x are untouched. Compounding adj_ret gives the "as if the model
had been hard-capped at 1.0x gross" return.

Read-only: GETs only (activities / portfolio history / positions), submits
and cancels NOTHING. Caveats: close-price marks, ignores dividends/borrow
costs, assumes no splits in the window. Sanity check printed at the end:
reconstructed final gross vs live /v2/positions gross.

Run via the "Performance Analysis (read-only)" workflow_dispatch.
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

import pandas as pd
import yfinance as yf


def _clean(v):
    return v.strip().strip("\ufeff").strip()


BASE = _clean(os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")).rstrip("/")
KEY = _clean(os.environ["ALPACA_API_KEY"])
SEC = _clean(os.environ["ALPACA_SECRET_KEY"])
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "DOGE"}


def api(path):
    req = urllib.request.Request(f"{BASE}{path}")
    req.add_header("APCA-API-KEY-ID", KEY)
    req.add_header("APCA-API-SECRET-KEY", SEC)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def yf_symbol(sym):
    # Alpaca crypto fills use e.g. BTCUSD / BTC/USD; yfinance wants BTC-USD.
    s = sym.replace("/", "")
    if s.endswith("USD") and s[:-3] in CRYPTO:
        return s[:-3] + "-USD"
    return sym


def main():
    # ── 1. every fill since inception (paginated, ascending) ──────────────
    fills, token = [], None
    while True:
        q = ("/v2/account/activities/FILL?after=2026-05-01T00:00:00Z"
             "&direction=asc&page_size=100")
        if token:
            q += f"&page_token={token}"
        batch = api(q)
        if not batch:
            break
        fills += batch
        token = batch[-1]["id"]
        if len(batch) < 100:
            break
    print(f"FILL activities since 2026-05-01: {len(fills)}")
    if not fills:
        print("No fills — nothing to analyze.")
        return 0

    # ── 2. daily share counts per symbol (signed) ──────────────────────────
    daily_delta = defaultdict(lambda: defaultdict(float))
    for f in fills:
        d = f["transaction_time"][:10]
        qty = float(f["qty"])
        side = f["side"]
        signed = qty if side == "buy" else -qty
        daily_delta[d][f["symbol"]] += signed

    first_day = date.fromisoformat(min(daily_delta))
    today = date.today()
    all_days = [first_day + timedelta(n) for n in range((today - first_day).days + 1)]
    symbols = sorted({s for day in daily_delta.values() for s in day})
    print(f"Window {first_day} → {today}, {len(symbols)} symbols traded")

    # ── 3. daily closes for every traded symbol ────────────────────────────
    ymap = {s: yf_symbol(s) for s in symbols}
    px = yf.download(sorted(set(ymap.values())),
                     start=str(first_day - timedelta(5)),
                     end=str(today + timedelta(1)),
                     progress=False, auto_adjust=False)["Close"]
    if isinstance(px, pd.Series):
        px = px.to_frame(name=list(ymap.values())[0])
    px = px.ffill()

    def close_on(sym, d):
        col = ymap[sym]
        if col not in px.columns:
            return None
        s = px[col].loc[:str(d)]
        return float(s.iloc[-1]) if len(s) and pd.notna(s.iloc[-1]) else None

    # ── 4. reconstruct end-of-day positions → gross ────────────────────────
    shares = defaultdict(float)
    gross_by_day = {}
    for d in all_days:
        for sym, q in daily_delta.get(str(d), {}).items():
            shares[sym] += q
        g = 0.0
        for sym, q in shares.items():
            if abs(q) < 1e-9:
                continue
            p = close_on(sym, d)
            if p is not None:
                g += abs(q) * p
        gross_by_day[str(d)] = g

    # ── 5. equity curve from portfolio history ─────────────────────────────
    ph = api("/v2/account/portfolio/history?period=3M&timeframe=1D")
    eq = {}
    for ts, e in zip(ph["timestamp"], ph["equity"]):
        if e:
            eq[str(date.fromtimestamp(ts))] = float(e)
    days = sorted(k for k in eq if k >= str(first_day))

    # ── 6. de-lever daily returns ──────────────────────────────────────────
    print(f"\n  {'date':10s} {'equity':>10s} {'gross':>10s} {'lev':>5s} "
          f"{'raw%':>7s} {'adj%':>7s}")
    raw_cum, adj_cum, lev_sum, lev_max = 1.0, 1.0, 0.0, 0.0
    prev = None
    for d in days:
        if prev is not None:
            raw = eq[d] / eq[prev] - 1
            lam = max(1.0, gross_by_day.get(prev, 0.0) / eq[prev])
            adj = raw / lam
            raw_cum *= 1 + raw
            adj_cum *= 1 + adj
            lev_sum += lam
            lev_max = max(lev_max, lam)
            print(f"  {d:10s} {eq[d]:10,.0f} {gross_by_day.get(d, 0):10,.0f} "
                  f"{lam:5.2f} {raw * 100:+7.2f} {adj * 100:+7.2f}")
        prev = d
    n = len(days) - 1

    # ── 7. sanity check vs live positions ─────────────────────────────────
    live_gross = sum(abs(float(p["market_value"])) for p in api("/v2/positions"))
    recon_gross = gross_by_day.get(days[-1], 0.0)
    print(f"\nSanity: reconstructed gross today ${recon_gross:,.0f} "
          f"vs live Alpaca gross ${live_gross:,.0f} "
          f"({(recon_gross / live_gross - 1) * 100:+.1f}% drift)"
          if live_gross else "\nSanity: no live positions to compare")

    print(f"\n══ SUMMARY over {n} trading days ══")
    print(f"raw return       : {(raw_cum - 1) * 100:+.2f}%")
    print(f"ADJUSTED return  : {(adj_cum - 1) * 100:+.2f}%   "
          f"(each day's return / that day's leverage, capped at 1x)")
    print(f"avg leverage     : {lev_sum / n:.2f}x    max leverage: {lev_max:.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
