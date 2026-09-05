#!/usr/bin/env python3
"""Availability probe — can the SURVIVORSHIP CEILING be removed, and how big is it?

Every v27 read so far reports `delisted 0`. That is not a happy finding, it is
the bias confirming itself: the universe was screened from TODAY's listings, so
every name in it survived by construction. Every number produced against it is
therefore an upper bound with an unknown floor, and that caveat is attached to
specs #1, #2 and #3 alike.

`probe_delisted.py` already established that yfinance serves 0 of 3 known
bankruptcies. What it did not establish is the two things that decide whether
this is fixable:

  1. HOW BIG IS THE HOLE? How many US listings actually went away over the
     study window? Unknown so far -- the caveat says "biases upward" without
     ever saying by how much.
  2. IS THERE A SOURCE THAT SERVES THEM? yfinance is one source and it is
     the only one that has been tried.

Both are answerable free.

THE HOLE. When a security stops trading on an exchange the EXCHANGE files a
Form 25 (or 25-NSE) with the SEC naming the issuer. EDGAR full-text search
indexes those and returns the ticker inside `display_names`, so the delisted
population can be enumerated directly rather than estimated. Measured
2026-09-05: the query works and returns e.g. MultiPlan (MPLN), GRIID (GRDI),
Benson Hill (BHIL).

THE SOURCES. Three are tried against the tickers Form 25 hands back:

  yfinance   the incumbent, and the reason the ceiling exists
  Stooq      free CSV, no key, historically retains delisted US names
  Alpaca     ALREADY PAID FOR -- the account this project trades through.
             If Alpaca serves delisted bars, the ceiling costs nothing to
             remove, which would be the single cheapest fix available.

Availability only. Computes no return, writes nothing under data/, spends no K.
"""
from __future__ import annotations

import collections
import io
import os
import sys
import time

import pandas as pd

UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
WINDOW_START, WINDOW_END = "2023-09-01", "2026-09-05"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
STOOQ = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/{sym}/bars"
N_PRICE_TEST = int(os.environ.get("QT_CEILING_N", "24"))
MAX_PAGES = int(os.environ.get("QT_CEILING_PAGES", "40"))
SLEEP = 0.15


def _get(sess, url, params=None, headers=None):
    import requests
    for attempt in range(3):
        try:
            r = sess.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException:
            time.sleep(1.2 * (attempt + 1)); continue
        if r.status_code == 200:
            return 200, r
        if r.status_code in (403, 404):
            return r.status_code, r
        time.sleep(1.2 * (attempt + 1))
    return -1, None


def _ticker_of(display_names) -> "str | None":
    """'Benson Hill, Inc.  (BHIL, BHILW)  (CIK 0001830210)' -> 'BHIL'.

    The first name is the issuer; the exchange is a co-filer and carries no
    ticker, so it is skipped. The first ticker in the parenthesis is the
    common stock -- BHILW is a warrant.
    """
    for nm in (display_names or []):
        if "(CIK" not in nm:
            continue
        head = nm.split("(CIK")[0]
        if "(" not in head:
            continue                      # exchange co-filer: no ticker
        inside = head[head.rfind("(") + 1:head.rfind(")")] if ")" in head else ""
        first = inside.split(",")[0].strip().upper()
        if first and first.replace(".", "").replace("-", "").isalnum() and len(first) <= 6:
            return first
    return None


def enumerate_delistings(sess) -> tuple:
    """Form 25 / 25-NSE filings in the window -> (total, [(ticker, date)])."""
    found, total_seen = {}, 0
    for form in ("25-NSE", "25"):
        st, r = _get(sess, FTS_URL, params={
            "q": '"delisting"', "forms": form,
            "startdt": WINDOW_START, "enddt": WINDOW_END})
        time.sleep(SLEEP)
        if st != 200:
            print(f"  forms={form}: HTTP {st}"); continue
        total = (((r.json().get("hits") or {}).get("total") or {}).get("value")) or 0
        total_seen += total
        print(f"  forms={form}: {total} filings in {WINDOW_START}..{WINDOW_END}")
        for page in range(MAX_PAGES):
            st2, r2 = _get(sess, FTS_URL, params={
                "q": '"delisting"', "forms": form, "from": page * 10,
                "startdt": WINDOW_START, "enddt": WINDOW_END})
            time.sleep(SLEEP)
            if st2 != 200:
                break
            hits = ((r2.json().get("hits") or {}).get("hits") or [])
            if not hits:
                break
            for h in hits:
                src = h.get("_source", {}) or {}
                tk = _ticker_of(src.get("display_names"))
                if tk and tk not in found:
                    found[tk] = str(src.get("file_date", ""))
    return total_seen, sorted(found.items())


YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"


def classify(sess, sym: str) -> tuple:
    """-> (exchange, first_date, last_date). Which KIND of name is this?

    A row count alone cannot tell three very different situations apart, and
    the first draft of this probe conflated them:

      still on NYSE/Nasdaq  the Form 25 removed a WARRANT or a UNIT, or the
                            issuer TRANSFERRED exchanges. Not a delisting at
                            all -- a false positive in the enumeration.
      OTC Markets           delisted from the exchange and still trading over
                            the counter. SAME company, continuous price
                            history, and exactly the data a survivorship
                            correction needs.
      nothing               the true hole.

    Measured 2026-09-05: ACVA is still on NYSE (ACV Auctions, alive), while
    AAGR and AFIB moved to OTCPK and their series run to the present. So the
    delisted enumeration DOES over-count, and the price coverage IS real.
    """
    st, r = _get(sess, YAHOO_CHART.format(sym=sym), params={"range": "5y", "interval": "1d"},
                 headers={"User-Agent": "Mozilla/5.0"})
    if st != 200 or r is None:
        return f"http:{st}", "", ""
    try:
        res = ((r.json().get("chart") or {}).get("result") or [None])[0] or {}
    except ValueError:
        return "err", "", ""
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    fmt = lambda t: pd.Timestamp(t, unit="s").strftime("%Y-%m-%d") if t else ""
    return str(meta.get("fullExchangeName") or "?"), fmt(ts[0] if ts else 0), fmt(ts[-1] if ts else 0)


def bucket_of(exchange: str) -> str:
    e = (exchange or "").upper()
    if "OTC" in e or "PINK" in e:
        return "otc-continuation"
    if any(x in e for x in ("NYSE", "NASDAQ", "AMEX", "NASDAQGS", "NASDAQCM")):
        return "still-listed"
    return "no-data"


def try_yfinance(sym: str) -> tuple:
    try:
        import yfinance as yf
        df = yf.download(sym, start="2023-01-01", progress=False, auto_adjust=True)
        n = 0 if df is None else len(df)
        return (n > 0), n
    except Exception as exc:
        return False, f"err:{type(exc).__name__}"


def try_stooq(sess, sym: str) -> tuple:
    st, r = _get(sess, STOOQ.format(sym=sym.lower()))
    if st != 200 or r is None:
        return False, f"http:{st}"
    body = r.text.strip()
    if not body or body.lower().startswith("no data") or "\n" not in body:
        return False, 0
    try:
        df = pd.read_csv(io.StringIO(body))
    except Exception:
        return False, 0
    if "Date" not in df.columns or len(df) == 0:
        return False, 0
    recent = df[df["Date"] >= "2023-01-01"]
    return (len(recent) > 0), len(recent)


