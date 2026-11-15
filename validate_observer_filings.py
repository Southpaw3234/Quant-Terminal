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

    d = ob.digest([r], "2026-11-10",
                  {"watched": 10, "totals": {"4": {"filings": 1}}})
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
    from qt import agentio
    w = agentio.watched_universe("no-such-daily.json",
                                 "data/universe/v29_universe_pit.csv", "")
    check("falls-back-to-panel", len(w["tickers"]) > 3000
          and "wider" in w["source"],
          f"{len(w['tickers'])} tickers from: {w['source']} — an observer "
          f"that silently watches fewer names than it claims is worse than "
          f"one that watches too many")
    check("NA-survives-the-watch-list", "NA" in w["tickers"],
          "Nano Labs; pandas turns it into NaN by default")
    check("empty-universe-exits",
          "sys.exit(2)" in inspect.getsource(ob.main),
          "an empty watch list must not be reported as 'nothing happened'")


# ── 🔑 direction comes from a second source, by accession ────────────────────

def _row(code, name="A", acc="0002163322-26-000004", filed="2026-11-10",
         shares=100.0, price=10.0, deriv=False):
    return {"ticker": "GRPN", "name": name, "code": code, "shares": shares,
            "price": price, "value": abs(shares) * price,
            "filing_date": filed, "transaction_date": filed, "tx_id": acc,
            "is_derivative": deriv, "currency": "USD"}


def test_direction_from_finnhub():
    print("\ndirection is read from transaction codes, matched by accession")
    check("P-is-purchase", ob.direction_of([_row("P")]) == "purchase")
    check("S-is-sale", ob.direction_of([_row("S", shares=-5)]) == "sale")
    check("P-and-S-is-mixed",
          ob.direction_of([_row("P"), _row("S", shares=-5)]) == "mixed")
    for code in ("A", "M", "F", "G", "X"):
        check(f"code-{code}-is-other-not-a-purchase",
              ob.direction_of([_row(code)]) == "other",
              "a grant or an option exercise is not an open-market buy")
    check("derivative-P-is-not-a-purchase",
          ob.direction_of([_row("P", deriv=True)]) == "other",
          "excluded on Finnhub's own flag, not inferred from the code")

    r = ob.observation("4", HIT4, "GRPN", 1490281, "t0")
    n = ob.enrich_direction([r], {"GRPN": [_row("P")]}, "t1")
    check("direction-filled-by-accession",
          n == 1 and r["direction"] == "purchase"
          and r["direction_observed_at"] == "t1"
          and "accession" in r["direction_source"])
    check("purchase-value-recorded", r["purchase_value"] == 1000.0)

    # 🔑 A same-day row for a DIFFERENT accession must not donate direction.
    r2 = ob.observation("4", HIT4, "GRPN", 1490281, "t0")
    n2 = ob.enrich_direction([r2], {"GRPN": [_row("P", acc="0009-26-1")]}, "t1")
    check("other-insiders-filing-does-not-donate-direction",
          n2 == 0 and r2["direction"] is None,
          "two insiders can file on one day; a date match would hand one "
          "filing the other's direction")
    check("unmatched-says-it-is-pending",
          "not in Finnhub" in r2["direction_source"])

    # A direction is written once.
    ob.enrich_direction([r], {"GRPN": [_row("S", shares=-1)]}, "t2")
    check("a-recorded-direction-is-never-rewritten",
          r["direction"] == "purchase" and r["direction_observed_at"] == "t1")

    r3 = ob.observation("4", HIT4, "GRPN", 1490281, "t0")
    ob.enrich_direction([r3], {}, "t1", {"GRPN": "finnhub answered 500"})
    check("a-failed-lookup-is-named-not-read-as-no-purchase",
          r3["direction"] is None and "500" in r3["direction_source"])

    r8 = ob.observation("8-K", HITS[0], "ABCD", 1, "t0")
    ob.enrich_direction([r8], {"ABCD": [_row("P", acc="0000001-26-000001")]}, "t1")
    check("only-ownership-forms-get-a-direction", r8["direction"] is None
          and r8["direction_source"].startswith("n/a"))

    check("purchase-value-None-when-unpriced",
          ob.purchase_value([_row("P", price=0)]) is None,
          "None is 'not stated', never zero")


