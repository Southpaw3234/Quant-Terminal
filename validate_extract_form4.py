#!/usr/bin/env python3
"""Validate the Form 4 extractor's pure logic against known inputs.

No network. Every case here has a hand-checkable answer, because the
clustering rules are exactly the kind of thing that looks obviously right
and quietly manufactures events:

  * one person filing twice is not two insiders
  * a cluster's timestamp must be its LAST filing, not its first
  * option exercises and awards are not purchases
  * a filing consumed by one cluster must not seed another

Each of those, done wrong, inflates event counts in a direction that makes
a null look like a finding.
"""
from __future__ import annotations

import sys

import extract_form4 as f4

FAILURES: list = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)


def _tx(name, code, shares, price, fd, **extra):
    d = {"name": name, "transactionCode": code, "change": shares,
         "transactionPrice": price, "filingDate": fd,
         "transactionDate": fd, "isDerivative": False, "currency": "USD"}
    d.update(extra)
    return d


def test_normalise():
    print("\n--- normalise: tolerates missing and garbage fields ---")
    rows = f4.normalise([
        _tx("Alice", "P", 100, 10.0, "2026-01-05"),
        {},                                        # empty dict
        {"name": "Bob", "change": None, "transactionPrice": "oops"},
        "not a dict",                              # wrong type entirely
    ], "ACME")
    check("normalise-count", len(rows) == 3,
          f"{len(rows)} rows from 4 inputs (non-dict dropped)")
    check("normalise-value", abs(rows[0]["value"] - 1000.0) < 1e-9,
          f"value={rows[0]['value']} (100 sh x $10)")
    check("normalise-garbage", rows[2]["shares"] == 0.0 and rows[2]["price"] == 0.0,
          "unparseable numerics became 0.0 rather than raising")
    check("normalise-upper", rows[0]["ticker"] == "ACME", "ticker uppercased")


def test_filter():
    print("\n--- filter: code P only ---")
    rows = f4.normalise([
        _tx("Alice", "P", 100, 10.0, "2026-01-05"),   # keep
        _tx("Bob",   "A", 500, 10.0, "2026-01-05"),   # award
        _tx("Carol", "M", 500, 10.0, "2026-01-05"),   # option exercise
        _tx("Dave",  "S", -100, 10.0, "2026-01-05"),  # sale
        _tx("Erin",  "P", 100, 0.0, "2026-01-05"),    # no price -> unknowable
        _tx("Frank", "P", 100, 10.0, ""),             # no filing date
    ], "ACME")
    keep = f4.filter_open_market_purchases(rows)
    names = sorted(r["name"] for r in keep)
    check("filter-p-only", names == ["Alice"],
          f"kept {names} of 6 (awards, exercises, sales, priceless, undated dropped)")

    # Fields confirmed to exist by the 2026-09-01 live probe.
    extra = f4.normalise([
        _tx("Gina", "P", 100, 10.0, "2026-01-05", isDerivative=True),
        _tx("Hank", "P", 100, 10.0, "2026-01-05", currency="EUR"),
        _tx("Ivan", "P", 100, 10.0, "2026-01-05", currency=""),   # tolerated
    ], "ACME")
    kept = sorted(r["name"] for r in f4.filter_open_market_purchases(extra))
    check("filter-derivative-currency", kept == ["Ivan"],
          f"kept {kept} — derivative row and EUR row dropped, blank "
          f"currency tolerated")


def test_cluster_distinct_insiders():
    print("\n--- cluster: one person filing twice is NOT a cluster ---")
    same = f4.filter_open_market_purchases(f4.normalise([
        _tx("Alice", "P", 10000, 50.0, "2026-01-05"),
        _tx("Alice", "P", 10000, 50.0, "2026-01-06"),
    ], "ACME"))
    ev = f4.cluster(same, min_insiders=2, window_days=5, min_value=100_000)
    check("cluster-same-person", len(ev) == 0,
          f"{len(ev)} events from one insider filing twice (expect 0)")

    two = f4.filter_open_market_purchases(f4.normalise([
        _tx("Alice", "P", 10000, 50.0, "2026-01-05"),
        _tx("Bob",   "P", 10000, 50.0, "2026-01-06"),
    ], "ACME"))
    ev = f4.cluster(two, min_insiders=2, window_days=5, min_value=100_000)
    check("cluster-two-people", len(ev) == 1,
          f"{len(ev)} event from two distinct insiders (expect 1)")


