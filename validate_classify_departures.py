#!/usr/bin/env python3
"""Pure-logic validation for the departure classifier. No network.

The class of wrongness this file exists to prevent: **"no-data" being read
as "died".** A name the price source cannot find has left for one of half a
dozen reasons, and they do not share a sign: a bankruptcy is a total loss,
an acquisition is a payout, a SPAC redemption is flat. A classifier that
gets the bucket wrong moves a survivorship estimate in the wrong direction
while looking like it reduced the uncertainty.

So the fixtures below are shaped like five real companies whose filing
histories were read before this was written, and the ordering of the rules
is tested as carefully as the rules.
"""

from __future__ import annotations

import ast
import inspect
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import classify_departures as cd

PASS, FAIL = [], []
F25 = "2025-07-01"


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def sub(filings, sic="", tickers=(), exchanges=()):
    """A submissions payload. `filings` = [(form, date, "items")]."""
    return {"sic": sic, "tickers": list(tickers), "exchanges": list(exchanges),
            "filings": {"recent": {
                "form": [f[0] for f in filings],
                "filingDate": [f[1] for f in filings],
                "items": [f[2] if len(f) > 2 else "" for f in filings]}}}


def cat(s, when=F25):
    return cd.classify(s, when)["category"]


def test_the_five_real_shapes():
    print("\nfive shapes read from real filing histories")
    azek = sub([("425", "2025-04-01"), ("425", "2025-05-10"),
                ("8-K", "2025-07-01", "2.01,3.01,3.03,5.01,9.01"),
                ("15-12G", "2025-07-11"), ("25-NSE", "2025-07-01")], sic="3089")
    r = cd.classify(azek, F25)
    check("an-acquired-company-is-acquired", r["category"] == "acquired",
          "AZEK: thirty 425s and an 8-K item 2.01 — a payout, not a loss")
    check("the-evidence-names-the-filing", "2.01" in r["evidence"])

    tcs = sub([("8-K", "2024-12-22", "1.03,2.04,7.01"),
               ("8-K", "2024-12-09", "3.01"), ("15-12G", "2025-02-01"),
               ("8-K", "2025-01-28", "1.02,3.03,5.01,5.03")], sic="5700")
    check("a-bankruptcy-is-a-bankruptcy", cat(tcs, "2024-12-10") == "bankruptcy",
          "Container Store: 8-K item 1.03")

    spac = sub([("10-Q", "2024-08-14"), ("8-K", "2024-10-25", "3.01"),
                ("25-NSE", "2024-10-30")], sic="6770")
    check("a-blank-check-is-a-spac", cat(spac, "2024-10-30") == "spac",
          "Pyrophyte: SIC 6770")

    fisv = sub([("8-K", "2023-06-06", "3.01"), ("10-Q", "2023-07-27")],
               sic="7389", tickers=["FISV"], exchanges=["Nasdaq"])
    check("a-ticker-change-is-NOT-a-departure",
          cat(fisv, "2023-06-06") == "still_listed",
          "Fiserv moved exchange and symbol; it did not go anywhere")

    fund = sub([("N-CSRS", "2025-12-01"), ("NPORT-P", "2026-01-20"),
                ("425", "2026-01-05"), ("25-NSE", "2026-02-09")])
    check("a-closed-end-fund-is-a-fund", cat(fund, "2026-02-09") == "fund")


def test_rule_order():
    print("\nthe first matching rule wins, and the order is deliberate")
    # A bankrupt company also files 5.01 and Form 15 on the way out.
    both = sub([("8-K", "2025-06-01", "1.03"),
                ("8-K", "2025-07-01", "2.01,5.01"), ("15-12G", "2025-08-01")])
    check("🔑-bankruptcy-outranks-acquired", cat(both) == "bankruptcy",
          "a sale out of chapter 11 reports a change of control too; calling "
          "it an acquisition would turn the worst outcome into a payout")
    # A bankrupt blank check or fund is still a bankruptcy.
    check("bankruptcy-outranks-spac",
          cat(sub([("8-K", "2025-06-01", "1.03")], sic="6770")) == "bankruptcy")
    # A company still listed on a major exchange is not acquired even if it
    # filed merger paper (it may have been the ACQUIRER).
    acq = sub([("425", "2025-04-01"), ("425", "2025-05-01"),
               ("8-K", "2025-07-01", "2.01")],
              tickers=["BIG"], exchanges=["NYSE"])
    check("🔑-an-acquirer-is-not-recorded-as-acquired", cat(acq) == "still_listed",
          "the buyer files 425s and an 8-K 2.01 as well")
    # An acquired company gets a routine 3.01 notice; it was not for cause.
    check("acquired-outranks-delisted-for-cause",
          cat(sub([("8-K", "2025-07-01", "2.01,3.01")])) == "acquired")
    # An OTC listing is not a major-exchange listing.
    otc = sub([("8-K", "2025-03-01", "3.01")], tickers=["XYZQ"],
              exchanges=["OTC"])
    check("an-OTC-listing-is-not-still-listed", cat(otc) == "delisted_for_cause")


