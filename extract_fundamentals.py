#!/usr/bin/env python3
"""Point-in-time fundamentals from SEC XBRL — the data layer for a value model.

A value screen asks "what is this company worth relative to its price". Every
number on the left of that comparison arrives on a DELAY, and getting the
delay wrong is the single most common way a fundamental backtest invents
returns that never existed.

    A 10-K for fiscal 2024 is FILED in March 2025.
    Using it to rank stocks in January 2025 is not a small error.
    It is knowing the answer before the question.

SEC XBRL makes this avoidable, and it is why this layer exists at all. Every
fact carries `filed` alongside `end`:

    {"end":"2011-08-02", "val":18400740, "filed":"2011-08-05",
     "form":"10-Q", "fp":"Q2", "accn":"0001193125-11-212269"}

`end` is the fiscal period. `filed` is the day the market could first see it.
This module keys on `filed`, never on `end`.

RESTATEMENTS ARE THE SECOND TRAP. The same period is reported more than once:
an original figure, then a revision. A backtest that takes today's value for a
2019 period is using a number nobody had in 2019. `known_as_of` therefore
picks the LATEST FILING AT OR BEFORE the as-of date, which reproduces what was
actually on the tape then, restatement history and all.

FLOWS VERSUS STOCKS. Balance-sheet items (assets, equity, cash) are
instantaneous and are taken as reported. Income-statement items (revenue, net
income) are periodic, and mixing a quarter into a series of years silently
divides a ratio by four. So flows are taken from ANNUAL filings only --
conservative, unambiguous, and up to fifteen months stale by construction.
Trailing-twelve-month construction needs Q4 derived as FY minus Q1 Q2 Q3 and
is a later refinement, not a default.

This computes NO ratio and selects NO stock. Which ratios, how combined, and
how weighted are specification choices with enormous freedom in them, and
they belong in a pre-registration rather than in an extractor. See
docs/V29_VALUE_MODEL.md.

Writes data/fundamentals/fundamentals_pit.csv. No return computed, no K.
"""
from __future__ import annotations

import collections
import os
import sys
import time

import pandas as pd

UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
PANEL = os.environ.get("QT_PIT_PANEL", "data/universe/v27_universe_pit.csv")
OUT_CSV = os.environ.get("QT_FUND_OUT", "data/fundamentals/fundamentals_pit.csv")
START = os.environ.get("QT_FUND_START", "2023-09-01")
END = os.environ.get("QT_FUND_END", "2026-09-05")
LIMIT = int(os.environ.get("QT_FUND_LIMIT", "0"))
if LIMIT:
    OUT_CSV = OUT_CSV.replace(".csv", f"_SMOKE{LIMIT}.csv")
SLEEP = 0.12
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"

