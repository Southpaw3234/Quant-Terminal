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

Re-swept 2026-09-07 over the universe rebuilt on the ten-year roster, which
added 190 distinct names the three-year roster could not reach.
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
# 'annual' reproduces the v1 panel byte for byte. 'ttm' is the basis declared
# by value_ebit_ev_v2 on 2026-11-08. The DEFAULT STAYS 'annual' so a re-sweep
# of v1's frozen panel cannot silently change basis.
FLOWS = (os.environ.get("QT_FUND_FLOWS", "annual") or "annual").strip().lower()
if FLOWS not in ("annual", "ttm"):
    print(f"[fundamentals] QT_FUND_FLOWS must be 'annual' or 'ttm', got {FLOWS!r}")
    sys.exit(2)
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
            # `start` is captured for TTM construction, which identifies a
            # period by its DURATION rather than by `fp`. Many filers report
            # both a discrete quarter and a year-to-date figure for the same
            # period end, and only the duration separates them.
            out.append({"end": str(end), "start": str(r.get("start") or ""),
                        "val": float(val), "filed": str(filed),
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


# ────────────────────────────────────────── trailing twelve months (v2 flows)
#
# Declared by value_ebit_ev_v2 on 2026-11-08, BEFORE this code existed, so the
# construction could not be tuned toward a nicer answer.
#
# TTM = FY(n-1) + YTD(current) - YTD(same period, prior year).
#
# 🔑 That form needs only YEAR-TO-DATE figures and never a derived discrete
# quarter. Deriving Q4 as FY minus Q1+Q2+Q3 compounds three restatement
# histories into one number; this needs three facts, each as filed.
#
# Periods are identified by DURATION, not by the `fp` field. Filers routinely
# report a discrete quarter AND a year-to-date figure against the same period
# end, and `fp` says "Q3" for both. Duration is what separates them.

_ANNUAL_DAYS = (350, 380)
_YTD_DAYS = ((80, 100), (170, 195), (260, 285))     # Q1, Q2, Q3 year-to-date


def _duration_days(f) -> "int | None":
    if not f.get("start"):
        return None
    try:
        return (pd.Timestamp(f["end"]) - pd.Timestamp(f["start"])).days
    except (ValueError, TypeError):
        return None


def fiscal_year_starts(facts: list) -> list:
    """The `start` dates of every annual window present, for YTD alignment."""
    return [f["start"] for f in facts
            if f.get("start") and _duration_days(f) is not None
            and _ANNUAL_DAYS[0] <= _duration_days(f) <= _ANNUAL_DAYS[1]]


def _starts_a_fiscal_year(start: str, fy_starts: list) -> bool:
    """Does `start` fall on a fiscal-year boundary, to within a week?

    Compared modulo 365 so a YTD window in any year matches the fiscal
    calendar, and with ±7 days of slack for 52/53-week filers whose year
    start drifts.
    """
    try:
        s = pd.Timestamp(start)
    except (ValueError, TypeError):
        return False
    for fy in fy_starts:
        try:
            off = abs((s - pd.Timestamp(fy)).days) % 365
        except (ValueError, TypeError):
            continue
        if off <= 7 or off >= 358:
            return True
    return False


def period_bucket(f, fy_starts: "list | None" = None) -> "str | None":
    """'FY', 'Q1'..'Q3' for a year-to-date window, or None if unusable.

    ⚠️ DURATION ALONE IS NOT ENOUGH, and the test suite pins why: a DISCRETE
    second quarter is ~90 days and so is a Q1 YEAR-TO-DATE window. `fp` says
    "Q2" for the discrete one and cannot be trusted either. What separates
    them is the START — a year-to-date window begins on the fiscal year's
    first day, a discrete quarter begins mid-year.

    Pass `fy_starts` to enforce that. Without it the function reports the
    duration bucket only, which is useful for inspection and NOT safe for
    TTM arithmetic; `ttm_as_of` always passes it.

    None covers discrete quarters, 52/53-week stubs and anything whose length
    does not match a standard window. Unusable is NOT an error — it simply
    cannot take part in a TTM, and a name with no usable window drops out of
    that rebalance rather than falling back to a different basis.
    """
    days = _duration_days(f)
    if days is None:
        return None
    if _ANNUAL_DAYS[0] <= days <= _ANNUAL_DAYS[1]:
        return "FY"
    for i, (lo, hi) in enumerate(_YTD_DAYS, start=1):
        if lo <= days <= hi:
            if fy_starts is not None and not _starts_a_fiscal_year(
                    f.get("start", ""), fy_starts):
                return None
            return f"Q{i}"
    return None


def ttm_as_of(facts: list, asof) -> "dict | None":
    """Trailing twelve months as KNOWN on `asof`. None when it cannot be formed.

    Point-in-time rules are the same as `known_as_of`: only facts already
    FILED on or before the date are visible, and within a period the latest
    filing wins so a restatement counts only once it had actually happened.

    ⚠️ RETURNS None RATHER THAN FALLING BACK. value_ebit_ev_v2 declares that
    where a TTM cannot be formed the metric is NULL and the name drops out of
    the rebalance. Falling back to the annual figure would silently mix two
    bases in one series, which is the exact failure the change exists to
    avoid.
    """
    d = str(asof)[:10]
    # Fiscal-year boundaries are taken from the WHOLE fact set, not just the
    # visible slice: the calendar a company reports on is not itself a
    # point-in-time fact, and a name whose first 10-K is not yet filed has no
    # TTM anyway for want of FY(n-1).
    fy_starts = fiscal_year_starts(facts)
    visible = [f for f in facts
               if f.get("filed", "") <= d
               and period_bucket(f, fy_starts) is not None]
    if not visible:
        return None

    def latest(cands):
        return max(cands, key=lambda f: f["filed"]) if cands else None

    newest_end = max(f["end"] for f in visible)
    at_newest = [f for f in visible if f["end"] == newest_end]

    # A freshly filed annual IS the trailing twelve months. No arithmetic.
    fy_now = latest([f for f in at_newest if period_bucket(f, fy_starts) == "FY"])
    if fy_now is not None:
        return {"val": fy_now["val"], "end": fy_now["end"],
                "filed": fy_now["filed"], "basis": "FY"}

    cur = latest([f for f in at_newest if period_bucket(f, fy_starts) != "FY"])
    if cur is None:
        return None
    bucket = period_bucket(cur, fy_starts)
    e_cur, s_cur = pd.Timestamp(cur["end"]), pd.Timestamp(cur["start"])

    # The same year-to-date window one year earlier. ±45 days absorbs 52/53-week
    # fiscal calendars and period-end drift.
    prior = latest([f for f in visible if period_bucket(f, fy_starts) == bucket
                    and abs((pd.Timestamp(f["end"])
                             - (e_cur - pd.DateOffset(years=1))).days) <= 45])
    # The fiscal year that ended immediately before the current one began.
    fy_prev = latest([f for f in visible if period_bucket(f, fy_starts) == "FY"
                      and abs((pd.Timestamp(f["end"])
                               - (s_cur - pd.Timedelta(days=1))).days) <= 20])
    if prior is None or fy_prev is None:
        return None

    return {"val": fy_prev["val"] + cur["val"] - prior["val"],
            "end": cur["end"],
            # A TTM is only KNOWN once all three of its parts are filed.
            "filed": max(cur["filed"], prior["filed"], fy_prev["filed"]),
            "basis": f"FY+{bucket}-{bucket}"}


def snapshot(facts_by_metric: dict, asof, flows: str = "annual") -> dict:
    """One row: every metric as known on `asof`, plus how stale it was.

    `flows='ttm'` switches income-statement items to a trailing-twelve-month
    basis. Stocks are instantaneous and are unaffected either way.
    """
    row, filed_dates = {}, []
    for name in METRICS:
        kind = METRICS[name][1]
        facts = facts_by_metric.get(name, [])
        if flows == "ttm" and kind == "flow":
            f = ttm_as_of(facts, asof)
            if f is not None:
                row[f"{name}_basis"] = f["basis"]
        else:
            f = known_as_of(facts, asof)
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
    if FLOWS == "ttm":
        print(f"[fundamentals] flows on a TRAILING TWELVE MONTH basis "
              f"(FY(n-1) + YTD - YTD prior year), per value_ebit_ev_v2; a name "
              f"whose TTM cannot be formed is NULL for that month and does NOT "
              f"fall back to the annual figure")
    else:
        print(f"[fundamentals] flows taken from ANNUAL filings only")
    print(f"[fundamentals] every value keyed on `filed`, never `end`\n")

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
            # TTM needs the 10-Q windows as well, so flows are collected
            # unfiltered in that mode and the basis is decided in ttm_as_of.
            by_metric[name] = collect_facts(
                body, tags, unit,
                annual_only=(kind == "flow" and FLOWS == "annual"))
            if by_metric[name]:
                stats[f"has_{name}"] += 1
        for d in grid:
            if (tk, d.strftime("%Y-%m-%d")) not in pairs:
                continue                      # not in the universe that month
            snap = snapshot(by_metric, d, flows=FLOWS)
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
