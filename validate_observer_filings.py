#!/usr/bin/env python3
"""Pure-logic validation for OBSERVER. No network, no clock.

The class of wrongness this file exists to prevent: **OBSERVER reporting
something it did not observe.**

That has two concrete shapes, and both would look entirely ordinary on a
dashboard:

  1. A Form 4 presented as a PURCHASE. The transaction code lives in the
     filing body, which EDGAR serves from a host that answers 403. Insider
     sales outnumber purchases, so an undirected filing shown as buying is
     wrong in the most flattering possible direction, every day.
  2. A Form 4 attributed to the wrong entity. The filing names a person and
     a company with no label saying which is which, so a guess attributes an
     insider's activity to whichever CIK came first.
"""

from __future__ import annotations

import ast
import inspect
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import observer_filings as ob
from qt import edgar

PASS, FAIL = [], []


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def _imports(mod) -> set:
    tree = ast.parse(inspect.getsource(mod))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            for a in n.names:
                out.add(f"{n.module}.{a.name}")
    return out


# ── OBSERVER collects; it does not measure or trade ──────────────────────────

def test_capabilities_absent():
    print("\nOBSERVER cannot measure, score or trade")
    imported = _imports(ob)
    for m, why in (
        ("qt.referee", "an unattended collector must not spend a read"),
        ("qt.wrc", "no reality check in a collector"),
        ("qt.calendar_time", "no criterion is evaluated here"),
        ("alpaca", "no broker, ever"),
        ("alpaca_trade_api", "no broker, ever"),
    ):
        check(f"does-not-import-{m}", m not in imported, why)

    # Brief §4: no composite scores, no point forecasts, no ranking.
    names = set()
    for n in ast.walk(ast.parse(inspect.getsource(ob))):
        if isinstance(n, ast.Name):
            names.add(n.id.lower())
        elif isinstance(n, ast.Attribute):
            names.add(n.attr.lower())
    for banned in ("score", "rank", "forecast", "predict", "recommend",
                   "signal", "confidence"):
        hit = sorted(x for x in names if banned in x)
        check(f"binds-no-{banned}", not hit,
              "OBSERVER collects; scoring is the failure mode this whole "
              "architecture exists to avoid"
              + (f" — found {hit}" if hit else ""))


# ── 🔑 direction is never invented ───────────────────────────────────────────

HIT4 = {"_id": "0002163322-26-000004:wk-form4.xml",
        "_source": {"file_date": "2026-11-10",
                    "ciks": ["0002163322", "0001490281"],
                    "display_names": ["Mittal Ajay V.  (CIK 0002163322)",
                                      "Groupon, Inc.  (CIK 0001490281)"]}}


def test_direction_is_null_not_guessed():
    print("\nForm 4 direction is NOT OBSERVED, and says so")
    r = ob.observation("4", HIT4, "GRPN", 1490281, "2026-11-11T00:00:00Z")

    check("direction-defaults-null", r["direction"] is None,
          "the transaction code is in the body, and the body is on a host "
          "that answers 403. Unknown is the only true value")
    check("direction-source-explains", "403" in r["direction_source"],
          r["direction_source"])

    d = ob.digest([r], "2026-11-10", {"4": {"filings": 1}, "8-K": {"filings": 0}})
    # 🔑 The sentence that must survive every future edit to the digest.
    check("digest-warns-not-purchases",
          "not" in d.lower() and "purchase" in d.lower(),
          "insider SALES outnumber buys; an undirected filing shown as "
          "buying is wrong in the most flattering direction, every day")
    # Over the RECORD lines only. The warning block says "do not read them
    # as buying" on purpose, and a check over the whole digest trips on the
    # very sentence that prevents the error -- the same prose-vs-substance
    # confusion that has now bitten four guards in this repo.
    record_lines = [ln.lower() for ln in d.splitlines() if ln.startswith("· ")]
    check("no-record-line-claims-a-purchase",
          not any(w in ln for ln in record_lines
                  for w in ("bought", "buy", "purchase", "acquired")),
          f"{len(record_lines)} record line(s), none asserting a direction "
          f"nobody observed")


# ── 🔑 the issuer is identified, never positional ────────────────────────────

INFO = {
    1490281: {"name": "Groupon, Inc.", "tickers": ["GRPN"],
              "entityType": "operating"},
    2163322: {"name": "Mittal Ajay V.", "tickers": [], "entityType": "other"},
}