# tag -> (unit, kind, [us-gaap tags in fallback order])
# Fallback chains are not optional: the revenue tag changed with ASC 606, so a
# single-tag lookup silently returns nothing for whichever half of the
# universe uses the other one.
METRICS = {
    "assets":        ("USD", "stock", ["Assets"]),
    "liabilities":   ("USD", "stock", ["Liabilities"]),
    "equity":        ("USD", "stock", ["StockholdersEquity",
                                       "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    "cash":          ("USD", "stock", ["CashAndCashEquivalentsAtCarryingValue",
                                       "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    "debt_lt":       ("USD", "stock", ["LongTermDebtNoncurrent", "LongTermDebt"]),
    "revenue":       ("USD", "flow",  ["RevenueFromContractWithCustomerExcludingAssessedTax",
                                       "Revenues", "SalesRevenueNet"]),
    "net_income":    ("USD", "flow",  ["NetIncomeLoss"]),
    "op_income":     ("USD", "flow",  ["OperatingIncomeLoss"]),
    "shares":        ("shares", "stock", ["CommonStockSharesOutstanding",
                                          "WeightedAverageNumberOfDilutedSharesOutstanding"]),
}


# ═══════════════════════════════════════════════════ pure logic (no network)

def month_ends(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="ME")


def collect_facts(companyfacts: dict, tags: list, unit: str, annual_only: bool) -> list:
    """Flatten the XBRL blob into [{end, val, filed, form, fp}] for one metric.

    Walks the fallback chain in order and takes the FIRST tag that yields
    anything, rather than merging them: two tags for the same concept can
    disagree, and a merged series would be neither company's books.
    """
    gaap = (companyfacts.get("facts", {}) or {}).get("us-gaap", {}) or {}
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        rows = (node.get("units", {}) or {}).get(unit, []) or []
        out = []
        for r in rows:
            end, val, filed = r.get("end"), r.get("val"), r.get("filed")
            if not end or not filed or val is None:
                continue
            form, fp = str(r.get("form", "")), str(r.get("fp", ""))
            if annual_only and not (form.startswith("10-K") and fp == "FY"):
                continue
            out.append({"end": str(end), "val": float(val), "filed": str(filed),
                        "form": form, "fp": fp, "tag": tag})
        if out:
            return out
    return []


def known_as_of(facts: list, asof) -> "dict | None":
    """The value as it stood on `asof` — never a number filed afterwards.

    Two rules, and both matter:
      1. Only facts already FILED on or before the date are visible.
      2. Among those, take the most recent fiscal period; and within that
         period take the LATEST filing, which is the restated figure IF the
         restatement had already happened by then, and the original if it
         had not.
    """
    d = str(asof)[:10]
    visible = [f for f in facts if f["filed"] <= d]
    if not visible:
        return None
    max_end = max(f["end"] for f in visible)
    same = [f for f in visible if f["end"] == max_end]
    return max(same, key=lambda f: f["filed"])


def snapshot(facts_by_metric: dict, asof) -> dict:
    """One row: every metric as known on `asof`, plus how stale it was."""
    row, filed_dates = {}, []
    for name in METRICS:
        f = known_as_of(facts_by_metric.get(name, []), asof)
        if f is None:
            row[name] = None
            continue
        row[name] = f["val"]
        row[f"{name}_end"] = f["end"]
        filed_dates.append(f["filed"])
    if filed_dates:
        newest = max(filed_dates)
        row["latest_filed"] = newest
        row["staleness_days"] = (pd.Timestamp(str(asof)[:10]) - pd.Timestamp(newest)).days
    return row


# ═══════════════════════════════════════════════════════════ network shell

def _get(sess, url, params=None):
    import requests
    for attempt in range(3):
        try:
            r = sess.get(url, params=params, timeout=45)
        except requests.RequestException:
            time.sleep(1.2 * (attempt + 1)); continue
        if r.status_code == 200:
            try:
                return "ok", r.json()
            except ValueError:
                return "err", None
        if r.status_code == 404:
            return "404", None
        time.sleep(1.2 * (attempt + 1))
    return "err", None


def resolve_cik(sess, tk: str):
    st, body = _get(sess, FTS_URL, params={
        "q": '"Item 2.02"', "forms": "8-K", "entityName": tk,
        "startdt": "2023-09-01", "enddt": "2026-09-05"})
    time.sleep(SLEEP)
    if st != "ok":
        return None
    for h in ((body.get("hits", {}) or {}).get("hits", []) or []):
        src = h.get("_source", {}) or {}
        names = " ".join(src.get("display_names", []) or [])
        if f"({tk})" in names or f"({tk}," in names or f", {tk})" in names:
            ciks = src.get("ciks") or []
            if ciks:
                return int(ciks[0])
    return None


def main() -> None:
    import requests
    if not os.path.exists(PANEL):
        print(f"[fundamentals] missing {PANEL} — run the point-in-time universe first")
        sys.exit(1)
    panel = pd.read_csv(PANEL)
    pairs = {(str(t).upper(), str(d)) for t, d in zip(panel["ticker"], panel["date"])}
    known_cik = {}
    for t, c in zip(panel["ticker"], panel.get("cik", [])):
        if pd.notna(c) and str(c).strip() not in ("", "nan"):
            try:
                known_cik[str(t).upper()] = int(float(c))
            except ValueError:
                pass
    tickers = sorted({str(t).upper() for t in panel["ticker"]})
    if LIMIT:
        tickers = tickers[:LIMIT]
    grid = month_ends(START, END)
    print(f"[fundamentals] {len(tickers)} names from {PANEL}; {len(grid)} month-ends "
          f"{grid[0].date()}..{grid[-1].date()}")
    print(f"[fundamentals] metrics: {', '.join(METRICS)}")
    print(f"[fundamentals] flows taken from ANNUAL filings only; every value keyed "
          f"on `filed`, never `end`\n")

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})

    rows, stats = [], collections.Counter()
    t0 = time.time()
    for k, tk in enumerate(tickers):
        cik = known_cik.get(tk) or resolve_cik(sess, tk)
        if cik is None:
            stats["no_cik"] += 1
            continue
        st, body = _get(sess, FACTS_URL.format(cik=cik))
        time.sleep(SLEEP)
        if st != "ok" or not body:
            stats["no_facts"] += 1
            continue
        stats["with_facts"] += 1
        by_metric = {}
        for name, (unit, kind, tags) in METRICS.items():
            by_metric[name] = collect_facts(body, tags, unit, annual_only=(kind == "flow"))
            if by_metric[name]:
                stats[f"has_{name}"] += 1
        for d in grid:
            if (tk, d.strftime("%Y-%m-%d")) not in pairs:
                continue                      # not in the universe that month
            snap = snapshot(by_metric, d)
            if snap.get("latest_filed") is None:
                stats["month_no_data"] += 1
                continue
            snap.update({"ticker": tk, "cik": cik, "asof": d.strftime("%Y-%m-%d")})
            rows.append(snap)
            stats["month_rows"] += 1
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(tickers)} names, {len(rows)} rows, {time.time()-t0:.0f}s")

    if not rows:
        print("[fundamentals] nothing collected — refusing to write"); sys.exit(1)
    front = ["ticker", "cik", "asof", "latest_filed", "staleness_days"]
    df = pd.DataFrame(rows)
    cols = front + [c for c in df.columns if c not in front]
    df = df[cols].sort_values(["asof", "ticker"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print("\n" + "=" * 74)
    print(f"  names with facts      {stats['with_facts']}/{len(tickers)}")
    print(f"  no CIK / no facts     {stats['no_cik']} / {stats['no_facts']}")
    print(f"  ticker-months written {stats['month_rows']:,}")
    print(f"  ticker-months dropped {stats['month_no_data']:,}  (nothing filed yet on that date)")
    print(f"\n  COVERAGE PER METRIC (names with any history):")
    for name in METRICS:
        n = stats[f"has_{name}"]
        print(f"    {name:<14} {n:>4}/{stats['with_facts']}  "
              f"({100*n/max(1,stats['with_facts']):.0f}%)")
    if "staleness_days" in df:
        s = pd.to_numeric(df["staleness_days"], errors="coerce").dropna()
        print(f"\n  STALENESS of the newest visible filing, in days:")
        print(f"    p25 {s.quantile(.25):.0f}   median {s.median():.0f}   "
              f"p75 {s.quantile(.75):.0f}   p95 {s.quantile(.95):.0f}   max {s.max():.0f}")
        print(f"    This is the real information lag a value screen operates under, and it")
        print(f"    is the number a backtest keyed on `end` would have silently erased.")
    print(f"\n[fundamentals] wrote {OUT_CSV} ({len(df):,} rows)")
    print("  No ratio computed and no stock selected. Which ratios, how combined and how")
    print("  weighted are SPECIFICATION choices — see docs/V29_VALUE_MODEL.md.")
    print("[fundamentals] K untouched.")


if __name__ == "__main__":
    main()
