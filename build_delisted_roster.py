#!/usr/bin/env python3
"""Build the roster of names that LEFT — step 1 of the survivorship fix.

`data/universe/v27_universe.csv` was screened from today's listings, so a
company that was liquid in 2024 and gone by 2025 is absent from it by
construction. Every v27 read therefore reports `delisted 0`, and every number
carries "biases upward, unfixable free". This file is the first of the three
pieces that make that statement false.

It produces an inventory of exchange departures over the study window, keyed
by CIK, with enough per-name fact for the point-in-time screen to consume next.
It does NOT screen, does not price into a universe, and computes no return.

WHAT THE PROBE ESTABLISHED, AND WHY THIS IS NOT JUST "LIST THE FORM 25s"
------------------------------------------------------------------------
probe_ceiling.py (run 33942599424) measured three things that shape every
design choice below:

  * 20/24 of these names ARE priced by yfinance -- the standing belief that
    delisted names cannot be obtained free came from a 3-name sample of
    bankruptcies and does not survive a bigger one.
  * 18/24 are OTC CONTINUATIONS: delisted from the exchange, still trading
    over the counter, same company, series running to the present. That is
    the price path a strategy would actually have experienced.
  * 2/24 were STILL LISTED on NYSE. ACV Auctions is alive. A Form 25 removes
    A SECURITY -- a warrant, a unit -- and is also filed on an exchange
    TRANSFER. So the filing alone is NOT a delisting signal, and a roster
    built by listing Form 25s would be wrong about roughly one name in ten.

So each candidate is checked against what its common stock is doing NOW, and
the roster records the classification rather than asserting one.

⚠️ TICKER REUSE IS THE TRAP THIS IS KEYED AGAINST. A symbol freed by a
delisting gets reassigned, so "ADN today" need not be "ADN in 2024". Rows are
keyed on CIK, and the issuer name EDGAR filed under is compared with the name
the price source reports today. A mismatch is flagged, never silently
resolved -- splicing two companies into one series would manufacture exactly
the kind of return that looks like alpha.

Writes data/universe/delisted_roster.csv. Computes no return, spends no K.
"""
from __future__ import annotations

import collections
import os
import re
import sys
import time

import pandas as pd

UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
WINDOW_START = os.environ.get("QT_ROSTER_START", "2023-09-01")
WINDOW_END = os.environ.get("QT_ROSTER_END", "2026-09-05")
OUT_CSV = os.environ.get("QT_ROSTER_OUT", "data/universe/delisted_roster.csv")
MAX_PAGES = int(os.environ.get("QT_ROSTER_PAGES", "120"))
LIMIT = int(os.environ.get("QT_ROSTER_LIMIT", "0"))       # 0 = every candidate
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
SLEEP_SEC = 0.15
SLEEP_YF = 0.25
if LIMIT:
    OUT_CSV = OUT_CSV.replace(".csv", f"_SMOKE{LIMIT}.csv")

MAJOR = ("NYSE", "NASDAQ", "AMEX", "NYSEARCA", "CBOE")
_SUFFIXES = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
             "PLC", "HOLDINGS", "HOLDING", "GROUP", "LLC", "LP", "SA", "NV",
             "AG", "THE", "CLASS", "COM", "TRUST", "INTERNATIONAL"}


# ═══════════════════════════════════════════════════ pure logic (no network)

def tickers_of(display_names) -> list:
    """['Benson Hill, Inc.  (BHIL, BHILW)  (CIK 0001830210)', 'NYSE ... (CIK ...)']
    -> ['BHIL', 'BHILW'].

    The exchange is a co-filer on every Form 25 and carries no ticker
    parenthesis, so it drops out naturally rather than by name matching.
    """
    for nm in (display_names or []):
        if "(CIK" not in nm:
            continue
        head = nm.split("(CIK")[0]
        if "(" not in head or ")" not in head:
            continue
        inside = head[head.rfind("(") + 1:head.rfind(")")]
        out = []
        for part in inside.split(","):
            t = part.strip().upper()
            if t and len(t) <= 6 and t.replace(".", "").replace("-", "").isalnum():
                out.append(t)
        if out:
            return out
    return []