def test_windows_and_fallbacks():
    print("\nwindows, and what happens when nothing matches")
    check("a-deficiency-notice-is-for-cause",
          cat(sub([("8-K", "2025-02-01", "3.01"), ("25-NSE", F25)]))
          == "delisted_for_cause")
    check("form-15-alone-is-deregistered",
          cat(sub([("15-12G", "2025-07-20"), ("10-K", "2025-03-01")]))
          == "deregistered")
    check("a-bankruptcy-three-years-earlier-is-not-this-departure",
          cat(sub([("8-K", "2022-01-01", "1.03"), ("10-K", "2025-03-01")]))
          == "unclassified",
          "the window is measured from the Form 25, not from anywhere")
    check("one-stray-425-is-not-an-acquisition",
          cat(sub([("425", "2025-05-01"), ("10-K", "2025-03-01")]))
          == "unclassified", "two merger filings, or a completed deal")
    check("a-2.01-a-year-earlier-is-not-this-departure",
          cat(sub([("8-K", "2024-03-01", "2.01"), ("10-K", "2025-03-01")]))
          == "unclassified")
    check("item-numbers-are-matched-whole",
          cat(sub([("8-K", "2025-06-01", "1.01,9.01")])) == "unclassified",
          "1.01 is not 1.03")
    check("an-8-K-amendment-counts",
          cat(sub([("8-K/A", "2025-06-01", "1.03")])) == "bankruptcy")

    e = cd.classify({}, F25)
    check("an-empty-payload-is-unclassified-not-a-crash",
          e["category"] == "unclassified" and "no filing history" in e["evidence"])
    check("a-garbage-date-does-not-crash",
          cd.classify(sub([("8-K", "nope", "1.03")]), "also-nope")["category"]
          == "unclassified")
    check("every-result-carries-evidence",
          all(cd.classify(s, F25)["evidence"] for s in (
              sub([("8-K", "2025-06-01", "1.03")]), sub([("10-K", "2025-01-01")]),
              sub([], sic="6770"))))
    check("every-category-returned-is-declared",
          all(cat(s) in cd.CATEGORIES for s in (
              {}, sub([("N-CSR", "2025-01-01")]), sub([("15-12G", "2025-07-05")]))))


def test_bookkeeping():
    print("\ncounts, failures and capabilities")
    rows = [{"category": "acquired"}, {"category": "acquired"},
            {"category": "bankruptcy"}, {"category": "lookup_failed"}]
    s = cd.summarise(rows)
    check("summary-counts-each-category", s["acquired"] == 2
          and s["bankruptcy"] == 1 and s["total"] == 4)
    check("🔑-a-failed-lookup-is-its-own-category",
          s["lookup_failed"] == 1 and s["unclassified"] == 0,
          "one is a fact about EDGAR tonight, the other about a company")
    src = inspect.getsource(cd.main)
    check("a-mostly-failed-run-refuses-to-write", "sys.exit(5)" in src
          and src.index("sys.exit(5)") < src.index("csv.DictWriter"),
          "a file of lookup_failed rows is an outage with a CSV header")
    check("an-empty-roster-refuses", "sys.exit(2)" in src)

    imported = set()
    for n in ast.walk(ast.parse(inspect.getsource(cd))):
        if isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module)
            imported.update(f"{n.module}.{a.name}" for a in n.names)
    for m in ("qt.referee", "qt.wrc", "qt.calendar_time", "qt.prices",
              "yfinance", "pandas"):
        check(f"does-not-import-{m}", m not in imported,
              "pandas turns the ticker NA into NaN" if m == "pandas" else
              "this inventories; it does not measure")
    r = cd.classify(sub([("8-K", "2025-06-01", "1.03")]), F25)
    check("a-classification-carries-no-number",
          not any(isinstance(v, (int, float)) and not isinstance(v, bool)
                  for v in r.values()),
          "a bucket and its evidence — no loss estimate, no return")
    lits = [n.value for n in ast.walk(ast.parse(inspect.getsource(cd)))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    check("writes-its-own-file-never-the-frozen-panel",
          not any(s.endswith("v29_universe_pit.csv") for s in lits))


def main():
    print("=" * 70)
    print("classify_departures — pure logic (no network)")
    print("=" * 70)
    test_the_five_real_shapes()
    test_rule_order()
    test_windows_and_fallbacks()
    test_bookkeeping()
    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing priced.")


if __name__ == "__main__":
    main()
