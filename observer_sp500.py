#!/usr/bin/env python3
"""OBSERVER — S&P 500 fundamentals for the dashboard's company lookup.

Two passes, because the two halves change at different speeds:

    --sec      WEEKLY. The constituent list (Wikipedia's maintained table,
               which carries each company's SEC CIK and GICS sector), then
               every company's SEC XBRL facts read by qt/company_facts.py.
               -> fundamentals/sp500_sec.json
    --prices   NIGHTLY. One year of daily closes for every constituent and
               SPY, then qt/sp500.py's ratios and sector medians, joined to
               the latest --sec file. -> fundamentals/sp500.json

Collects and describes. No score, no rank, no rating, no fair value, and
nothing here touches the research programmes or the K budget.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from qt import agentio as io_
from qt import company_facts as cfx
from qt import sp500

OUT_DIR = os.environ.get("QT_SP500_DIR", "fundamentals")
WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
LIMIT = int(os.environ.get("QT_SP500_LIMIT", "0"))


def constituents(sess) -> pd.DataFrame:
    r = sess.get(WIKI, timeout=30)
    r.raise_for_status()
    t = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
    t = t.rename(columns={"Symbol": "ticker", "Security": "name", "GICS Sector": "sector",
                          "GICS Sub-Industry": "industry", "Headquarters Location": "hq",
                          "Date added": "added", "CIK": "cik", "Founded": "founded"})
    t["ticker"] = t["ticker"].astype(str).str.strip().str.upper()
    return t


def sec_pass() -> None:
    import requests
    now = io_.now_utc()
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    t = constituents(sess)
    if len(t) < 450:
        print(f"::error::sp500: only {len(t)} constituents parsed — refusing to publish a partial list")
        sys.exit(5)
    if LIMIT:
        t = t.head(LIMIT)
    out, failed, t0 = {}, {}, time.time()
    for i, r in enumerate(t.itertuples(index=False), 1):
        rec = {"name": r.name, "sector": r.sector, "industry": r.industry, "hq": r.hq,
               "added": str(r.added), "founded": str(r.founded), "cik": int(r.cik)}
        try:
            resp = sess.get(FACTS_URL.format(cik=int(r.cik)), timeout=60)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            rec["facts"] = cfx.extract(resp.json(), now[:10])
        except Exception as e:                              # noqa: BLE001
            failed[r.ticker] = f"{type(e).__name__}: {e}"[:160]
            rec["facts"] = None
        out[r.ticker] = rec
        time.sleep(0.12)
        if i % 100 == 0:
            print(f"  ...{i}/{len(t)}  {time.time() - t0:.0f}s  failed {len(failed)}")
    if len(failed) > 0.2 * len(t):
        print(f"::error::sp500: {len(failed)} of {len(t)} companies failed — refusing to publish")
        sys.exit(5)
    os.makedirs(OUT_DIR, exist_ok=True)
    io_.write_json(os.path.join(OUT_DIR, "sp500_sec.json"), {
        "generated_at": now, "constituents_source": WIKI, "n": len(out),
        "failed": failed, "companies": out,
        "source": "SEC XBRL companyfacts (data.sec.gov), read by qt/company_facts.py"})
    print(f"[sp500] SEC facts for {len(out) - len(failed)} of {len(out)} in {time.time() - t0:.0f}s")


def prices_pass() -> None:
    now = io_.now_utc()
    sec = io_.read_json(os.path.join(OUT_DIR, "sp500_sec.json"))
    if not sec or not sec.get("companies"):
        print("::error::sp500: no fundamentals/sp500_sec.json — run --sec first")
        sys.exit(5)
    comps = sec["companies"]
    yf_name = {t: t.replace(".", "-") for t in comps}
    import yfinance as yf
    raw = yf.download(sorted(set(yf_name.values())) + ["SPY"], period="13mo",
                      auto_adjust=True, progress=False, threads=True, group_by="column")
    close = raw["Close"]
    bench = close["SPY"] if "SPY" in close.columns else None
    rows, missing = {}, []
    for t, c in comps.items():
        col = yf_name[t]
        px = close[col] if col in close.columns else pd.Series(dtype=float)
        p = sp500.price_stats(px, bench) if px.dropna().size else {"price": None}
        if p.get("price") is None:
            missing.append(t)
        f = c.get("facts") or {}
        rows[t] = {k: c[k] for k in ("name", "sector", "industry", "hq", "added", "founded", "cik")}
        # The page needs value, period end and basis; tag and filing date
        # stay in sp500_sec.json for anyone tracing a figure.
        slim = {k: ({"val": v["val"], "end": v["end"], "basis": v["basis"]} if v else None)
                for k, v in f.items()}
        rows[t].update({"price": p, "facts": slim, "ratios": sp500.ratios(f, p),
                        "shares_class_shared": sum(1 for x in comps.values()
                                                   if x.get("cik") == c.get("cik")) > 1})
    med = sp500.sector_medians(rows)
    last = max((r["price"].get("last_session") or "" for r in rows.values()), default="")
    os.makedirs(OUT_DIR, exist_ok=True)
    io_.write_json(os.path.join(OUT_DIR, "sp500.json"), {
        "generated_at": now, "last_session": last, "sec_generated_at": sec["generated_at"],
        "n": len(rows), "missing_prices": missing, "sector_medians": med,
        "companies": rows, "is_not_a_recommendation": True})
    print(f"[sp500] {len(rows)} companies priced through {last}; {len(missing)} without a price")
    print("[sp500] described only. Nothing scored, K unchanged.")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sec", action="store_true")
    g.add_argument("--prices", action="store_true")
    a = ap.parse_args()
    sec_pass() if a.sec else prices_pass()


if __name__ == "__main__":
    main()
