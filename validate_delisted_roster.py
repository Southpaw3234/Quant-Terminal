#!/usr/bin/env python3
"""Pure-logic validation for build_delisted_roster.py — no network, no secrets.

Four things decide whether the roster is right, and each is tested against a
hand-checked answer taken from real EDGAR and price-source payloads observed
on 2026-09-05:

  1. TICKER AND ISSUER EXTRACTION. Every Form 25 carries the exchange as a
     co-filer. Pulling the wrong party gives a roster of stock exchanges.
  2. NAME MATCHING, which is the ticker-reuse defence. It has to tolerate
     "Inc" versus "Inc." and refuse an empty name.
  3. CLASSIFICATION ORDER. no-data before identity, identity before venue --
     get the order wrong and a reused symbol is silently promoted to a usable
     row.
  4. That ACVA-shaped input lands in `still-listed`, since ACV Auctions is
     alive on NYSE and is exactly the false positive that made this file
     necessary.
"""
from __future__ import annotations

import sys

import build_delisted_roster as r

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


# Real shapes, copied from EDGAR full-text search hits on 2026-09-05.
BHIL = ["Benson Hill, Inc.  (BHIL, BHILW)  (CIK 0001830210)",
        "NEW YORK STOCK EXCHANGE LLC  (CIK 0000876661)"]
GRDI = ["GRIID Infrastructure Inc.  (GRDI, GRDIW)  (CIK 0001830029)",
        "NYSE AMERICAN LLC  (CIK 0001143313)"]
NOTICK = ["SOME PRIVATE FILER  (CIK 0001111111)"]


def test_extraction():
    print("\n--- pulling the issuer, not the exchange ---")
    check("tickers-common-first", r.tickers_of(BHIL) == ["BHIL", "BHILW"],
          "common stock first, warrant second, exchange contributes nothing")
    check("issuer-not-exchange", r.issuer_of(BHIL) == "Benson Hill, Inc.",
          "the issuer is the first party carrying a ticker parenthesis")
    check("amex-co-filer-dropped", r.tickers_of(GRDI) == ["GRDI", "GRDIW"],
          "NYSE American is a co-filer on the filing and is skipped")
    check("no-ticker-yields-empty", r.tickers_of(NOTICK) == [],
          "a filer with no ticker yields nothing rather than a bad guess")
    check("cik-skips-exchanges",
          r.cik_of({"ciks": ["0000876661", "0001830210"]}) == 1830210,
          "the NYSE LLC CIK is never taken as the issuer, even when listed first")
    check("cik-none-when-only-exchange",
          r.cik_of({"ciks": ["0000876661"]}) is None,
          "an exchange-only filing yields no issuer")


def test_name_matching():
    print("\n--- the ticker-reuse defence ---")
    check("match-tolerates-suffix",
          r.name_matches("Benson Hill, Inc.", "Benson Hill Inc"),
          "punctuation and the Inc/Corp suffix must not break a real match")
    check("match-tolerates-partial",
          r.name_matches("Acutus Medical, Inc.", "Acutus Medical, Inc."),
          "identical names match")
    check("mismatch-detected",
          not r.name_matches("African Agriculture Holdings Inc.", "Applied Digital Corp"),
          "a REUSED symbol pointing at a different company is caught")
    check("empty-source-is-not-a-match",
          not r.name_matches("Benson Hill, Inc.", ""),
          "a missing name is NOT a match — the default must be to flag, not to trust")
    check("suffix-only-is-not-a-match",
          not r.name_matches("Holdings Inc", "Corp Ltd"),
          "two names sharing only corporate suffixes do not match")


def test_classification():
    print("\n--- classification, and the order it must run in ---")
    check("otc-continuation",
          r.classify("OTC Markets OTCPK", True, True) == "otc-continuation",
          "delisted from the exchange, still trading, same company — the usable case")
    check("acva-is-still-listed",
          r.classify("NYSE", True, True) == "still-listed",
          "ACV Auctions is ALIVE on NYSE: its Form 25 removed a warrant or was a "
          "transfer. This is the false positive the roster exists to catch")
    check("no-data-is-the-true-hole",
          r.classify("", False, False) == "no-data",
          "nothing priced anywhere")
    check("no-data-beats-identity",
          r.classify("NYSE", False, True) == "no-data",
          "absence of bars is checked BEFORE identity — there is nothing to identify")
    check("identity-beats-venue",
          r.classify("NYSE", True, False) == "identity-mismatch",
          "a name mismatch is flagged even on a major exchange, rather than being "
          "promoted to still-listed and quietly dropped from the dead list")
    check("unknown-venue-not-silently-usable",
          r.classify("Some Foreign Board", True, True) == "unknown-venue",
          "an unrecognised venue gets its own bucket instead of defaulting to usable")


def test_constants():
    print("\n--- the roster does not screen, price, or spend ---")
    src = open("build_delisted_roster.py", encoding="utf-8").read()
    # Assert the WRITE, not the mention. The first draft of this check failed
    # on the docstring, which names the frozen universe file while explaining
    # why the roster exists. A test that cannot tell a sentence from a write
    # is worse than no test: it trains you to edit prose to make CI green.
    check("writes-only-the-roster",
          src.count(".to_csv(") == 1 and "df.to_csv(OUT_CSV" in src,
          "exactly ONE write in the file and it targets OUT_CSV — the frozen "
          "universe cannot be written even though the docstring names it")
    check("no-return-computed",
          "forward_return" not in src and "event_study" not in src,
          "no forward return, no engine import — this is inventory, not measurement")
    check("smoke-run-renamed",
          "_SMOKE" in src,
          "a capped run cannot write the real roster filename")
    check("keyed-on-cik",
          '"cik": cik' in src,
          "rows are keyed on CIK, not on the symbol that can be reused")


def main():
    print("=" * 70)
    print("build_delisted_roster — pure logic (no network)")
    print("=" * 70)
    test_extraction()
    test_name_matching()
    test_classification()
    test_constants()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("The issuer is taken over the exchange; a reused symbol is flagged")
    print("rather than trusted; and an ACVA-shaped row lands in still-listed.")


if __name__ == "__main__":
    main()
