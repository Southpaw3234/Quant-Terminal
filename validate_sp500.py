#!/usr/bin/env python3
"""Pure-logic checks for the S&P 500 lookup: qt/company_facts.py and
qt/sp500.py. Synthetic filings and prices, no network.

What this guards:
  1. a stale figure shown as current (NVIDIA's 2022 revenue, JPMorgan's
     2018 cash -- what "first tag with data" produced on the first try)
  2. a ratio that misleads: P/E on a loss, P/B on negative equity, growth
     across periods that aren't a year apart
  3. "not reported" turned into a zero (a 0% dividend yield nobody filed)
  4. a verdict sneaking into the output
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from qt import company_facts as cfx
from qt import sp500

PASS, FAIL = [], []


def check(name, ok, why=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def fact(start, end, val, filed, form="10-K", fp="FY"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form, "fp": fp}


def cf(tags: dict, ns="us-gaap", unit="USD"):
    """{tag: [facts]} -> a companyfacts-shaped blob."""
    return {"facts": {ns: {t: {"units": {unit: rows}} for t, rows in tags.items()}}}


def test_freshest_tag_wins():
    print("\nthe freshest figure wins, and a stale one is not shown")
    old = [fact("2020-02-01", "2021-01-31", 16.7e9, "2021-03-15"),
           fact("2021-02-01", "2022-01-30", 26.9e9, "2022-03-18")]
    new = [fact("2024-01-29", "2025-01-26", 130.5e9, "2025-02-26"),
           fact("2025-01-27", "2026-01-25", 210.0e9, "2026-02-25")]
    blob = cf({"RevenueFromContractWithCustomerExcludingAssessedTax": old, "Revenues": new})
    v = cfx.value(blob, "revenue", "2026-11-27")
    check("🔑-a-later-tag-beats-an-earlier-stale-one",
          v and v["end"] == "2026-01-25" and v["tag"] == "Revenues",
          "NVIDIA's revenue tag changed in 2022; the first try showed 2022 revenue")
    only_old = cf({"RevenueFromContractWithCustomerExcludingAssessedTax": old})
    check("🔑-a-figure-over-15-months-old-is-not-shown",
          cfx.value(only_old, "revenue", "2026-11-27") is None)
    check("a-future-filing-is-invisible",
          cfx.value(blob, "revenue", "2025-03-01")["end"] == "2025-01-26")
    bs = cf({"Assets": [fact("", "2026-09-30", 5e12, "2026-11-01", "10-Q", "Q3")]})
    check("a-balance-figure-is-as-of-its-date",
          cfx.value(bs, "assets", "2026-11-27")["basis"] == "as of")


def test_ttm_and_growth():
    print("\ntrailing twelve months, and growth only a year apart")
    rows = [fact("2024-01-01", "2024-12-31", 100.0, "2025-02-15"),
            fact("2025-01-01", "2025-09-30", 90.0, "2025-11-01", "10-Q", "Q3"),
            fact("2024-01-01", "2024-09-30", 70.0, "2025-11-01", "10-Q", "Q3"),
            fact("2023-01-01", "2023-12-31", 80.0, "2024-02-15"),
            fact("2023-01-01", "2023-09-30", 55.0, "2024-11-01", "10-Q", "Q3"),
            fact("2024-01-01", "2024-09-30", 70.0, "2024-11-01", "10-Q", "Q3")]
    blob = cf({"Revenues": rows})
    v = cfx.value(blob, "revenue", "2025-12-01")
    check("ttm-is-FY-plus-YTD-minus-prior-YTD",
          v and v["val"] == 120.0 and v["basis"] == "trailing 12 months", f"{v}")
    a = cfx.year_ago(blob, "revenue", v)
    check("year-ago-is-the-period-a-year-earlier", a and a["end"] == "2024-09-30", f"{a}")
    check("year-ago-is-None-without-a-match",
          cfx.year_ago(blob, "revenue", {"end": "2030-01-01"}) is None)


def series(n=260, drift=0.0005, seed=1, start=100.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-11-01", periods=n)
    return pd.Series(start * np.exp(np.cumsum(drift + 0.01 * rng.standard_normal(n))), index=idx)


def test_ratios():
    print("\nratios are blank where they would mislead")
    p = {"price": 50.0}
    f = {"shares": {"val": 1e9}, "revenue": {"val": 20e9}, "net_income": {"val": -1e9},
         "equity": {"val": -5e9}, "eps": {"val": -1.0}, "op_cash_flow": {"val": 3e9},
         "capex": {"val": 1e9}, "debt_long": {"val": 4e9}, "cash": {"val": 1e9},
         "revenue_year_ago": {"val": 16e9}}
    r = sp500.ratios(f, p)
    check("🔑-no-PE-on-a-loss", r["pe"] is None)
    check("🔑-no-PB-on-negative-equity", r["pb"] is None and r["roe"] is None
          and r["debt_to_equity"] is None)
    check("price-to-sales-still-works", abs(r["ps"] - 2.5) < 1e-9)
    check("growth-is-a-year-on-year-ratio", abs(r["revenue_growth"] - 0.25) < 1e-9)
    check("free-cash-flow-is-ocf-minus-capex", r["fcf"] == 2e9 and abs(r["fcf_yield"] - 0.04) < 1e-9)
    check("net-cash-is-cash-minus-debt", r["net_cash"] == -3e9)
    check("🔑-no-dividend-figure-is-None-not-zero", r["dividend_yield"] is None,
          "a 0% yield would claim something nobody filed")
    check("no-shares-means-no-market-value", sp500.ratios({}, p)["market_cap"] is None)
    ps = sp500.price_stats(series(), series(seed=2))
    check("price-stats-have-their-figures",
          all(k in ps for k in ("price", "high_52w", "low_52w", "return_1y", "vol_1y", "beta_1y")))
    z = series(); z.iloc[100] = 0.0
    zs = sp500.price_stats(z, None)
    check("a-zero-close-does-not-poison-volatility", zs.get("vol_1y") is not None
          and np.isfinite(zs["vol_1y"]), str(zs.get("vol_1y")))
    check("a-short-history-gets-no-statistics", "vol_1y" not in sp500.price_stats(series(40), None))
    s = series()
    check("beta-of-a-stock-against-itself-is-1",
          abs(sp500.price_stats(s, s)["beta_1y"] - 1.0) < 1e-9)


def test_medians_and_verdicts():
    print("\nsector medians, and no verdict anywhere")
    rows = {f"T{i}": {"sector": "Tech", "ratios": {"pe": float(i), "ps": None}} for i in range(1, 8)}
    rows["Z"] = {"sector": "Tiny", "ratios": {"pe": 5.0}}
    med = sp500.sector_medians(rows)
    check("median-over-the-sector", med["Tech"]["pe"] == 4.0 and med["Tech"]["n"] == 7)
    check("missing-values-are-skipped-not-zeroed", med["Tech"]["ps"] is None)
    check("fewer-than-five-peers-gives-no-median", med["Tiny"]["pe"] is None)
    banned = ("buy", "sell", "hold", "rating", "score", "rank", "target", "fair_value", "signal")
    keys = set(sp500.ratios({}, {"price": 1.0})) | set(sp500.RATIOS)
    check("🔑-no-key-is-a-verdict", not any(b in k for k in keys for b in banned), "")


def test_small_caps():
    print("\nthe watched small caps: sector from SIC, foreign filers handled")
    from qt import sic_sector as sic
    cases = {2834: "Health Care", 1311: "Energy", 1040: "Materials", 6022: "Financials",
             6798: "Real Estate", 7372: "Information Technology", 3841: "Health Care",
             3711: "Consumer Discretionary", 4911: "Utilities", 4813: "Communication Services",
             2840: "Consumer Staples", 5411: "Consumer Staples", 3721: "Industrials",
             8731: "Health Care", 9995: "Other", 2711: "Communication Services",
             2780: "Industrials"}
    got = {k: sic.sector(k) for k in cases}
    check("🔑-sic-codes-map-to-the-expected-sector", got == cases,
          str({k: v for k, v in got.items() if v != cases[k]}))
    check("narrower-ranges-win", sic.sector(2836) == "Health Care" and sic.sector(2821) == "Materials")
    check("a-missing-code-is-Other-not-a-guess", sic.sector(None) == "Other" and sic.sector("") == "Other")

    ifrs_usd = {"facts": {"ifrs-full": {"Revenue": {"units": {"USD": [
        fact("2025-01-01", "2025-12-31", 50e6, "2026-04-01", "20-F", "FY")]}}}}}
    v = cfx.value(ifrs_usd, "revenue", "2026-11-27")
    check("ifrs-figures-in-usd-are-read", v and v["val"] == 50e6 and v["tag"] == "ifrs-full:Revenue")
    ifrs_chf = {"facts": {"ifrs-full": {"Revenue": {"units": {"CHF": [
        fact("2025-01-01", "2025-12-31", 50e6, "2026-04-01", "20-F", "FY")]}}}}}
    check("🔑-ifrs-figures-in-another-currency-are-not-mixed-with-dollar-prices",
          cfx.value(ifrs_chf, "revenue", "2026-11-27") is None)
    r = cfx.reporting(ifrs_chf)
    check("the-reporting-currency-is-recorded", r == {"standard": "IFRS", "currencies": ["CHF"]}, str(r))
    check("us-gaap-is-recorded", cfx.reporting(cf({"Assets": []}))["standard"] == "US GAAP")


def main():
    print("=" * 70)
    print("S&P 500 and watched-company lookup — pure logic, no network")
    print("=" * 70)
    test_freshest_tag_wins()
    test_ttm_and_growth()
    test_ratios()
    test_medians_and_verdicts()
    test_small_caps()
    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing scored.")


if __name__ == "__main__":
    main()
