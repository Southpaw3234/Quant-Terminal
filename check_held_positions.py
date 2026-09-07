#!/usr/bin/env python3
"""Daily check over HELD POSITIONS ONLY — is anything we own in trouble?

This is the operational twin of the roster, and it is deliberately a different
job with opposite requirements:

    the roster        wide, slow, frozen. Every delisting in the market, kept
                      stable so a specification can be reproduced.
    this              narrow, fast, current. Twenty names, checked today.

Conflating them is what makes people sweep all of EDGAR hourly to answer a
question about twenty stocks. This asks only about what is actually held, so
it takes seconds.

WHAT IT CHECKS
  * price staleness -- has the name stopped trading?
  * a Form 25 filed against its CIK -- is it leaving the exchange?
  * whether the position read can be trusted at all

WHAT IT DOES NOT DO: place, cancel or size an order, or write to any research
input. It reports. Execution stays a separate, guarded, deliberate step, and
this file has no broker-trading import at all.

🔑 IT REFUSES ON AN UNTRUSTED POSITION READ. On 2026-09-01 the broker returned
zero positions, every guard believed it, and the system recorded "flat" while
short. A monitor that reports "nothing held, all clear" on that same empty
read is worse than no monitor, because it converts a broker glitch into a
green light.
"""
from __future__ import annotations

import os
import sys
import time

import pandas as pd

STALE_DAYS = int(os.environ.get("QT_HELD_STALE_DAYS", "5"))
PORTFOLIO = os.environ.get("QT_HELD_PORTFOLIO", "data/portfolios/value_ebit_ev_v1.csv")
ALPACA_POSITIONS = "https://paper-api.alpaca.markets/v2/positions"
ALPACA_ACCOUNT = "https://paper-api.alpaca.markets/v2/account"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"


def _bom(s: str) -> str:
    # A stored secret carrying a byte-order mark raises UnicodeEncodeError in
    # requests before the call is made. Measured on run 34050275750.
    return (s or "").replace("﻿", "").strip()