def issuer_of(display_names) -> str:
    for nm in (display_names or []):
        if "(CIK" in nm and "(" in nm.split("(CIK")[0]:
            return nm.split("(")[0].strip()
    return ""


def cik_of(hit_source) -> "int | None":
    for c in (hit_source.get("ciks") or []):
        try:
            n = int(c)
        except (TypeError, ValueError):
            continue
        if n not in (876661, 1143313):        # NYSE LLC / NYSE American co-filers
            return n
    return None


def _norm(name: str) -> set:
    toks = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split()
    return {t for t in toks if t not in _SUFFIXES and len(t) > 1}


def name_matches(edgar_name: str, source_name: str) -> bool:
    """Is the thing priced today the same issuer EDGAR filed for?

    Deliberately permissive on wording and strict on emptiness: a missing name
    is NOT a match, because the whole point is to avoid splicing two companies
    on a reused symbol.
    """
    a, b = _norm(edgar_name), _norm(source_name)
    if not a or not b:
        return False
    if a & b:
        return len(a & b) >= 1 and (len(a & b) / min(len(a), len(b))) >= 0.5
    return False


def classify(exchange: str, has_bars: bool, matched: bool) -> str:
    """The four outcomes, in the order they must be checked."""
    if not has_bars:
        return "no-data"
    if not matched:
        return "identity-mismatch"
    e = (exchange or "").upper()
    if "OTC" in e or "PINK" in e:
        return "otc-continuation"
    if any(x in e for x in MAJOR):
        return "still-listed"
    return "unknown-venue"


# ═══════════════════════════════════════════════════════════ network shell

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


def enumerate_candidates(sess) -> dict:
    """CIK -> {tickers, issuer, form, date}. Earliest filing per CIK wins."""
    cands: dict = {}
    for form in ("25-NSE", "25"):
        st, r = _get(sess, FTS_URL, params={
            "q": '"delisting"', "forms": form,
            "startdt": WINDOW_START, "enddt": WINDOW_END})
        time.sleep(SLEEP_SEC)
        total = 0
        if st == 200:
            total = (((r.json().get("hits") or {}).get("total") or {}).get("value")) or 0
        print(f"  forms={form}: {total} filings")
        for page in range(MAX_PAGES):
            st2, r2 = _get(sess, FTS_URL, params={
                "q": '"delisting"', "forms": form, "from": page * 10,
                "startdt": WINDOW_START, "enddt": WINDOW_END})
            time.sleep(SLEEP_SEC)
            if st2 != 200:
                break
            hits = ((r2.json().get("hits") or {}).get("hits") or [])
            if not hits:
                break
            for h in hits:
                src = h.get("_source", {}) or {}
                cik = cik_of(src)
                tks = tickers_of(src.get("display_names"))
                if not cik or not tks:
                    continue
                d = str(src.get("file_date", ""))
                prev = cands.get(cik)
                if prev is None or (d and d < prev["date"]):
                    cands[cik] = {"tickers": tks, "issuer": issuer_of(src.get("display_names")),
                                  "form": form, "date": d}
            if (page + 1) % 20 == 0:
                print(f"    ...{form} page {page+1}, {len(cands)} issuers so far")
    return cands


