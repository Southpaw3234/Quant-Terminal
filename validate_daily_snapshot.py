#!/usr/bin/env python3
"""Pure-logic validation for the daily terminal feed. No network, no clock.

The daily snapshot is the most dangerous file in the repository, for a reason
that has nothing to do with arithmetic: it is the only thing a reader will
actually look at. The registry is correct and nobody reads it; the terminal
is read every day. So the checks here are mostly about whether the page can
LIE -- by going stale without saying so, by drifting away from the declared
screen, or by presenting a pool of eligible names as a portfolio.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import re
import sys
from pathlib import Path

import daily_snapshot as ds

PASS, FAIL = [], []


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


# ── the screen must be imported, never re-typed ──────────────────────────────

def test_screen_is_imported():
    print("\nthe declared screen is imported, not restated")
    import build_universe_v29 as v29

    src = inspect.getsource(ds)
    # A literal 2.00 / 50000 / 5000000 in this file would mean the terminal
    # carries its own copy of the declared tuple. Two copies drift, and the
    # terminal would then show a universe no specification was written
    # against while looking perfectly healthy.
    for lit in ("2.00", "50000", "5000000", "5_000_000"):
        bad = re.search(r"(?<![\w.])" + re.escape(lit) + r"(?![\w.])", src)
        check(f"no-hardcoded-{lit}", bad is None,
              f"the screen tuple lives in build_universe_v29; this file must "
              f"reference it, not repeat it")

    for attr in ("ADV_MIN", "ADV_MAX", "PRICE_MIN", "MIN_HISTORY",
                 "ADV_LOOKBACK"):
        check(f"screen-imported-{attr}", hasattr(v29, attr),
              f"v29.{attr} = {getattr(v29, attr, None)}")

    check("reuses-screen-chunk", "v29.screen_chunk" in src,
          "the same code path that built the frozen panel, not a copy")
    check("reuses-fetch-chunk", "v29.fetch_chunk" in src)


# ── honesty invariants ───────────────────────────────────────────────────────

def test_honesty_fields_are_data():
    print("\nthe snapshot states what it is NOT, as data")
    src = inspect.getsource(ds.main)
    for field, literal in (('"status"', '"research"'),
                           ('"live_trading"', "False"),
                           ('"capital_deployed"', "0"),
                           ('"eligible_is_a_buy_list"', "False")):
        check(f"declares-{field.strip(chr(34))}",
              field in src and literal in src,
              "a disclaimer in prose can be removed by a page edit; a field "
              "cannot be removed without the validator noticing")

    # The one that matters most. If a read ever MEETS its criteria this flips,
    # and that is the day the page is allowed to say something different.
    rec = ds.research_record()
    check("no-criteria-met-yet", rec["any_criteria_met"] is False,
          f"every read so far is NOT MET across "
          f"{len(rec['programmes'])} programmes — ⚠️ TRIPWIRE: this check is "
          f"MEANT to go red the day a specification passes")

    total_reads = sum(len(p["reads"]) for p in rec["programmes"])
    check("reads-are-counted", total_reads >= 5,
          f"{total_reads} reads on the record; reads are append-only so this "
          f"is a lower bound and never goes stale")
    for p in rec["programmes"]:
        check(f"k-within-budget-{p['id']}",
              p["k_budget"] is not None and 0 <= p["k_used"] <= p["k_budget"],
              f"K {p['k_used']}/{p['k_budget']}")


# ── no measurement may happen here ───────────────────────────────────────────

def _imported_modules(mod) -> set:
    """Every module name this file imports, from its AST.

    Parsed rather than grepped: the file's own prose says the words "Referee"
    and "K spent" while explaining that it does not use them, and a substring
    search cannot tell a comment from a call. What makes a capability
    unreachable is the absence of the IMPORT, so that is what is checked.
    """
    tree = ast.parse(inspect.getsource(mod))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
                out.add(node.module)
                for a in node.names:
                    out.add(f"{node.module}.{a.name}")
    return out


def test_no_measurement():
    print("\nthe terminal gathers; it does not measure")
    imported = _imported_modules(ds)

    # The Referee is what spends K. This job runs unattended on a cron, so
    # the capability has to be absent rather than merely unused.
    for m, why in (
        ("qt.referee", "a cron job must not be able to spend a read"),
        ("qt.wrc", "no reality check is run in the daily feed"),
        ("qt.calendar_time", "no criterion is evaluated in the daily feed"),
        ("qt.execution", "the terminal does not model fills"),
        ("qt.ledger", "the terminal does not write a ledger"),
        ("alpaca", "the daily terminal cannot reach a broker"),
        ("alpaca_trade_api", "the daily terminal cannot reach a broker"),
    ):
        check(f"does-not-import-{m}", m not in imported, why)

    # It should, however, import the screen it claims to apply.
    check("imports-the-declared-screen", "build_universe_v29" in imported,
          "the same module that built the frozen panel")


# ── staleness ────────────────────────────────────────────────────────────────

def test_staleness():
    print("\nstaleness is computed, not assumed")
    now = dt.datetime(2026, 11, 11, 22, 0, tzinfo=dt.timezone.utc)

    s = ds.staleness("2026-11-11T21:00:00Z", "2026-11-11", now=now)
    check("fresh-feed-age", abs(s["feed_age_days"] - 0.042) < 0.01,
          f"{s['feed_age_days']} days")
    check("fresh-data-age", s["data_age_days"] == 0, f"{s['data_age_days']}")

    s = ds.staleness("2026-10-02T21:00:00Z", "2026-10-02", now=now)
    check("stale-feed-detected", s["feed_age_days"] > 40,
          f"{s['feed_age_days']} days — the quant_runner.py failure: the feed "
          f"froze and the page kept rendering a book the account did not hold")

    # Pipeline healthy, price source dead. The dangerous case, because the
    # job is green and the numbers are old.
    s = ds.staleness("2026-11-11T21:00:00Z", "2026-09-29", now=now)
    check("running-but-stale-data", s["feed_age_days"] < 1
          and s["data_age_days"] > 30,
          f"feed {s['feed_age_days']}d / data {s['data_age_days']}d — green "
          f"job, stale prices, and the two must be distinguishable")

    s = ds.staleness("not-a-date", "not-a-date", now=now)
    check("garbage-dates-are-none",
          s["feed_age_days"] is None and s["data_age_days"] is None,
          "an unparseable date reads as UNKNOWN, never as fresh")


# ── the EDGAR index parser ───────────────────────────────────────────────────

def test_display_name_tickers():
    print("\nticker extraction from EFTS display_names")
    f = ds.tickers_from_display_names

    # The live shape, from EDGAR on 2026-11-10.
    check("real-shape",
          f(["BIOVIE INC.  (BIVI, BIVIW)  (CIK 0001580149)"]) == {"BIVI", "BIVIW"},
          "share class and warrant, comma-separated in ONE parenthetical")

    # 🔑 The trap. The last bracket is the CIK, and taking "the thing in
    # brackets" would file every company in the market under ticker 'CIK'.
    check("cik-parenthetical-not-a-ticker",
          "CIK" not in f(["SOMECO  (SOME)  (CIK 0001234567)"]))
    check("no-ticker-yields-nothing",
          f(["PRIVATE HOLDINGS LLC  (CIK 0001234567)"]) == set(),
          "an empty set is true; a placeholder '' would later match a blank "
          "field somewhere")

    check("ticker-NA-survives",
          f(["NANO LABS LTD  (NA)  (CIK 0001872302)"]) == {"NA"},
          "'NA' is a real listed company and has already been lost twice in "
          "this repo — see qt/tickers.py")
    check("handles-dot-and-dash", f(["X CORP  (BRK.B, RDS-A)"]) == {"BRK.B", "RDS-A"})
    check("rejects-overlong-token",
          f(["LONG  (ABCDEFGHIJK)"]) == set(), "not a ticker")
    check("empty-input", f(None) == set() and f([]) == set())


HITS = [
    # One 8-K: three documents, one accession. EFTS indexes DOCUMENTS.
    {"_id": "0000001-26-000001:a.htm",
     "_source": {"display_names": ["EXAMPLE CORP  (EXMP)  (CIK 0000001)"]}},
    {"_id": "0000001-26-000001:ex1.htm",
     "_source": {"display_names": ["EXAMPLE CORP  (EXMP)  (CIK 0000001)"]}},
    {"_id": "0000001-26-000001:ex2.htm",
     "_source": {"display_names": ["EXAMPLE CORP  (EXMP)  (CIK 0000001)"]}},
    # A second filer, in the universe.
    {"_id": "0000002-26-000002:b.htm",
     "_source": {"display_names": ["NANO LABS LTD  (NA)  (CIK 0000002)"]}},
    # A third, NOT in the universe.
    {"_id": "0000003-26-000003:c.htm",
     "_source": {"display_names": ["OUTSIDE INC  (ZZZZ)  (CIK 0000003)"]}},
]


def test_summarise_hits():
    print("\nfolding EFTS hits into counts")
    eligible = {"EXMP", "NA"}
    got = ds.summarise_hits(HITS, eligible)

    # 🔑 The one that matters. Counting hits instead of accessions reports
    # roughly triple the filings that happened, and looks plausible.
    check("deduped-by-accession", got["filings"] == 3,
          f"{got['filings']} filings from {len(HITS)} documents — one 8-K "
          f"with two exhibits is ONE filing")
    check("universe-intersection", got["in_universe"] == 2,
          f"{got['in_universe']} — ZZZZ is outside the universe")
    check("ticker-NA-counted", "NA" in got["names"],
          "the ticker that pandas keeps eating is in the universe here")
    check("names-sorted", got["names"] == sorted(got["names"]))

    none = ds.summarise_hits(HITS, set())
    check("no-match-still-counts-filings",
          none["filings"] == 3 and none["in_universe"] == 0,
          "'quiet day' and 'broken universe join' stay distinguishable")

    check("empty-is-zero-not-crash",
          ds.summarise_hits([], eligible)["filings"] == 0
          and ds.summarise_hits(None, eligible)["filings"] == 0)
    check("missing-id-skipped",
          ds.summarise_hits([{"_source": {}}], eligible)["filings"] == 0)
    check("name-list-is-capped",
          len(ds.summarise_hits(HITS, eligible, max_names=1)["names"]) == 1,
          "the snapshot is served to a browser; the list is capped while the "
          "count is not")


def test_evidence_query_is_unbiased():
    print("\nthe evidence query is a census, not a sample")
    src = inspect.getsource(ds._efts)
    check("empty-q", '"q": ""' in src,
          "EFTS accepts forms+dates with no keyword; any keyword would "
          "select the filings containing that word and still look like a "
          "census of the day")
    # Scanned over STRING LITERALS, not raw source: the module's own comment
    # names the host in order to warn against it, and a substring search
    # cannot tell a warning from a URL. Same distinction as the import check.
    literals = [n.value for n in ast.walk(ast.parse(inspect.getsource(ds)))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    check("not-www-sec-gov",
          not any("www.sec.gov" in s for s in literals),
          "that host answers 403 from CI — the first snapshot lost its whole "
          "evidence feed to it, so no URL here may point at it")
    check("efts-host", "efts.sec.gov" in ds.EFTS_URL)


# ── the registry view must not invent anything ───────────────────────────────

def test_research_record_matches_registries():
    print("\nthe research record is a VIEW of the registries")
    rec = ds.research_record()
    for pid, path, _ in ds.REGISTRIES:
        p = Path(path)
        if not p.exists():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        prog = next((x for x in rec["programmes"] if x["id"] == pid), None)
        check(f"{pid}-present", prog is not None)
        if prog is None:
            continue
        check(f"{pid}-budget-from-file",
              prog["k_budget"] == doc.get("k_budget"),
              f"{prog['k_budget']} — read from the file, not restated here")
        in_file = {s for s, v in doc.get("specifications", {}).items()
                   if v.get("read_at")}
        check(f"{pid}-reads-match-file",
              {r["spec_id"] for r in prog["reads"]} == in_file,
              f"{len(in_file)} read in the registry, {len(prog['reads'])} in "
              f"the view")
        for r in prog["reads"]:
            check(f"{pid}-{r['spec_id']}-has-verdict",
                  bool(r["verdict"]),
                  f"{r['read_at']} → {r['verdict']}")


def main():
    print("=" * 70)
    print("daily_snapshot — pure logic (no network, no clock)")
    print("=" * 70)
    test_screen_is_imported()
    test_honesty_fields_are_data()
    test_no_measurement()
    test_staleness()
    test_display_name_tickers()
    test_summarise_hits()
    test_evidence_query_is_unbiased()
    test_research_record_matches_registries()

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, no criterion evaluated.")


if __name__ == "__main__":
    main()