def try_alpaca(sess, sym: str, key: str, secret: str) -> tuple:
    st, r = _get(sess, ALPACA_BARS.format(sym=sym),
                 params={"timeframe": "1Day", "start": "2023-01-01",
                         "end": "2026-09-01", "limit": 200, "feed": "iex"},
                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    if st != 200 or r is None:
        return False, f"http:{st}"
    bars = (r.json() or {}).get("bars") or []
    return (len(bars) > 0), len(bars)


def main() -> None:
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})

    print(f"[ceiling] window {WINDOW_START}..{WINDOW_END}; UA={UA!r}\n")
    print("=" * 72); print("1. HOW BIG IS THE HOLE — Form 25 / 25-NSE delisting notices"); print("=" * 72)
    total, delisted = enumerate_delistings(sess)
    print(f"\n  {total} delisting filings; {len(delisted)} distinct tickers recovered "
          f"(capped at {MAX_PAGES} pages per form)")
    if not delisted:
        print("  no tickers recovered — cannot test price sources"); sys.exit(1)
    by_year = collections.Counter(d[:4] for _, d in delisted if d)
    print(f"  by year: {dict(sorted(by_year.items()))}")
    print(f"  sample: {', '.join(t for t, _ in delisted[:18])}")

    print("\n" + "=" * 72)
    print(f"2. CAN ANY SOURCE PRICE THEM — {N_PRICE_TEST} delisted names, 3 sources")
    print("=" * 72)
    # .strip() does NOT remove a byte-order mark, and requests encodes headers
    # as latin-1, so a BOM-prefixed secret raises UnicodeEncodeError before the
    # request is ever sent. Measured: run 33942204384 died exactly there. The
    # live pipeline is unaffected -- alpaca-py encodes headers as UTF-8 -- but a
    # BOM inside a stored secret is worth knowing about.
    _BOM = "﻿"
    key = (os.environ.get("ALPACA_API_KEY") or "").replace(_BOM, "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").replace(_BOM, "").strip()
    if not (key and secret):
        print("  ⚠️ ALPACA_API_KEY/ALPACA_SECRET_KEY not set — Alpaca column will read 'nokey'")
    sample = [t for t, _ in delisted[:N_PRICE_TEST]]
    score = collections.Counter()
    buckets = collections.Counter()
    print(f"\n  {'ticker':<8} {'form25':<12} {'exchange now':<22} {'series ends':<12} "
          f"{'yf':<7} {'stooq':<7} {'alpaca':<7}")
    for tk in sample:
        when = dict(delisted).get(tk, "")
        exch, _first, last = classify(sess, tk); time.sleep(SLEEP)
        b = bucket_of(exch); buckets[b] += 1
        # FAIL SOFT per source. A probe that dies on source 3 throws away what
        # sources 1 and 2 already established.
        def _safe(fn, *a):
            try:
                return fn(*a)
            except Exception as exc:
                return False, f"err:{type(exc).__name__}"
        y_ok, y_n = _safe(try_yfinance, tk)
        s_ok, s_n = _safe(try_stooq, sess, tk); time.sleep(SLEEP)
        if key and secret:
            a_ok, a_n = _safe(try_alpaca, sess, tk, key, secret); time.sleep(SLEEP)
        else:
            a_ok, a_n = False, "nokey"
        score["yfinance"] += int(y_ok); score["stooq"] += int(s_ok); score["alpaca"] += int(a_ok)
        print(f"  {tk:<8} {when:<12} {exch[:22]:<22} {last:<12} "
              f"{str(y_n):<7} {str(s_n):<7} {str(a_n):<7}")

    n = len(sample)
    print(f"\n  {'source':<12} {'served':<10} rate")
    for src in ("yfinance", "stooq", "alpaca"):
        print(f"  {src:<12} {score[src]}/{n:<8} {100*score[src]/max(1,n):.0f}%")
    print(f"\n  WHAT THESE NAMES ACTUALLY ARE:")
    for b in ("still-listed", "otc-continuation", "no-data"):
        print(f"    {buckets[b]:>3}/{n}  {b}")
    print(f"    still-listed  = the Form 25 removed a warrant/unit or was an exchange TRANSFER.")
    print(f"                    A FALSE POSITIVE in the enumeration above, not a delisting.")
    print(f"    otc-continue  = delisted from the exchange, still trading OTC. SAME company,")
    print(f"                    continuous history — exactly what a survivorship fix needs.")
    print(f"    no-data       = the true hole.")

    print("\n" + "=" * 72)
    best = max((("yfinance", score["yfinance"]), ("stooq", score["stooq"]),
                ("alpaca", score["alpaca"])), key=lambda kv: kv[1])
    if best[1] >= 0.6 * n:
        print(f"✅ THE CEILING IS LARGELY REMOVABLE AT ZERO COST: {best[0]} serves {best[1]}/{n}.")
        print(f"   Most exchange delistings CONTINUE trading OTC and the series runs to the")
        print(f"   present, so the price path a strategy would actually have experienced is")
        print(f"   obtainable. This does not raise measured returns — it LOWERS them, which is")
        print(f"   the whole point of removing a ceiling.")
        print(f"   ⚠️ TWO THINGS THIS DOES NOT SETTLE. (1) The enumeration over-counts: "
              f"{buckets['still-listed']}/{n}")
        print(f"   of these are still listed, so Form 25 alone is not a delisting filter. (2) A")
        print(f"   ticker can be REUSED after a delisting; matching by CIK rather than symbol is")
        print(f"   required before any of this is spliced into a universe.")
    elif best[1] > 0:
        print(f"🟡 PARTIAL: {best[0]} serves {best[1]}/{n}. Enough to BOUND the bias — price the "
              f"names\n   that are available, treat the rest as a stated unknown — but not to "
              f"eliminate it.")
    else:
        print(f"🔴 NO FREE SOURCE SERVES DELISTED NAMES. The hole is now MEASURED ({total} "
              f"filings)\n   even though it cannot be filled free. A paid survivorship-free "
              f"vendor is the only fix,\n   and the caveat stays on every number until then.")
    print("[ceiling] done. Availability only — no return computed, nothing written, K untouched.")


if __name__ == "__main__":
    main()