def price_facts(sess, sym: str) -> dict:
    st, r = _get(sess, YAHOO_CHART.format(sym=sym),
                 params={"range": "5y", "interval": "1d"},
                 headers={"User-Agent": "Mozilla/5.0"})
    out = {"exchange": "", "name": "", "first": "", "last": "", "n": 0, "ts": []}
    if st != 200 or r is None:
        out["exchange"] = f"http:{st}"
        return out
    try:
        res = ((r.json().get("chart") or {}).get("result") or [None])[0] or {}
    except ValueError:
        return out
    meta = res.get("meta") or {}
    ts = [t for t in (res.get("timestamp") or []) if t]
    out["exchange"] = str(meta.get("fullExchangeName") or "")
    out["name"] = str(meta.get("longName") or meta.get("shortName") or "")
    out["n"] = len(ts)
    out["ts"] = ts
    if ts:
        out["first"] = pd.Timestamp(ts[0], unit="s").strftime("%Y-%m-%d")
        out["last"] = pd.Timestamp(ts[-1], unit="s").strftime("%Y-%m-%d")
    return out


def main() -> None:
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    print(f"[roster] window {WINDOW_START}..{WINDOW_END}; out={OUT_CSV}\n")

    print("=" * 74); print("1. CANDIDATES — Form 25 / 25-NSE, keyed by CIK"); print("=" * 74)
    cands = enumerate_candidates(sess)
    print(f"\n  {len(cands)} distinct issuers with an exchange departure filing")
    if not cands:
        print("[roster] nothing enumerated — refusing to write an empty roster"); sys.exit(1)
    items = sorted(cands.items(), key=lambda kv: kv[1]["date"])
    if LIMIT:
        items = items[:LIMIT]

    print("\n" + "=" * 74)
    print(f"2. CLASSIFY — what is each one's common stock doing now ({len(items)} names)")
    print("=" * 74)
    rows, status = [], collections.Counter()
    t0 = time.time()
    for k, (cik, c) in enumerate(items):
        sym = c["tickers"][0]
        pf = price_facts(sess, sym)
        time.sleep(SLEEP_YF)
        matched = name_matches(c["issuer"], pf["name"])
        st = classify(pf["exchange"], pf["n"] > 0, matched)
        status[st] += 1
        after = 0
        if c["date"] and pf["ts"]:
            cut = pd.Timestamp(c["date"]).timestamp()
            after = sum(1 for t in pf["ts"] if t > cut)
        rows.append({
            "cik": cik,
            "ticker": sym,
            "all_tickers": "|".join(c["tickers"]),
            "issuer_edgar": c["issuer"],
            "name_source": pf["name"],
            "name_match": bool(matched),
            "form": c["form"],
            "form25_date": c["date"],
            "exchange_now": pf["exchange"],
            "first_bar": pf["first"],
            "last_bar": pf["last"],
            "bars_total": pf["n"],
            "bars_after_form25": after,
            "status": st,
        })
        if (k + 1) % 50 == 0:
            print(f"  ...{k+1}/{len(items)} classified, {time.time()-t0:.0f}s")

    df = pd.DataFrame(rows).sort_values(["form25_date", "ticker"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print("\n" + "=" * 74)
    n = len(df)
    for s, c in status.most_common():
        print(f"  {c:>4}/{n}  {s}")
    usable = df[df["status"] == "otc-continuation"]
    print(f"\n  by year of departure: "
          f"{dict(sorted(collections.Counter(d[:4] for d in df['form25_date'] if d).items()))}")
    if len(usable):
        print(f"  otc-continuation names carry a median of "
              f"{int(usable['bars_after_form25'].median())} bars AFTER the filing — the price path")
        print(f"  a position held through the delisting would actually have experienced.")
    print(f"\n[roster] wrote {OUT_CSV} ({n} rows)")
    print("  NEXT: only `otc-continuation` rows are directly usable. `still-listed` are Form 25s")
    print("  on warrants/units or exchange transfers and must be EXCLUDED from the dead list.")
    print("  `identity-mismatch` is the ticker-reuse trap and needs a per-name decision — it is")
    print("  flagged, deliberately, rather than resolved by a rule that would sometimes splice")
    print("  two companies together. `no-data` is the residual hole a delisting-return")
    print("  assumption has to cover, and that assumption is a PRE-REGISTRATION choice.")
    print("[roster] no return computed, no universe rebuilt, K untouched.")


if __name__ == "__main__":
    main()
