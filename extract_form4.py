#!/usr/bin/env python3
"""Form 4 insider-purchase extractor — v27 component A3.

Turns insider-transaction data into candidate EVENTS for `event_study.py`.

WHY FINNHUB AND NOT SEC EDGAR
-----------------------------
The obvious source is SEC EDGAR, and `quant_runner.py:6984` already talks to
it. It is a DEAD PATH from CI, and the repo already knows this
(`quant_runner.py:6900`):

    "The SEC EDGAR scraper returns 0 from GitHub Actions because SEC
     throttles shared datacenter IPs regardless of User-Agent."

Confirmed again 2026-09-01 — a direct request returns SEC's "Your Request
Originates from an Undeclared Automated Tool" page, not JSON. Finnhub works
from those same IPs, the key is already configured, and the live run on
2026-09-01 pulled 100 transactions from it.

The cost of that substitution is real and is recorded here rather than
discovered later: **Finnhub gives `filingDate`, a DATE with no intraday
time.** EDGAR's `acceptanceDateTime` would have told us whether a filing
landed at 09:00 (tradeable that session) or 16:30 (not). Without it, the
only defensible reading is that a filing dated D is actionable at the close
of the NEXT session — which is exactly what `event_study.py` does with a
date-valued `event_ts`, because its entry rule is "first bar strictly after".

🔑 This BIASES MEASURED EFFECTS DOWNWARD by up to one session. That is the
right direction to be wrong in. Do not "fix" it by entering on the filing
date without a real timestamp — that is a look-ahead wearing a bug fix's
clothes, and it is the same class of error as v25's stale-signal bug.

WHAT COUNTS AGAINST K
---------------------
`docs/V27_PREREGISTRATION.md` declares **K = 5** specifications. Running this
extractor does NOT consume K — it is a data pipeline, and it emits every
open-market purchase it can see along with the metadata a filter would need.

**A SPECIFICATION is the (min_insiders, window_days, min_value) triple plus
the universe.** Choosing one and reading its E1 result consumes one of the
five. Sweeping the triple to see which looks best consumes all five and then
keeps going, which is the failure mode WRC/SPA exists to punish. Declare the
triple in the pre-registration BEFORE reading, or the read does not count.

TRANSACTION CODES
-----------------
Only Form 4 code **P** (open-market purchase) is kept. This matters more
than it looks: `quant_runner.py`'s existing insider block infers buy/sell
from the SIGN of `change`, which lumps in code A (grant/award), M and X
(option exercises) and G (gifts). Those are compensation events, not
somebody choosing to buy their own stock with their own money, and mixing
them is how an "insider buying" signal becomes an "insider got paid" signal.

ENV
---
  FINNHUB_API_KEY        required for live fetch (a GitHub secret)
  QT_F4_UNIVERSE         comma-separated tickers, or a path to a file of them
  QT_F4_LOOKBACK_DAYS    default 365
  QT_F4_OUT              default data/events/events_form4.csv
  QT_F4_RAW              default data/events/form4_raw.csv
  QT_F4_MIN_INSIDERS     default 2      ] the SPECIFICATION triple --
  QT_F4_WINDOW_DAYS      default 5      ] declare these before reading,
  QT_F4_MIN_VALUE        default 100000 ] they are not free parameters
  QT_F4_PROBE            "1" -> fetch ONE ticker, print the observed schema

Probe mode exists because this file was written without ever having seen a
live Finnhub insider payload — the key is a CI secret. Assuming a schema and
discovering it was wrong three weeks into an accumulation window is exactly
the kind of thing this project has already paid for once.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

FINNHUB_URL = "https://finnhub.io/api/v1/stock/insider-transactions"

OUT_CSV = Path(os.environ.get("QT_F4_OUT", "data/events/events_form4.csv"))
RAW_CSV = Path(os.environ.get("QT_F4_RAW", "data/events/form4_raw.csv"))
LOOKBACK_DAYS = int(os.environ.get("QT_F4_LOOKBACK_DAYS", "365"))

# ---- THE SPECIFICATION TRIPLE. Declared, not tuned. See module docstring.
MIN_INSIDERS = int(os.environ.get("QT_F4_MIN_INSIDERS", "2"))
WINDOW_DAYS = int(os.environ.get("QT_F4_WINDOW_DAYS", "5"))
MIN_VALUE = float(os.environ.get("QT_F4_MIN_VALUE", "100000"))

PURCHASE_CODE = "P"


# ------------------------------------------------------------------ pure

def normalise(raw_rows: list, ticker: str) -> list:
    """Finnhub payload -> flat records. Tolerant of field drift by design.

    Field names are taken from the shape `quant_runner.py` already consumes
    (`change`, `transactionPrice`, `filingDate`, `transactionDate`, `name`)
    plus `transactionCode`, which that code ignores. Anything missing yields
    None rather than an exception, and `--probe` prints what actually came
    back so a mismatch is visible immediately instead of silently emptying
    the pipeline.
    """
    out = []
    for d in raw_rows or []:
        if not isinstance(d, dict):
            continue
        try:
            change = float(d.get("change") or 0)
        except (TypeError, ValueError):
            change = 0.0
        try:
            price = float(d.get("transactionPrice") or 0)
        except (TypeError, ValueError):
            price = 0.0
        out.append({
            "ticker": ticker.upper(),
            "name": str(d.get("name") or "").strip(),
            "code": str(d.get("transactionCode") or "").strip().upper(),
            "shares": change,
            "price": price,
            "value": abs(change) * price,
            "filing_date": str(d.get("filingDate") or "").strip(),
            "transaction_date": str(d.get("transactionDate") or "").strip(),
            # Confirmed present by the 2026-09-01 live probe. `id` is a stable
            # per-transaction key (better than name+date for dedup);
            # `is_derivative` lets us exclude derivative rows explicitly
            # rather than trusting code P to imply non-derivative.
            "tx_id": str(d.get("id") or "").strip(),
            "is_derivative": bool(d.get("isDerivative") or False),
            "currency": str(d.get("currency") or "").strip().upper(),
        })
    return out


def filter_open_market_purchases(rows: list) -> list:
    """Code P only, positive share change, a real price, a filing date.

    A zero price is a Form 4 that reported no per-share price (gifts,
    certain plans). Its dollar value is unknowable, so it cannot clear a
    min_value bar and is dropped rather than counted as $0.
    """
    keep = []
    for r in rows:
        if r["code"] != PURCHASE_CODE:
            continue
        # Explicit, not implied. The live probe confirmed Finnhub carries an
        # isDerivative flag, so exclude derivative rows on the flag rather
        # than assuming code P never appears on one.
        if r.get("is_derivative"):
            continue
        # Non-USD prices would be compared against USD bars and against a USD
        # benchmark. Blank is tolerated (older rows omit it); a stated
        # non-USD currency is not.
        cur = r.get("currency") or ""
        if cur and cur != "USD":
            continue
        if r["shares"] <= 0 or r["price"] <= 0:
            continue
        if not r["filing_date"]:
            continue
        keep.append(r)
    return keep


def cluster(rows: list, min_insiders: int = MIN_INSIDERS,
            window_days: int = WINDOW_DAYS,
            min_value: float = MIN_VALUE) -> list:
    """Group purchases into cluster-buy events.

    A cluster is >= `min_insiders` DISTINCT insiders filing purchases on the
    same ticker within a `window_days` calendar window, with aggregate value
    >= `min_value`.

    Distinctness is by insider name. Two filings from the same person on
    consecutive days are one person changing their position, not two people
    independently deciding to buy, and counting them as a cluster is the
    cheapest possible way to manufacture events that are not there.

    The event timestamp is the LAST filing date in the cluster -- the moment
    the full cluster became visible. Using the first would mean claiming to
    have known about a cluster before the filings that constitute it existed.
    """
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df["fd"] = pd.to_datetime(df["filing_date"], errors="coerce")
    df = df.dropna(subset=["fd"]).sort_values(["ticker", "fd"])

    events = []
    for tk, grp in df.groupby("ticker"):
        grp = grp.sort_values("fd").reset_index(drop=True)
        used = set()
        for i in range(len(grp)):
            if i in used:
                continue
            t0 = grp.loc[i, "fd"]
            win = grp[(grp["fd"] >= t0)
                      & (grp["fd"] <= t0 + pd.Timedelta(days=window_days))]
            win = win[~win.index.isin(used)]
            names = set(win["name"]) - {""}
            total = float(win["value"].sum())
            if len(names) >= min_insiders and total >= min_value:
                last = win["fd"].max()
                events.append({
                    "event_id": f"f4_{tk}_{last.date().isoformat()}",
                    "ticker": tk,
                    "event_ts": last.date().isoformat(),
                    "event_type": "form4_cluster_buy",
                    "n_insiders": len(names),
                    "n_filings": int(len(win)),
                    "total_value": round(total, 2),
                    "first_filing": t0.date().isoformat(),
                })
                used.update(win.index.tolist())
    return sorted(events, key=lambda e: (e["event_ts"], e["ticker"]))


# --------------------------------------------------------------- network

def _universe() -> list:
    raw = os.environ.get("QT_F4_UNIVERSE", "").strip()
    if not raw:
        # Same 30 names quant_runner.py already queries. Deliberately the
        # WRONG universe for v27 -- these are the mega-caps the retired
        # project measured as having no edge. It is here so the pipeline can
        # be exercised end to end; the inverted screen is A4.
        return ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
                "JPM", "V", "MA", "AMD", "NFLX", "AVGO", "CRM", "NOW",
                "PLTR", "GS", "MS", "WMT", "COST", "UNH", "LLY", "XOM",
                "HD", "INTC", "QCOM", "MU", "TXN", "BA", "CVX"]
    p = Path(raw)
    if p.exists():
        return read_universe_file(p)
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def read_universe_file(p: Path) -> list:
    """Ticker list from either a CSV with a `ticker` column or a plain list.

    A4 writes `data/universe/v27_universe.csv`, which has a header and six
    columns. Reading it line-by-line would silently produce tickers like
    "TICKER,DESCRIPTION,TYPE" and then fetch nothing for any of them -- an
    empty result that looks exactly like "this universe has no insider
    buying" rather than like a parsing bug. Detect the header instead.
    """
    text = p.read_text()
    first = text.splitlines()[0] if text.splitlines() else ""
    if "," in first and "ticker" in first.lower():
        df = pd.read_csv(p)
        col = next((c for c in df.columns if c.strip().lower() == "ticker"),
                   None)
        if col is None:
            raise ValueError(f"{p} looks like a CSV but has no ticker column")
        return [str(t).strip().upper() for t in df[col].dropna()
                if str(t).strip()]
    return [ln.strip().upper() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]


def fetch(tickers: list, key: str, lookback_days: int) -> list:
    import datetime as dt
    import time
    import requests

    to_d = dt.date.today()
    from_d = (to_d - dt.timedelta(days=lookback_days)).isoformat()
    rows, failed = [], 0
    for tk in tickers:
        try:
            r = requests.get(FINNHUB_URL,
                             params={"symbol": tk, "from": from_d,
                                     "to": to_d.isoformat(), "token": key},
                             timeout=15)
            if r.status_code != 200:
                failed += 1
                continue
            rows.extend(normalise(r.json().get("data") or [], tk))
            time.sleep(1.1)          # free tier is 60 calls/min
        except Exception:
            failed += 1
            continue
    if failed:
        print(f"[form4] {failed}/{len(tickers)} ticker(s) failed to fetch")
    return rows


def probe(key: str) -> None:
    """Survey the universe's transaction-code mix and confirm the schema.

    Written because this module was authored without ever having seen a live
    Finnhub insider payload. Verifying the shape costs a few API calls;
    assuming it costs an accumulation window.

    The FIRST probe (2026-09-01, AAPL, 365d) returned 116 rows with codes
    {'S':30, 'M':53, 'A':17, 'F':12, 'G':4} and ZERO 'P'. That is the whole
    finding: mega-cap insiders are COMPENSATED in stock and sell it; they do
    not buy it on the open market. So this probe now surveys several names,
    because "does this universe contain any purchases at all" decides
    whether the extractor can produce events before any statistics matter.
    """
    import requests
    import datetime as dt
    import time
    n_probe = int(os.environ.get("QT_F4_PROBE_N", "8"))
    tickers = _universe()[:n_probe]
    to_d = dt.date.today()
    from_d = (to_d - dt.timedelta(days=365)).isoformat()

    all_codes: dict = {}
    total_rows = 0
    schema_printed = False
    per_ticker = []
    for tk in tickers:
        r = requests.get(FINNHUB_URL,
                         params={"symbol": tk, "from": from_d,
                                 "to": to_d.isoformat(), "token": key},
                         timeout=20)
        if r.status_code != 200:
            print(f"[form4 probe] {tk} HTTP {r.status_code}: {r.text[:200]}")
            sys.exit(1)
        data = r.json().get("data") or []
        total_rows += len(data)
        if data and not schema_printed:
            print(f"[form4 probe] record keys: {sorted(data[0].keys())}")
            print(f"[form4 probe] sample:\n{json.dumps(data[0], indent=2)[:600]}")
            has_time = any(":" in str(d.get('filingDate') or '') for d in data)
            print(f"[form4 probe] filingDate carries a TIME component: {has_time}")
            print("[form4 probe] (False -> next-session entry is the only safe read)")
            schema_printed = True
        codes: dict = {}
        for d in data:
            c = str(d.get("transactionCode") or "?")
            codes[c] = codes.get(c, 0) + 1
            all_codes[c] = all_codes.get(c, 0) + 1
        per_ticker.append((tk, len(data), codes.get(PURCHASE_CODE, 0)))
        time.sleep(1.1)

    print(f"\n[form4 probe] surveyed {len(tickers)} ticker(s), "
          f"{total_rows} transaction(s) over 365d")
    for tk, n, np_ in per_ticker:
        print(f"[form4 probe]   {tk:<6} {n:>4} rows   P={np_}")
    print(f"[form4 probe] UNIVERSE code histogram: {all_codes}")
    n_p = all_codes.get(PURCHASE_CODE, 0)
    print(f"[form4 probe] open-market purchases (P): {n_p}")
    if n_p == 0:
        print("[form4 probe] ⚠️  ZERO open-market purchases in this universe. "
              "The extractor is working; the UNIVERSE is wrong. Mega-cap "
              "insiders are paid in stock and sell it — they do not buy it. "
              "A3 cannot produce events until A4 (the inverted screen: small, "
              "illiquid, uncovered) supplies names whose insiders actually "
              "buy. This is a data-availability fact, not a tuning problem.")


# ------------------------------------------------------------------ main

def main() -> None:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if os.environ.get("QT_F4_PROBE", "").strip() == "1":
        if not key:
            print("[form4] FATAL: probe needs FINNHUB_API_KEY")
            sys.exit(2)
        probe(key)
        return
    if not key:
        print("[form4] FATAL: FINNHUB_API_KEY not set. SEC EDGAR is NOT a fallback "
              "from CI — it returns 0 (see module docstring).")
        sys.exit(2)

    tickers = _universe()
    print(f"[form4] fetching {len(tickers)} ticker(s), lookback {LOOKBACK_DAYS}d")
    raw = fetch(tickers, key, LOOKBACK_DAYS)
    print(f"[form4] {len(raw)} raw insider transaction(s)")
    if raw:
        RAW_CSV.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(raw).to_csv(RAW_CSV, index=False)
        print(f"[form4] wrote {RAW_CSV}")

    buys = filter_open_market_purchases(raw)
    codes = {}
    for r in raw:
        codes[r["code"] or "?"] = codes.get(r["code"] or "?", 0) + 1
    print(f"[form4] transaction codes seen: {codes}")
    print(f"[form4] {len(buys)} open-market purchase(s) (code {PURCHASE_CODE}) "
          f"of {len(raw)} — the rest are awards, exercises, gifts and sales")

    events = cluster(buys, MIN_INSIDERS, WINDOW_DAYS, MIN_VALUE)
    print(f"\n[form4] SPECIFICATION: >= {MIN_INSIDERS} distinct insiders "
          f"within {WINDOW_DAYS}d, aggregate >= ${MIN_VALUE:,.0f}")
    print(f"[form4] -> {len(events)} cluster-buy event(s)")
    print("[form4] ⚠️  This triple is ONE of the K=5 specifications in "
          "docs/V27_PREREGISTRATION.md. Sweeping it and keeping the best "
          "read invalidates E1.")

    if not events:
        print("[form4] no events — nothing written.")
        return
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(events).to_csv(OUT_CSV, index=False)
    print(f"[form4] wrote {OUT_CSV} ({len(events)} event(s))")
    n = len(events)
    print(f"[form4] E1 needs N >= 40 INDEPENDENT events; this is {n} before "
          f"event_study.py's overlap filter, so expect fewer.")


if __name__ == "__main__":
    main()