def test_first_purchase_and_clusters():
    print("\nfirst purchases and cluster events")
    rows = [_row("P", name="A", filed="2026-11-10"),
            _row("P", name="B", filed="2026-11-10"),
            _row("P", name="B", filed="2025-03-01", acc="x"),
            _row("S", name="C", filed="2024-01-05", acc="y", shares=-9)]
    fp = ob.first_purchases(rows, "2026-11-10")
    check("first-purchase-is-the-insider-with-no-earlier-buy",
          [x["insider"] for x in fp] == ["A"])
    check("history-window-is-stated",
          fp[0]["history_from"] == "2024-01-05",
          "'first in the available history', never 'first ever'")
    check("no-purchases-no-claim", ob.first_purchases([_row("S")], "2026-11-10") == [])

    big = [_row("P", name="A", filed="2026-11-08", shares=10000, price=8),
           _row("P", name="B", filed="2026-11-10", shares=5000, price=8)]
    ev = ob.cluster_events(big, "2026-11-10", "t1")
    check("cluster-completes-on-the-last-filing-day",
          len(ev) == 1 and ev[0]["kind"] == "derived_event"
          and ev[0]["event_type"] == "form4_cluster_buy"
          and ev[0]["n_insiders"] == 2)
    check("cluster-carries-the-frozen-definition",
          ev[0]["definition"] == {"min_insiders": ob.f4.MIN_INSIDERS,
                                  "window_days": ob.f4.WINDOW_DAYS,
                                  "min_value": ob.f4.MIN_VALUE},
          "imported, not restated, so it IS the event the base rate describes")
    check("cluster-not-reported-before-it-is-complete",
          ob.cluster_events(big, "2026-11-08", "t1") == [])
    check("one-insider-twice-is-not-a-cluster",
          ob.cluster_events([_row("P", name="A", filed="2026-11-08",
                                  shares=10000, price=8),
                             _row("P", name="A", filed="2026-11-10",
                                  shares=10000, price=8)],
                            "2026-11-10", "t1") == [])
    check("below-the-dollar-bar-is-not-a-cluster",
          ob.cluster_events([_row("P", name="A"), _row("P", name="B")],
                            "2026-11-10", "t1") == [])
    check("cluster-function-is-the-frozen-one",
          ob.f4.cluster.__module__ == "extract_form4")


def test_append_only():
    print("\na re-run can add to a day and never shrink it")
    a = ob.observation("8-K", HITS[0], "ABCD", 1, "first-run")
    b = ob.observation("8-K", HITS[3], "WXYZ", 2, "first-run")
    a2 = ob.observation("8-K", HITS[0], "ABCD", 1, "second-run")
    merged, added = ob.merge_day([a, b], [a2])
    check("partial-rerun-does-not-shrink", len(merged) == 2 and added == 0,
          "a second run with a partial EDGAR response used to REPLACE a "
          "complete day with a shorter one")
    check("original-observed_at-survives",
          merged[0]["observed_at"] == "first-run")
    merged2, added2 = ob.merge_day([a], [a2, b])
    check("new-filings-are-appended", len(merged2) == 2 and added2 == 1)
    check("duplicate-ids-in-one-batch-collapse",
          ob.merge_day([], [a, a2])[1] == 1)
    check("record-has-an-id-and-raw",
          a["id"] == "edgar:0000001-26-000001" and "raw" in a)