def read_positions(sess, key: str, secret: str) -> tuple:
    """-> (positions, trustworthy, why). Never guesses a flat book."""
    import requests
    try:
        acct = sess.get(ALPACA_ACCOUNT, timeout=20,
                        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        pos = sess.get(ALPACA_POSITIONS, timeout=20,
                       headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    except requests.RequestException as exc:
        return [], False, f"broker unreachable ({type(exc).__name__})"
    if acct.status_code != 200:
        return [], False, f"account read failed (HTTP {acct.status_code})"
    if pos.status_code != 200:
        return [], False, f"position read failed (HTTP {pos.status_code})"
    rows = pos.json() or []
    if not rows:
        # THE 2026-09-01 FAILURE. An empty list is not evidence of a flat book;
        # it is evidence of an empty response, and those are different things.
        return [], False, ("EMPTY position list. This is NOT proof of a flat "
                           "book — on 2026-09-01 the broker returned exactly "
                           "this while the account was short")
    return rows, True, "ok"


def price_state(sess, sym: str) -> dict:
    import requests
    try:
        r = sess.get(YAHOO_CHART.format(sym=sym), timeout=20,
                     params={"range": "1mo", "interval": "1d"},
                     headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException:
        return {"last": "", "days_stale": None, "exchange": "", "ok": False}
    if r.status_code != 200:
        return {"last": "", "days_stale": None, "exchange": "", "ok": False}
    try:
        res = ((r.json().get("chart") or {}).get("result") or [None])[0] or {}
    except ValueError:
        return {"last": "", "days_stale": None, "exchange": "", "ok": False}
    ts = [t for t in (res.get("timestamp") or []) if t]
    meta = res.get("meta") or {}
    if not ts:
        return {"last": "", "days_stale": None,
                "exchange": str(meta.get("fullExchangeName") or ""), "ok": False}
    last = pd.Timestamp(ts[-1], unit="s").normalize()
    return {"last": last.strftime("%Y-%m-%d"),
            "days_stale": int((pd.Timestamp.utcnow().normalize().tz_localize(None) - last).days),
            "exchange": str(meta.get("fullExchangeName") or ""), "ok": True}


def has_departure_filing(sess, ticker: str) -> bool:
    """Any Form 25 against this name in the last year."""
    import requests
    try:
        r = sess.get(FTS_URL, timeout=25, headers={"User-Agent": UA},
                     params={"q": '"securities"', "forms": "25-NSE",
                             "entityName": ticker,
                             "startdt": (pd.Timestamp.utcnow() - pd.Timedelta(days=365)
                                         ).strftime("%Y-%m-%d"),
                             "enddt": pd.Timestamp.utcnow().strftime("%Y-%m-%d")})
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    try:
        hits = ((r.json().get("hits") or {}).get("hits") or [])
    except ValueError:
        return False
    for h in hits:
        names = " ".join((h.get("_source", {}) or {}).get("display_names", []) or [])
        if f"({ticker})" in names or f"({ticker}," in names:
            return True
    return False


def main() -> None:
    import requests
    sess = requests.Session()
    key = _bom(os.environ.get("ALPACA_API_KEY"))
    secret = _bom(os.environ.get("ALPACA_SECRET_KEY"))

    print(f"[held] daily check — positions only, {time.strftime('%Y-%m-%d %H:%M UTC')}")
    held, trustworthy, why = ([], False, "no broker credentials")
    if key and secret:
        held, trustworthy, why = read_positions(sess, key, secret)

    if not trustworthy:
        print(f"\n🔴 POSITION READ NOT TRUSTWORTHY: {why}")
        print("   REFUSING to report an all-clear. A monitor that says 'nothing held,")
        print("   all clear' on an empty or failed read converts a broker glitch into")
        print("   a green light, which is exactly what happened on 2026-09-01.")
        if os.path.exists(PORTFOLIO):
            pf = pd.read_csv(PORTFOLIO)
            last = pf[pf["rebalance"] == pf["rebalance"].max()]
            print(f"\n   For reference only, the newest MODELLED portfolio "
                  f"({pf['rebalance'].max()}): {', '.join(last['ticker'].tolist())}")
            print("   That is what the model would hold, NOT what the account holds.")
        sys.exit(2)

    syms = sorted({str(p.get("symbol", "")).upper() for p in held if p.get("symbol")})
    print(f"[held] {len(syms)} positions: {', '.join(syms)}\n")
    print(f"  {'ticker':<8} {'qty':>10} {'last bar':<12} {'stale':<7} {'exchange':<22} form25")
    alerts = []
    for p in held:
        sym = str(p.get("symbol", "")).upper()
        qty = p.get("qty", "")
        st = price_state(sess, sym)
        time.sleep(0.2)
        stale = st["days_stale"]
        flag = ""
        if not st["ok"] or stale is None:
            flag = "NO PRICE"
        elif stale > STALE_DAYS:
            flag = f"{stale}d STALE"
        dep = has_departure_filing(sess, sym)
        time.sleep(0.2)
        if dep:
            flag = (flag + " +FORM25").strip()
        if flag:
            alerts.append(f"{sym}: {flag} (exchange {st['exchange'] or 'unknown'})")
        print(f"  {sym:<8} {str(qty):>10} {st['last']:<12} "
              f"{('' if stale is None else str(stale)+'d'):<7} "
              f"{st['exchange'][:22]:<22} {'YES' if dep else ''}")

    print("\n" + "=" * 70)
    if alerts:
        print("🔴 ATTENTION:")
        for a in alerts:
            print(f"   {a}")
        print("\n   A held name that has stopped trading or filed for removal is the")
        print("   one case where waiting a week is too slow. This reports it; it")
        print("   does NOT act on it, and no order is placed from here.")
    else:
        print(f"✅ All {len(syms)} held names traded within {STALE_DAYS} days and none "
              f"has a Form 25 against it.")
    print("[held] read-only. No order placed, no research input written.")


if __name__ == "__main__":
    main()