def test_pick_issuer():
    print("\nthe issuer is asked of the data, not inferred from position")
    cik, tk = edgar.pick_issuer(["0002163322", "0001490281"], INFO)
    check("issuer-is-the-one-with-tickers", (cik, tk) == (1490281, "GRPN"),
          f"{cik}/{tk} — the person is listed FIRST, so taking ciks[0] would "
          f"attribute the filing to Mittal Ajay V.")

    cik2, tk2 = edgar.pick_issuer(["0001490281", "0002163322"], INFO)
    check("order-does-not-matter", (cik2, tk2) == (1490281, "GRPN"))

    # An uncached CIK must be SKIPPED, never guessed — otherwise a cache miss
    # silently attributes a filing to a human.
    cik3, tk3 = edgar.pick_issuer(["0002163322", "0001490281"],
                                  {2163322: INFO[2163322]})
    check("uncached-cik-is-skipped-not-guessed", (cik3, tk3) == (None, None),
          "a missing lookup yields nothing; it must not fall back to the "
          "person it DID resolve")

    check("no-ciks-is-none", edgar.pick_issuer([], INFO) == (None, None))
    check("garbage-cik-survives",
          edgar.pick_issuer(["not-a-number", "0001490281"], INFO)[1] == "GRPN")
    check("empty-ticker-strings-ignored",
          edgar.pick_issuer(["1"], {1: {"tickers": ["", "  "]}}) == (None, None),
          "a blank ticker is not a ticker")


# ── EFTS counting ────────────────────────────────────────────────────────────

HITS = [
    {"_id": "0000001-26-000001:a.htm", "_source": {}},
    {"_id": "0000001-26-000001:ex1.htm", "_source": {}},
    {"_id": "0000001-26-000001:ex2.htm", "_source": {}},
    {"_id": "0000002-26-000002:b.htm", "_source": {}},
]


def test_dedupe_and_tickers():
    print("\nEFTS indexes documents, not filings")
    d = edgar.dedupe_filings(HITS)
    check("deduped-by-accession", len(d) == 2,
          f"{len(d)} filings from {len(HITS)} documents — one 8-K with two "
          f"exhibits is ONE filing, and counting hits reports triple")
    check("keeps-first-hit-per-filing",
          d["0000001-26-000001"]["_id"].endswith("a.htm"))
    check("no-id-skipped", edgar.dedupe_filings([{"_source": {}}]) == {})
    check("empty-no-crash", edgar.dedupe_filings([]) == {}
          and edgar.dedupe_filings(None) == {})

    f = edgar.tickers_from_display_names
    check("multi-ticker", f(["BIOVIE INC.  (BIVI, BIVIW)  (CIK 0001580149)"])
          == {"BIVI", "BIVIW"})
    check("cik-not-a-ticker", "CIK" not in f(["X  (SOME)  (CIK 0001234567)"]))
    check("form4-yields-nothing",
          f(HIT4["_source"]["display_names"]) == set(),
          "Form 4 display_names carry no symbol — which is exactly why "
          "pick_issuer exists")
    check("ticker-NA-survives", f(["NANO LABS LTD  (NA)  (CIK 1872302)"])
          == {"NA"}, "NA is Nano Labs and pandas eats it in two places")


def test_filing_url():
    print("\nevery record links to the filing")
    u = ob.filing_url(1490281, "0002163322-26-000004")
    check("dashes-stripped", "000216332226000004" in u, u)
    check("cik-unpadded", "/data/1490281/" in u, u)

    r = ob.observation("8-K", HITS[0], "ABCD", 1490281, "2026-11-11T00:00:00Z")
    for field in ("observed_at", "source", "form", "accession", "ticker",
                  "url", "direction", "direction_source"):
        check(f"record-has-{field}", field in r)


def test_universe_fallback_is_wider():
    print("\nthe watch list falls back WIDER, never narrower")
    src = inspect.getsource(ob.watched)
    check("falls-back-to-panel", "PANEL" in src,
          "an observer that silently watches fewer names than it claims is "
          "worse than one that watches too many")
    check("empty-universe-exits",
          "sys.exit(2)" in inspect.getsource(ob.main),
          "an empty watch list must not be reported as 'nothing happened'")


def main():
    print("=" * 70)
    print("observer_filings — pure logic (no network, no clock)")
    print("=" * 70)
    test_capabilities_absent()
    test_direction_is_null_not_guessed()
    test_pick_issuer()
    test_dedupe_and_tickers()
    test_filing_url()
    test_universe_fallback_is_wider()

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing scored.")


if __name__ == "__main__":
    main()
