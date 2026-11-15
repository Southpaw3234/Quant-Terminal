#!/usr/bin/env python3
"""Pure-logic validation for OBSERVER's macro and news collector. No network.

The class of wrongness this file exists to prevent: **an old number shown as
today's.** A macro series is the easiest thing in this system to show stale
-- CPI is monthly, so a feed that stopped in March still looks reasonable in
July. Each series therefore carries its own age and loses its value, not
just gains a warning, when it is past the cadence it is published on.
"""

from __future__ import annotations

import ast
import inspect
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import observer_macro as om

PASS, FAIL = [], []
NOW = "2026-11-14T03:00:00+00:00"
NOW_TS = 1794625200.0          # 2026-11-14T03:00:00Z


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def _imports(mod) -> set:
    out = set()
    for n in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            out.update(f"{n.module}.{a.name}" for a in n.names)
    return out


def _identifiers(mod) -> set:
    out = set()
    for n in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            out.add(n.name.lower())
        elif isinstance(n, ast.Name):
            out.add(n.id.lower())
        elif isinstance(n, ast.arg):
            out.add(n.arg.lower())
    return out


SPEC_D = {"id": "DGS10", "title": "10y", "units": "%", "max_age_days": 7}
SPEC_M = {"id": "CPIAUCSL", "title": "CPI", "units": "index", "max_age_days": 75}


def test_capabilities_absent():
    print("\nthe macro collector cannot measure, score or trade")
    imported = _imports(om)
    for m in ("qt.referee", "qt.wrc", "qt.calendar_time", "qt.prices",
              "alpaca", "alpaca_trade_api", "yfinance"):
        check(f"does-not-import-{m}", m not in imported)
    ids = _identifiers(om)
    for banned in ("score", "rank", "predict", "recommend", "signal",
                   "sentiment", "importance"):
        hit = sorted(i for i in ids if banned in i)
        check(f"binds-no-{banned}", not hit,
              "headlines are collected, not curated"
              + (f" — found {hit}" if hit else ""))


def test_parsing():
    print("\nFRED parsing")
    j = {"observations": [{"date": "2026-11-12", "value": "4.31"},
                          {"date": "2026-11-11", "value": "."},
                          {"date": "2026-11-10", "value": "4.28"}]}
    obs = om.parse_fred_json(j)
    check("json-sorted-oldest-first-and-dot-dropped",
          obs == [("2026-11-10", 4.28), ("2026-11-12", 4.31)],
          "FRED writes a holiday as '.', and a '.' is not a zero")
    c = om.parse_fred_csv("observation_date,DGS10\n2026-11-10,4.28\n"
                          "2026-11-11,\n2026-11-12,4.31\n")
    check("csv-agrees-with-json", c == obs)
    check("empty-payloads-yield-nothing",
          om.parse_fred_json({}) == [] and om.parse_fred_csv("") == [])


def test_series_freshness():
    print("\nevery series states its own age")
    obs = [("2025-11-12", 4.00), ("2026-11-10", 4.28), ("2026-11-12", 4.31)]
    s = om.series_summary(SPEC_D, obs, NOW, "fred-api")
    check("fresh-series-carries-value-and-date",
          s["status"] == "fresh" and s["value"] == 4.31
          and s["as_of"] == "2026-11-12")
    check("change-is-against-the-previous-observation",
          abs(s["change"] - 0.03) < 1e-9 and s["previous_as_of"] == "2026-11-10")
    check("year-ago-is-the-last-value-on-or-before",
          s["year_ago"] == 4.00 and abs(s["change_1y"] - 0.31) < 1e-9)
    check("series-links-to-its-source", "fred.stlouisfed.org/series/DGS10" in s["url"])

    old = [("2026-09-01", 4.10), ("2026-10-01", 4.20)]
    st = om.series_summary(SPEC_D, old, NOW, "fred-api")
    check("a-daily-series-six-weeks-old-is-STALE", st["status"] == "stale")
    check("🔑-a-stale-series-LOSES-its-value", st["value"] is None
          and st["previous"] is None and st["year_ago"] is None,
          "showing an old number beside a warning is still showing the old "
          "number")
    check("a-stale-series-keeps-its-date-so-the-age-is-visible",
          st["as_of"] == "2026-10-01" and st["age_days"] > 40)

    sm = om.series_summary(SPEC_M, old, NOW, "fred-api")
    check("the-same-age-is-FRESH-for-a-monthly-series",
          sm["status"] == "fresh" and sm["value"] == 4.20,
          "staleness is judged against the cadence the series is published "
          "on, declared per series")

    e = om.series_summary(SPEC_D, [], NOW, "fred-api")
    check("no-observations-is-missing-not-zero",
          e["status"] == "missing" and e["value"] is None)
    u = om.series_summary(SPEC_D, [("garbage", 1.0)], NOW, "x")
    check("an-undated-value-is-not-shown",
          u["status"] == "undated" and u["value"] is None)
    check("every-declared-series-has-a-cadence",
          all(x["max_age_days"] > 0 and x["id"] and x["title"]
              for x in om.SERIES) and len(om.SERIES) == 10)