def test_cluster_window_and_value():
    print("\n--- cluster: window and value bars bind ---")
    far = f4.filter_open_market_purchases(f4.normalise([
        _tx("Alice", "P", 10000, 50.0, "2026-01-05"),
        _tx("Bob",   "P", 10000, 50.0, "2026-01-20"),   # 15d apart
    ], "ACME"))
    check("cluster-window", len(f4.cluster(far, 2, 5, 100_000)) == 0,
          "two insiders 15d apart do not cluster in a 5d window")

    cheap = f4.filter_open_market_purchases(f4.normalise([
        _tx("Alice", "P", 10, 1.0, "2026-01-05"),
        _tx("Bob",   "P", 10, 1.0, "2026-01-06"),
    ], "ACME"))
    check("cluster-value", len(f4.cluster(cheap, 2, 5, 100_000)) == 0,
          "$20 of buying does not clear a $100k bar")


def test_cluster_timestamp():
    print("\n--- cluster: event_ts is the LAST filing, not the first ---")
    rows = f4.filter_open_market_purchases(f4.normalise([
        _tx("Alice", "P", 10000, 50.0, "2026-01-05"),
        _tx("Bob",   "P", 10000, 50.0, "2026-01-08"),
    ], "ACME"))
    ev = f4.cluster(rows, 2, 5, 100_000)
    ok = len(ev) == 1 and ev[0]["event_ts"] == "2026-01-08"
    check("cluster-timestamp", ok,
          f"event_ts={ev[0]['event_ts'] if ev else 'n/a'} "
          f"(2026-01-08 = when the cluster became visible; "
          f"2026-01-05 would claim foreknowledge)")
    check("cluster-first-recorded", ev and ev[0]["first_filing"] == "2026-01-05",
          "first_filing retained as metadata")


def test_no_double_count():
    print("\n--- cluster: a consumed filing does not seed another cluster ---")
    rows = f4.filter_open_market_purchases(f4.normalise([
        _tx("Alice", "P", 10000, 50.0, "2026-01-05"),
        _tx("Bob",   "P", 10000, 50.0, "2026-01-06"),
        _tx("Carol", "P", 10000, 50.0, "2026-01-07"),
    ], "ACME"))
    ev = f4.cluster(rows, 2, 5, 100_000)
    check("cluster-no-reuse", len(ev) == 1,
          f"3 filings in one window -> {len(ev)} event (expect 1, not 2-3)")
    check("cluster-insider-count", ev and ev[0]["n_insiders"] == 3,
          f"n_insiders={ev[0]['n_insiders'] if ev else 'n/a'} (expect 3)")


def test_event_schema():
    print("\n--- output schema matches event_study.py's contract ---")
    rows = f4.filter_open_market_purchases(f4.normalise([
        _tx("Alice", "P", 10000, 50.0, "2026-01-05"),
        _tx("Bob",   "P", 10000, 50.0, "2026-01-06"),
    ], "ACME"))
    ev = f4.cluster(rows, 2, 5, 100_000)[0]
    required = {"event_id", "ticker", "event_ts", "event_type"}
    check("schema-required", required <= set(ev),
          f"emits {sorted(required)} required by event_study.py")
    check("schema-date-only", ":" not in ev["event_ts"],
          f"event_ts={ev['event_ts']} is a DATE — Finnhub carries no "
          f"intraday time, so next-session entry is the only safe read")


def main():
    print("=" * 68)
    print("extract_form4.py — pure-logic validation (no network)")
    print("=" * 68)
    test_normalise()
    test_filter()
    test_cluster_distinct_insiders()
    test_cluster_window_and_value()
    test_cluster_timestamp()
    test_no_double_count()
    test_event_schema()
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
