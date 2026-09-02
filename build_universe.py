#!/usr/bin/env python3
"""Inverted universe screen — v27 component A4.

Builds the universe v27 measures on: names that large funds STRUCTURALLY
CANNOT hold. Small, thinly traded, uncovered. The retired v25 book was 279
US mega-caps — the single most competed cell in global equities, and now
measured as such (walk-forward AUC ~0.50, WRC p ~0.52).

This is not a refinement of that universe. It is its complement.

WHY THIS BLOCKS A3
------------------
`extract_form4.py`'s live probe on 2026-09-01 surveyed 8 mega-caps over 365
days: **2,661 insider transactions, 26 open-market purchases, 25 of them one
ticker.** Six of eight names had zero. Mega-cap insiders are compensated in
stock and sell it; they do not buy it. The cluster-buy specification cannot
reach E1's N>=40 on that universe at any parameter setting.

So A4 is not "nice to have next". Without it A3 emits nothing.

THE FILTER IS INVERTED, NOT LOOSENED
------------------------------------
`trading_model_v25.1.ipynb` Cell 2 drops any name whose average daily dollar
volume is BELOW $50M:

    _ADV_MIN_USD = 50_000_000
    ... if _compute_adv(raw_data[tk]) < _ADV_MIN_USD]

Here that becomes a CEILING. `ADV_MAX` (default $5M) keeps only what that
filter threw away. There is also an `ADV_MIN` floor, because a name too thin
to trade is not an opportunity — E4 caps position size at 5% of ADV, so the
floor and the eventual allocation are the same constraint seen twice.

⚠️  TWO BIASES, POINTING OPPOSITE WAYS, THAT DO NOT CANCEL
----------------------------------------------------------
1. **SURVIVORSHIP — biases results UPWARD, and this is the dangerous one.**
   The symbol list is TODAY's listed universe. Companies that delisted,
   went to zero, or were acquired are simply absent. Insiders buy their own
   stock on the way down too, and those events are invisible here. Every
   insider-buying study run on a survivor-only universe overstates the
   effect, and small illiquid names are exactly where delisting is common —
   so the bias is largest precisely where this project is looking.

   It is **not fixed** by anything in this file. Finnhub's free tier exposes
   currently-listed symbols only. Mitigating it needs a point-in-time
   constituent source (paid), and until there is one, an E1 pass on this
   universe carries an unquantified upward bias that must be stated
   alongside the number rather than discovered by a reader later.

2. **NEXT-SESSION ENTRY — biases results DOWNWARD** by up to one session
   (see `extract_form4.py`: Finnhub gives no intraday filing time).

These are different mechanisms of unknown relative magnitude. Do not net
them off in your head and call the result unbiased.

POINT-IN-TIME BY CONSTRUCTION
-----------------------------
`compute_adv(prices, asof, lookback)` uses only bars STRICTLY BEFORE `asof`.
Screening an event with liquidity measured over a window that includes the
event — or worse, over all history to today — decides membership using the
future. That is the same look-ahead class `event_study.py` closes at the
entry bar, and it would enter here through the universe instead.

WHAT COUNTS AGAINST K
---------------------
`docs/V27_PREREGISTRATION.md`: the universe is **part of the specification**.
The (ADV_MAX, ADV_MIN, PRICE_MIN, MIN_HISTORY_DAYS) tuple plus the event
triple in `extract_form4.py` together consume ONE of the K=5. Re-screening
at a different ADV ceiling because the first one gave few events is a second
specification, whether or not it is called one.

Checking how MANY names or events a screen yields is free — that is data
availability. Checking how they PERFORM is what costs K.

ENV
---
  FINNHUB_API_KEY        required for the symbol list
  QT_U_OUT               default data/universe/v27_universe.csv
  QT_U_ADV_MAX           default 5_000_000     ] the SPECIFICATION tuple --
  QT_U_ADV_MIN           default 250_000       ] declared, not tuned
  QT_U_PRICE_MIN         default 2.00          ]
  QT_U_MIN_HISTORY       default 200           ]
  QT_U_ADV_LOOKBACK      default 60  (trading days)
  QT_U_MAX_SYMBOLS       hard cap for cost control (0 = no cap)
  QT_U_SAMPLE_N          default 1500   ] the frozen random sample --
  QT_U_SAMPLE_SEED       default 20260901 ] part of the specification
  QT_U_SAMPLE_OUT        default data/universe/v27_sample.csv
  QT_U_SYMBOLS_ONLY      "1" -> fetch + report the symbol funnel, no prices
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FINNHUB_SYMBOLS = "https://finnhub.io/api/v1/stock/symbol"

OUT_CSV = Path(os.environ.get("QT_U_OUT", "data/universe/v27_universe.csv"))

# ---- THE SPECIFICATION TUPLE. Declared, not tuned. See module docstring.
ADV_MAX = float(os.environ.get("QT_U_ADV_MAX", "5000000"))
ADV_MIN = float(os.environ.get("QT_U_ADV_MIN", "250000"))
PRICE_MIN = float(os.environ.get("QT_U_PRICE_MIN", "2.00"))
MIN_HISTORY_DAYS = int(os.environ.get("QT_U_MIN_HISTORY", "200"))
ADV_LOOKBACK = int(os.environ.get("QT_U_ADV_LOOKBACK", "60"))

MAX_SYMBOLS = int(os.environ.get("QT_U_MAX_SYMBOLS", "0"))

# ---- SAMPLING. Decided 2026-09-01: price a fixed random sample rather than
# all ~18,400 names. Pricing everything is ~92 chunked yfinance downloads,
# slow and flaky, and it is not needed -- a RULE that draws a random sample
# is as defensible as a rule that takes everything, and far cheaper.
#
# 🔑 The sample is FROZEN TO DISK on first draw and reloaded thereafter.
# This is the entire point. A sample redrawn each run is not a sample, it is
# an unlimited supply of universes: run it enough times and one of them
# produces a flattering E1, with nothing in the record showing how many were
# discarded. Freezing makes the draw a one-time, dated event -- the same
# discipline `_freeze_first_write` applies to written evidence rows, applied
# to universe membership instead.
SAMPLE_N = int(os.environ.get("QT_U_SAMPLE_N", "1500"))
SAMPLE_SEED = int(os.environ.get("QT_U_SAMPLE_SEED", "20260901"))
SAMPLE_CSV = Path(os.environ.get("QT_U_SAMPLE_OUT",
                                 "data/universe/v27_sample.csv"))

# Finnhub `type` values that are actual operating companies. Everything else
# (ETP, closed-end funds, warrants, units, rights, preferreds) is excluded:
# an ETF has no insiders, and a warrant's "insider buying" is not a signal
# about a business.
COMMON_TYPES = {"Common Stock", "COMMON STOCK", "EQS", "Equity"}

# Primary US exchanges, by MIC. Everything else — above all the OTC venues
# (OTCM, PINX, OOTC) — is excluded.
#
# 🔑 THIS IS NOT TIDINESS. The first live screen returned RHHVF = ROCHE
# HOLDING AG, a ~$250B pharmaceutical company, as an "illiquid small-cap".
# It qualified because its US OTC line is thinly traded. Along with it came
# ATUUF, EXETF, CURLF, KRKNF, NOPMF, CDDRF — 7 of 69 names, all foreign
# ordinaries on OTC.
#
# Two independent reasons this destroys the screen:
#
#  1. ADV ON AN OTC FOREIGN ORDINARY MEASURES THE THINNESS OF ITS US
#     LISTING, NOT THE SIZE OR OBSCURITY OF THE COMPANY. The screen's whole
#     premise is "too small for large funds to hold". Roche is the single
#     most-covered pharma name on earth.
#  2. Foreign private issuers are EXEMPT from Section 16 (Rule 3a12-3(b)),
#     so they file no Form 4s at all. These names cannot produce a single
#     event no matter how long the study runs — they are pure dead weight
#     in a universe whose size is the binding constraint.
#
# The MIC histogram is printed on every symbols-only run so this allowlist
# can be checked against reality rather than trusted.
US_PRIMARY_MICS = {"XNAS", "XNYS", "ARCX", "BATS", "XASE", "IEXG", "EDGX",
                   "XBOS", "XPHL", "XCIS"}


# ------------------------------------------------------------------ pure

def filter_symbols(raw: list) -> list:
    """Finnhub symbol rows -> plausible operating companies.

    Excludes anything with a dot or caret in the symbol: those are class
    shares (BRK.B), warrants, units and indices in most feeds, and they
    either duplicate an existing line or are not the instrument we mean.
    """
    out = []
    seen = set()
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        sym = str(d.get("symbol") or "").strip().upper()
        typ = str(d.get("type") or "").strip()
        if not sym or sym in seen:
            continue
        if any(c in sym for c in ".^-="):
            continue
        if not sym.isalpha() or len(sym) > 5:
            continue
        # Strict: an UNKNOWN type is excluded, not waved through. The first
        # version read `if typ and typ not in COMMON_TYPES`, which let every
        # blank-type row pass -- the 2026-09-01 live funnel returned 19,088
        # "operating companies" against only 18,433 rows actually typed
        # Common Stock, and the gap was 767 untyped rows nobody had decided
        # to admit. Admitting instruments by accident is how a universe
        # acquires warrants and units it was never meant to contain.
        if typ not in COMMON_TYPES:
            continue
        # Primary US exchanges only. See US_PRIMARY_MICS for why this is
        # load-bearing rather than cosmetic.
        mic = str(d.get("mic") or "").strip().upper()
        if mic not in US_PRIMARY_MICS:
            continue
        # Belt and braces: the 5-letter -F suffix is the OTC foreign-ordinary
        # convention (RHHVF, EXETF, CURLF). If one ever reaches here with a
        # primary-exchange MIC, drop it anyway — a foreign private issuer
        # files no Form 4 and cannot produce an event.
        if len(sym) == 5 and sym.endswith("F"):
            continue
        seen.add(sym)
        out.append({"ticker": sym,
                    "description": str(d.get("description") or "").strip(),
                    "type": typ, "mic": mic})
    return sorted(out, key=lambda r: r["ticker"])


def sample_symbols(syms: list, n: int = SAMPLE_N,
                   seed: int = SAMPLE_SEED) -> list:
    """Deterministic random sample of the symbol list.

    Sorted first so the draw does not depend on the order Finnhub happened
    to return rows in — same seed plus same population must give the same
    sample on any machine, on any day.

    Returns everything if the population is already at or below n, so the
    sampling step is a no-op on a small universe rather than an error.
    """
    pool = sorted(syms, key=lambda r: r["ticker"])
    if n <= 0 or len(pool) <= n:
        return pool
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(pool), size=n, replace=False)
    return [pool[i] for i in sorted(idx)]


def load_or_draw_sample(syms: list, path: Path = SAMPLE_CSV,
                        n: int = SAMPLE_N, seed: int = SAMPLE_SEED) -> list:
    """Reload the frozen sample if it exists; otherwise draw and freeze it.

    A redrawn sample is a new universe, and a new universe is a new
    SPECIFICATION under docs/V27_PREREGISTRATION.md. Reloading is therefore
    the correct behaviour even when the symbol population has changed since
    the draw — new listings do not retroactively join the study, and names
    that have since delisted stay in it, which is the only handling that
    does not quietly re-bias the universe over time.

    To draw a genuinely new sample, delete the file deliberately and record
    why. That is meant to be an explicit act, not a side effect of a rerun.
    """
    if path.exists():
        try:
            old = pd.read_csv(path)
        except Exception as exc:
            print(f"[universe] frozen sample at {path} unreadable ({exc})")
            old = None
        if old is not None and "ticker" in old.columns and len(old):
            meta = ""
            if "drawn_at" in old.columns and "seed" in old.columns:
                meta = (f" (drawn {old['drawn_at'].iloc[0]}, "
                        f"seed {old['seed'].iloc[0]}, "
                        f"from a population of "
                        f"{old.get('population', pd.Series(['?'])).iloc[0]})")
            print(f"[universe] REUSING frozen sample: {len(old)} name(s)"
                  f"{meta}")
            print("[universe] the sample is NOT redrawn — a new draw is a new "
                  "specification. Delete the file deliberately to redraw.")
            by_tk = {r["ticker"]: r for r in syms}
            return [by_tk.get(tk, {"ticker": tk, "description": "",
                                   "type": ""})
                    for tk in old["ticker"].astype(str).str.upper()]

    drawn = sample_symbols(syms, n, seed)
    stamp = pd.Timestamp.utcnow().date().isoformat()
    out = pd.DataFrame([{"ticker": r["ticker"],
                         "description": r.get("description", ""),
                         "seed": seed, "n_requested": n,
                         "population": len(syms),
                         "drawn_at": stamp} for r in drawn])
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    print(f"[universe] DREW a new sample: {len(drawn)} of {len(syms):,} "
          f"(seed {seed}) and FROZE it to {path}")
    print("[universe] this draw is now part of the specification. Reruns "
          "reload it; they do not redraw.")
    return drawn


def compute_adv(close: pd.Series, volume: pd.Series, asof=None,
                lookback: int = ADV_LOOKBACK):
    """Median daily dollar volume over `lookback` bars STRICTLY BEFORE asof.

    Median, not mean: a single halt-and-resume day or an index-rebalance
    print can multiply a thin name's mean volume several times over, and a
    screen built on the mean would admit names on the strength of one day
    nobody can trade again.

    `asof=None` means "use all available history", which is only correct for
    building a forward-looking watchlist. For screening a historical event,
    always pass the event date -- otherwise membership is decided using bars
    that had not happened yet.
    """
    if close is None or volume is None or len(close) == 0:
        return None, 0
    df = pd.DataFrame({"c": close, "v": volume}).dropna()
    if asof is not None:
        df = df[df.index < pd.Timestamp(asof)]
    n = len(df)
    if n == 0:
        return None, 0
    win = df.tail(lookback)
    if len(win) == 0:
        return None, n
    return float(np.median(win["c"] * win["v"])), n


def screen(rows: list, adv_max: float = ADV_MAX, adv_min: float = ADV_MIN,
           price_min: float = PRICE_MIN,
           min_history: int = MIN_HISTORY_DAYS) -> tuple:
    """Apply the inverted screen. Returns (kept, funnel_counts).

    The funnel is returned rather than logged so a caller can see WHERE a
    universe died. "0 names survived" is useless; "4,900 had no price, 800
    were too liquid, 3 were left" tells you what to do next.
    """
    funnel = {"input": len(rows), "no_price": 0, "short_history": 0,
              "price_too_low": 0, "too_liquid": 0, "too_illiquid": 0,
              "kept": 0}
    kept = []
    for r in rows:
        adv, n_bars = r.get("adv"), r.get("n_bars", 0)
        px = r.get("last_price")
        if adv is None or px is None:
            funnel["no_price"] += 1
            continue
        if n_bars < min_history:
            funnel["short_history"] += 1
            continue
        if px < price_min:
            funnel["price_too_low"] += 1
            continue
        if adv > adv_max:
            funnel["too_liquid"] += 1
            continue
        if adv < adv_min:
            funnel["too_illiquid"] += 1
            continue
        kept.append(r)
        funnel["kept"] += 1
    return kept, funnel


def format_funnel(funnel: dict) -> str:
    order = ["input", "no_price", "short_history", "price_too_low",
             "too_liquid", "too_illiquid", "kept"]
    return "\n".join(f"    {k:<16} {funnel.get(k, 0):>7,}" for k in order)


# --------------------------------------------------------------- network

def fetch_symbols(key: str) -> list:
    import requests
    r = requests.get(FINNHUB_SYMBOLS,
                     params={"exchange": "US", "token": key}, timeout=60)
    if r.status_code != 200:
        print(f"[universe] FATAL: symbol list HTTP {r.status_code}: "
              f"{r.text[:300]}")
        sys.exit(2)
    return r.json() or []


def fetch_prices(tickers: list, lookback_days: int = 400) -> dict:
    """Bulk OHLCV. Returns {ticker: (close, volume)}."""
    import yfinance as yf
    start = (pd.Timestamp.today() - pd.Timedelta(days=lookback_days)).date().isoformat()
    out = {}
    CH = 200
    for i in range(0, len(tickers), CH):
        chunk = tickers[i:i + CH]
        try:
            raw = yf.download(chunk, start=start, progress=False,
                              auto_adjust=True, threads=True)
        except Exception as exc:
            print(f"[universe] chunk {i // CH} download failed: {exc}")
            continue
        if raw is None or len(raw) == 0:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            for t in chunk:
                try:
                    c = pd.to_numeric(raw["Close"][t], errors="coerce").dropna()
                    v = pd.to_numeric(raw["Volume"][t], errors="coerce").dropna()
                except Exception:
                    continue
                if len(c):
                    out[t] = (c, v)
        else:
            c = pd.to_numeric(raw["Close"], errors="coerce").dropna()
            v = pd.to_numeric(raw["Volume"], errors="coerce").dropna()
            if len(c):
                out[chunk[0]] = (c, v)
        print(f"[universe] priced {len(out)}/{min(i + CH, len(tickers))}")
    return out


# ------------------------------------------------------------------ main

def main() -> None:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        print("[universe] FATAL: FINNHUB_API_KEY not set")
        sys.exit(2)

    print("[universe] fetching US symbol list…")
    raw = fetch_symbols(key)
    print(f"[universe] {len(raw):,} raw symbol(s) from Finnhub")
    syms = filter_symbols(raw)
    print(f"[universe] {len(syms):,} plausible operating companies "
          f"after type/format filter")

    types: dict = {}
    for d in raw or []:
        if isinstance(d, dict):
            t = str(d.get("type") or "?")
            types[t] = types.get(t, 0) + 1
    top = sorted(types.items(), key=lambda kv: -kv[1])[:10]
    print(f"[universe] symbol types seen: {top}")

    # MIC histogram over COMMON-STOCK rows only, so the exchange allowlist
    # can be checked against reality instead of trusted. The OTC line is the
    # one to watch: it is where Roche came from.
    mics: dict = {}
    for d in raw or []:
        if isinstance(d, dict) and str(d.get("type") or "").strip() in COMMON_TYPES:
            m = str(d.get("mic") or "?").strip().upper()
            mics[m] = mics.get(m, 0) + 1
    ranked = sorted(mics.items(), key=lambda kv: -kv[1])[:14]
    print(f"[universe] MIC histogram (Common Stock rows): {ranked}")
    kept_mics = sum(v for k, v in mics.items() if k in US_PRIMARY_MICS)
    print(f"[universe] {kept_mics:,} on primary US exchanges, "
          f"{sum(mics.values()) - kept_mics:,} elsewhere (OTC etc.) — excluded")

    if os.environ.get("QT_U_SYMBOLS_ONLY", "").strip() == "1":
        print("[universe] QT_U_SYMBOLS_ONLY=1 — stopping before price fetch.")
        return

    syms = load_or_draw_sample(syms)
    tickers = [r["ticker"] for r in syms]
    if MAX_SYMBOLS and len(tickers) > MAX_SYMBOLS:
        print(f"[universe] capping {len(tickers):,} -> {MAX_SYMBOLS:,} "
              f"(QT_U_MAX_SYMBOLS)")
        tickers = tickers[:MAX_SYMBOLS]

    print(f"[universe] downloading prices for {len(tickers):,} ticker(s)…")
    prices = fetch_prices(tickers)
    print(f"[universe] {len(prices):,} ticker(s) returned price data")

    rows = []
    by_tk = {r["ticker"]: r for r in syms}
    for tk, (c, v) in prices.items():
        adv, n_bars = compute_adv(c, v, asof=None, lookback=ADV_LOOKBACK)
        rows.append({**by_tk.get(tk, {"ticker": tk}),
                     "adv": adv, "n_bars": n_bars,
                     "last_price": float(c.iloc[-1]) if len(c) else None})
    for tk in tickers:
        if tk not in prices:
            rows.append({**by_tk.get(tk, {"ticker": tk}),
                         "adv": None, "n_bars": 0, "last_price": None})

    kept, funnel = screen(rows)
    print(f"\n[universe] SPECIFICATION: {ADV_MIN:,.0f} <= ADV <= {ADV_MAX:,.0f}, "
          f"price >= ${PRICE_MIN:.2f}, history >= {MIN_HISTORY_DAYS} bars, "
          f"ADV over {ADV_LOOKBACK} bars (median)")
    print("[universe] funnel:")
    print(format_funnel(funnel))

    if not kept:
        print("[universe] EMPTY universe — nothing written.")
        return
    df = pd.DataFrame(kept).sort_values("adv")
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"\n[universe] wrote {OUT_CSV} ({len(df):,} name(s))")
    print(f"[universe] ADV range ${df['adv'].min():,.0f} – ${df['adv'].max():,.0f}"
          f"  median ${df['adv'].median():,.0f}")
    print("\n[universe] ⚠️  SURVIVORSHIP: this is TODAY's listed universe. "
          "Delisted and acquired companies are absent, and insiders buy on "
          "the way down too. Any E1 result on this universe carries an "
          "UNQUANTIFIED UPWARD bias. State it with the number.")
    print("[universe] ⚠️  This tuple is part of ONE of the K=5 specifications "
          "in docs/V27_PREREGISTRATION.md. Re-screening at a different ADV "
          "ceiling is a SECOND specification.")


if __name__ == "__main__":
    main()