def test_news():
    print("\nheadlines are collected, not curated")
    items = [
        {"id": 1, "headline": "old", "url": "u1", "source": "A",
         "datetime": NOW_TS - 9 * 86400},
        {"id": 2, "headline": "newest", "url": "u2", "source": "A",
         "datetime": NOW_TS - 60},
        {"id": 3, "headline": "middle", "url": "u3", "source": "B",
         "datetime": NOW_TS - 3600},
        {"id": 4, "headline": "dupe", "url": "u3", "source": "B",
         "datetime": NOW_TS - 7200},
        {"id": 5, "headline": "", "url": "u5", "datetime": NOW_TS},
        {"id": 6, "headline": "no time", "url": "u6"},
        "garbage",
    ]
    n = om.shape_news(items, 10, NOW_TS)
    check("newest-first", [i["headline"] for i in n["items"]]
          == ["newest", "middle"])
    check("older-than-the-window-is-out", n["total"] == 2)
    check("duplicate-urls-collapse-and-junk-is-skipped", n["total"] == 2)
    check("every-headline-links-to-its-source",
          all(i["url"] and i["published_at"] for i in n["items"]))
    capped = om.shape_news(items, 1, NOW_TS)
    check("the-cap-is-reported-with-the-true-total",
          capped["shown"] == 1 and capped["total"] == 2 and capped["truncated"],
          "a capped list must say it is capped")
    check("empty-is-empty", om.shape_news([], 5, NOW_TS)["total"] == 0)

    old_key = om.FINNHUB_KEY
    try:
        om.FINNHUB_KEY = ""
        r = om.collect_news(None, ["GRPN"], NOW_TS)
    finally:
        om.FINNHUB_KEY = old_key
    check("no-key-is-not_collected-with-a-reason",
          r["status"] == "not_collected" and "FINNHUB_API_KEY" in r["reason"]
          and r["market"] is None,
          "never an empty list that reads as 'no news today'")


def test_digest_and_refusal():
    print("\nthe digest shows dark series as dark")
    doc = {"day": "2026-11-14",
           "series": [om.series_summary(SPEC_D, [("2026-11-12", 4.31)], NOW, "x"),
                      om.series_summary(SPEC_D, [("2026-09-01", 4.1)], NOW, "x")],
           "news": {"status": "not_collected", "reason": "no key"}}
    d = om.digest(doc)
    check("fresh-value-is-shown", "4.31" in d)
    check("stale-value-is-NOT-shown", "4.1 " not in d and "stale" in d)
    check("uncollected-news-is-named", "not collected" in d)
    src = inspect.getsource(om.main)
    check("an-all-dark-run-refuses-to-publish", "sys.exit(5)" in src,
          "ten missing series and no headlines is an outage, not a quiet day")


def main():
    print("=" * 70)
    print("observer_macro — pure logic (no network)")
    print("=" * 70)
    test_capabilities_absent()
    test_parsing()
    test_series_freshness()
    test_news()
    test_digest_and_refusal()
    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing forecast.")


if __name__ == "__main__":
    main()
