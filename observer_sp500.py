#!/usr/bin/env python3
"""OBSERVER — company fundamentals for the dashboard's lookup.

Two universes, two passes each:

    --universe sp500     the S&P 500, from Wikipedia's maintained table
                         (ticker, GICS sector, CIK)
    --universe watched   the ~1,000 small companies the bots watch, from the
                         published daily snapshot; SEC IDs from OBSERVER's
                         cache, else EDGAR full-text search; sector from the
                         SEC industry code (qt/sic_sector.py, approximate)

    --sec      WEEKLY. Every company's SEC XBRL facts, read by
               qt/company_facts.py.        -> fundamentals/<universe>_sec.json
    --prices   NIGHTLY. A year of closes, qt/sp500.py's ratios and the
               medians of each sector WITHIN the universe, so a small company
               is compared with small companies.
                                           -> fundamentals/<universe>.json

Collects and describes. No score, no rank, no rating, no fair value, and
nothing here touches the research programmes or the K budget.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from qt import agentio as io_
from qt import company_facts as cfx
from qt import sic_sector
from qt import sp500

OUT_DIR = os.environ.get("QT_SP500_DIR", "fundamentals")
WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
RAW = "https://raw.githubusercontent.com/Southpaw3234/Quant-Terminal/"
WATCHED_URL = os.environ.get("QT_WATCHED_URL", RAW + "data/docs/daily.json")
CIK_CACHE_URL = os.environ.get("QT_CIK_CACHE_URL", RAW + "agent-output/observations/cik_cache.json")
UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
LIMIT = int(os.environ.get("QT_SP500_LIMIT", "0"))
MIN_COUNT = {"sp500": 450, "watched": 500}


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    return s


def _json(sess, url, params=None, tries=3):
    for k in range(tries):
        try:
            r = sess.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception:                                   # noqa: BLE001
            pass
        time.sleep(1.5 * (k + 1))
    return None


# ── the two lists ────────────────────────────────────────────────────────────

def sp500_list(sess) -> list:
    r = sess.get(WIKI, timeout=30)
    r.raise_for_status()
    t = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
    out = []
    for x in t.itertuples(index=False):
        out.append({"ticker": str(x.Symbol).strip().upper(), "name": x.Security,
                    "sector": x[2], "industry": x[3], "hq": x[4], "added": str(x[5]),
                    "founded": str(x[7]), "cik": int(x.CIK),
                    "sector_source": "GICS (S&P)"})
    return out


def resolve_cik(sess, tk: str):
    """EDGAR full-text search, confirming the ticker inside the display name."""
    for params in ({"q": f'"{tk}"', "entityName": tk}, {"entityName": tk}):
        body = _json(sess, FTS_URL, params) or {}
        time.sleep(0.12)
        for h in (body.get("hits") or {}).get("hits") or []:
            src = h.get("_source") or {}
            for dn, ck in zip(src.get("display_names") or [], src.get("ciks") or []):
                if f"({tk})" in dn or f"({tk}," in dn or f", {tk})" in dn or f", {tk}," in dn:
                    return int(ck)
    return None


def watched_list(sess) -> list:
    daily = _json(sess, WATCHED_URL) or {}
    tickers = [str(t).strip().upper() for t in
               ((daily.get("universe") or {}).get("eligible_tickers") or [])]
    cache = _json(sess, CIK_CACHE_URL) or {}
    known = {}
    for cik, v in cache.items():
        for t in v.get("tickers") or []:
            known.setdefault(str(t).upper(), int(cik))
    out, unresolved = [], []
    for t in tickers:
        cik = known.get(t) or resolve_cik(sess, t)
        if cik is None:
            unresolved.append(t)
            continue
        sub = _json(sess, SUBS_URL.format(cik=cik)) or {}
        time.sleep(0.12)
        addr = ((sub.get("addresses") or {}).get("business") or {})
        st = addr.get("stateOrCountry") or ""
        place = st if len(st) == 2 and st.isalpha() and not addr.get("stateOrCountryDescription")             else (addr.get("stateOrCountryDescription") or st)
        hq = ", ".join(x for x in (str(addr.get("city") or "").title(), place) if x)
        out.append({"ticker": t, "name": sub.get("name") or t,
                    "sector": sic_sector.sector(sub.get("sic")),
                    "industry": str(sub.get("sicDescription") or "").title() or "Not classified",
                    "hq": hq, "added": None, "founded": None,
                    "cik": cik, "sic": sub.get("sic"),
                    "sector_source": "SEC industry code (approximate)"})
    print(f"[watched] {len(tickers)} tickers; {len(out)} resolved to an SEC filer; "
          f"{len(unresolved)} not found")
    return out, unresolved, daily.get("asof_session")


# ── passes ───────────────────────────────────────────────────────────────────

def sec_pass(universe: str) -> None:
    now = io_.now_utc()
    sess = _session()
    unresolved, asof = [], None
    if universe == "sp500":
        lst = sp500_list(sess)
    else:
        lst, unresolved, asof = watched_list(sess)
    if len(lst) < MIN_COUNT[universe]:
        print(f"::error::{universe}: only {len(lst)} companies — refusing to publish a partial list")
        sys.exit(5)
    if LIMIT:
        lst = lst[:LIMIT]
    out, failed, t0 = {}, {}, time.time()
    for i, rec in enumerate(lst, 1):
        rec = dict(rec)
        blob = _json(sess, FACTS_URL.format(cik=rec["cik"]))
        time.sleep(0.12)
        if blob is None:
            failed[rec["ticker"]] = "no XBRL company facts at the SEC"
            rec["facts"], rec["reporting"] = None, {"standard": "none", "currencies": []}
        else:
            try:
                rec["facts"] = cfx.extract(blob, now[:10])
                rec["reporting"] = cfx.reporting(blob)
            except Exception as e:                          # noqa: BLE001
                failed[rec["ticker"]] = f"{type(e).__name__}: {e}"[:160]
                rec["facts"], rec["reporting"] = None, {"standard": "none", "currencies": []}
        out[rec.pop("ticker")] = rec
        if i % 100 == 0:
            print(f"  ...{i}/{len(lst)}  {time.time() - t0:.0f}s  failed {len(failed)}")
    if len(failed) > 0.25 * len(lst):
        print(f"::error::{universe}: {len(failed)} of {len(lst)} failed — refusing to publish")
        sys.exit(5)
    os.makedirs(OUT_DIR, exist_ok=True)
    io_.write_json(os.path.join(OUT_DIR, f"{universe}_sec.json"), {
        "generated_at": now, "universe": universe, "n": len(out), "universe_asof": asof,
        "list_source": WIKI if universe == "sp500" else WATCHED_URL,
        "unresolved": unresolved, "failed": failed, "companies": out,
        "source": "SEC XBRL companyfacts (data.sec.gov), read by qt/company_facts.py"})
    print(f"[{universe}] SEC facts for {len(out) - len(failed)} of {len(out)} "
          f"in {time.time() - t0:.0f}s")


def prices_pass(universe: str) -> None:
    now = io_.now_utc()
    sec = io_.read_json(os.path.join(OUT_DIR, f"{universe}_sec.json"))
    if not sec or not sec.get("companies"):
        print(f"::error::{universe}: no fundamentals/{universe}_sec.json — run --sec first")
        sys.exit(5)
    comps = sec["companies"]
    yf_name = {t: t.replace(".", "-") for t in comps}
    import yfinance as yf
    raw = yf.download(sorted(set(yf_name.values())) + ["SPY"], period="13mo",
                      auto_adjust=True, progress=False, threads=True, group_by="column")
    close = raw["Close"]
    # Yahoo throttles back-to-back bulk downloads: run right after the other
    # universe, 193 of 503 came back empty. Retry the gaps in small batches.
    for attempt in range(3):
        gaps = [c for c in sorted(set(yf_name.values())) + ["SPY"]
                if c not in close.columns or close[c].dropna().empty]
        if not gaps:
            break
        print(f"[{universe}] retry {attempt + 1}: {len(gaps)} without a price")
        time.sleep(10 * (attempt + 1))
        for i in range(0, len(gaps), 40):
            got = yf.download(gaps[i:i + 40], period="13mo", auto_adjust=True,
                              progress=False, threads=False, group_by="column")
            if got is not None and len(got):
                gc = got["Close"]
                if isinstance(gc, pd.Series):
                    gc = gc.to_frame(gaps[i])
                for col in gc.columns:
                    if gc[col].dropna().size:
                        close = close.drop(columns=[col], errors="ignore").join(gc[[col]], how="outer")
            time.sleep(2)
    bench = close["SPY"] if "SPY" in close.columns else None
    rows, missing = {}, []
    for t, c in comps.items():
        col = yf_name[t]
        px = close[col] if col in close.columns else pd.Series(dtype=float)
        p = sp500.price_stats(px, bench) if px.dropna().size else {"price": None}
        if p.get("price") is None:
            missing.append(t)
        f = c.get("facts") or {}
        rows[t] = {k: c.get(k) for k in ("name", "sector", "industry", "hq", "added",
                                         "founded", "cik", "sector_source", "reporting")}
        # The page needs value, period end and basis; tag and filing date
        # stay in <universe>_sec.json for anyone tracing a figure.
        slim = {k: ({"val": v["val"], "end": v["end"], "basis": v["basis"]} if v else None)
                for k, v in f.items()}
        rows[t].update({"price": p, "facts": slim, "ratios": sp500.ratios(f, p),
                        "shares_class_shared": sum(1 for x in comps.values()
                                                   if x.get("cik") == c.get("cik")) > 1})
    med = sp500.sector_medians(rows)
    last = max((r["price"].get("last_session") or "" for r in rows.values()), default="")
    os.makedirs(OUT_DIR, exist_ok=True)
    io_.write_json(os.path.join(OUT_DIR, f"{universe}.json"), {
        "generated_at": now, "universe": universe, "last_session": last,
        "sec_generated_at": sec["generated_at"], "n": len(rows),
        "unresolved": sec.get("unresolved") or [], "missing_prices": missing,
        "sector_medians": med, "companies": rows, "is_not_a_recommendation": True})
    print(f"[{universe}] {len(rows)} companies priced through {last}; "
          f"{len(missing)} without a price")
    print(f"[{universe}] described only. Nothing scored, K unchanged.")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sec", action="store_true")
    g.add_argument("--prices", action="store_true")
    ap.add_argument("--universe", choices=("sp500", "watched"), default="sp500")
    a = ap.parse_args()
    sec_pass(a.universe) if a.sec else prices_pass(a.universe)


if __name__ == "__main__":
    main()