def test_cache_does_not_keep_failures():
    print("\na failed CIK lookup is not cached as an answer")
    import json, os, tempfile
    d = tempfile.mkdtemp()
    path = os.path.join(d, "c.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"1": {}, "2": {"tickers": ["X"]}}, f)
    old = ob.CACHE
    try:
        ob.CACHE = path
        c = ob.load_cache()
        check("empty-entries-dropped-on-load", list(c) == [2],
              "an earlier version cached {} for a failed lookup, making "
              "that CIK unresolvable forever")
        c[3] = {}
        ob.save_cache(c)
        check("empty-entries-not-written",
              list(json.load(open(path, encoding="utf-8"))) == ["2"])
    finally:
        ob.CACHE = old
    check("collector-never-caches-a-failure",
          "if got:" in inspect.getsource(ob.collect_filings))
    check("five-forms-are-collected",
          ob.FORMS == ("8-K", "4", "10-Q", "10-K", "424B5"))


# ── 🔑 one bad response does not end the run ─────────────────────────────────

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._p = status, payload

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


class _Sess:
    """Replays a scripted list of responses (or exceptions)."""
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_retries():
    print("\na transient failure is retried; an exhausted one is reported")
    real_sleep = edgar.time.sleep
    edgar.time.sleep = lambda *_: None          # no real waiting in a test
    try:
        s = _Sess([_Resp(500), _Resp(503), _Resp(200, {"a": 1})])
        got = edgar.fetch_json(s, "u")
        check("two-5xx-then-200-succeeds", got["ok"] and got["json"] == {"a": 1}
              and s.calls == 3,
              "the first live LIBRARIAN run made ~600 requests and died on "
              "one transient 500, discarding 55 issuers of finished work")
        s = _Sess([_Resp(500)] * 4)
        got = edgar.fetch_json(s, "u")
        check("exhausted-retries-report-failure-not-raise",
              not got["ok"] and "after 4 attempts" in got["reason"]
              and s.calls == 4)
        s = _Sess([_Resp(404)])
        got = edgar.fetch_json(s, "u")
        check("a-404-is-not-retried", not got["ok"] and s.calls == 1,
              "retrying a definite answer only delays it")
        s = _Sess([ConnectionError("x"), _Resp(200, {"ok": 1})])
        check("a-network-error-is-retried", edgar.fetch_json(s, "u")["ok"])
        s = _Sess([_Resp(429), _Resp(200, {})])
        check("a-429-is-retried", edgar.fetch_json(s, "u")["ok"])
        s = _Sess([_Resp(200, None)] * 4)
        check("non-json-200-is-a-failure", not edgar.fetch_json(s, "u")["ok"])

        # efts_day: a failed page makes the day a SAMPLE, not a crash.
        page = {"hits": {"total": {"value": 250},
                         "hits": [{"_id": f"{i:010d}-26-000001:a.htm",
                                   "_source": {}} for i in range(100)]}}
        s = _Sess([_Resp(200, page)] + [_Resp(500)] * 4)
        d = edgar.efts_day(s, "8-K", "2026-11-13", 12)
        check("a-failed-page-keeps-what-was-fetched", len(d["hits"]) == 100)
        check("🔑-and-flags-the-day-as-a-sample",
              d["truncated"] is True and d["error"],
              "a census missing a page must not be presented as complete")
        s = _Sess([_Resp(500)] * 4)
        try:
            edgar.efts_page(s, "8-K", "2026-11-13")
            raised = False
        except RuntimeError:
            raised = True
        check("efts_page-raises-only-after-retries", raised and s.calls == 4)
        check("a-failed-submissions-lookup-is-empty-not-cached-as-an-answer",
              edgar.submissions(_Sess([_Resp(500)] * 3), 1) == {})
        h = edgar.filing_history(_Sess([_Resp(500)] * 3), 1)
        check("a-failed-history-lookup-says-it-failed",
              h["ok"] is False and "attempts" in h["reason"])
    finally:
        edgar.time.sleep = real_sleep


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
    test_direction_from_finnhub()
    test_first_purchase_and_clusters()
    test_append_only()
    test_cache_does_not_keep_failures()
    test_retries()

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing scored.")


if __name__ == "__main__":
    main()
