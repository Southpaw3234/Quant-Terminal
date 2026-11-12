#!/usr/bin/env python3
"""Pure-logic validation for LIBRARIAN. No network, no clock.

The class of wrongness this file exists to prevent: **a filing that is
missing from the index being reported as a filing that dropped language.**

That is not a cosmetic error. Removals are this module's headline signal, so
a filing absent from the full-text index would manufacture a removal for
every declared phrase at once -- the most confident and most wrong output
the agent could produce, and the one a reader is least equipped to doubt.

Five things must hold, and each has a test that fails loudly:

  1. the phrase list is pre-registered, and a phrase declared too late is
     REFUSED rather than quietly used
  2. absent and unknown are never merged, and `removed` is unreachable from
     an unknown
  3. no document is ever scored -- enforced on identifiers and on the shape
     of a claim, not on the prose
  4. a phrase inside an exhibit is not reported as language in the filing
  5. this module cannot decide anything: no referee, no prices, no broker
"""

from __future__ import annotations

import ast
import inspect
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import librarian as lib
import librarian_diff as agent

PASS, FAIL = [], []
PHRASE_FILE = "data/librarian/phrases.json"


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def refuses(fn, *a, **k):
    try:
        fn(*a, **k)
        return False, "accepted"
    except ValueError as e:
        return True, str(e)


def _identifiers(mod) -> set:
    """Every name bound or defined in a module. AST, not text.

    🔑 Text search would match this file's own prose about not scoring, and
    has produced a false pass in this repository four separate times.
    """
    out = set()
    for n in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _imports(mod) -> set:
    out = set()
    for n in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            for a in n.names:
                out.add(f"{n.module}.{a.name}")
    return out


def _str_literals(mod) -> list:
    return [n.value for n in ast.walk(ast.parse(inspect.getsource(mod)))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _phrase(text="going concern", when="2026-01-01"):
    return {"text": text, "declared_at": when, "why": "test"}


def _side(acc, filed, state, reason=""):
    return {"accession": acc, "filed": filed, "state": state, "reason": reason}


# ── 1. the phrase list is pre-registered ─────────────────────────────────────

def test_preregistration():
    print("\nthe phrase list is pre-registered, and late phrases are refused")

    ok, msg = refuses(lib.parse_phrases,
                      json.dumps({"phrases": [{"text": "x"}]}).encode())
    check("phrase-without-declared_at-refused", ok, msg[:94])
    check("refusal-explains-the-stake",
          "look-ahead" in msg or "checkable" in msg,
          "a check that cannot fail is not a check")

    ok, _ = refuses(lib.parse_phrases, json.dumps({"phrases": []}).encode())
    check("empty-list-refused", ok,
          "an empty brief must not read as 'nothing changed'")

    dupe = {"phrases": [_phrase("covenant"), _phrase("COVENANT")]}
    ok, _ = refuses(lib.parse_phrases, json.dumps(dupe).encode())
    check("duplicate-phrase-refused", ok, "it would be counted twice")

    raw = json.dumps({"phrases": [_phrase()]}).encode()
    p = lib.parse_phrases(raw)
    check("valid-list-accepted", len(p["phrases"]) == 1)
    check("digest-is-of-the-file-as-committed",
          p["digest"] == lib.digest_phrases(raw) and len(p["digest"]) == 64)
    other = json.dumps({"phrases": [_phrase("covenant")]}).encode()
    check("editing-the-list-changes-the-digest",
          lib.digest_phrases(other) != p["digest"],
          "a later edit is visible against briefs already published")

    # 🔑 The guard itself.
    ok, msg = refuses(lib.check_declared_before,
                      _phrase(when="2026-06-01"), "2026-06-01")
    check("phrase-declared-SAME-DAY-refused", ok, msg[:88])
    ok, _ = refuses(lib.check_declared_before,
                    _phrase(when="2026-07-01"), "2026-06-01")
    check("phrase-declared-AFTER-the-filing-refused", ok)
    lib.check_declared_before(_phrase(when="2026-05-31"), "2026-06-01")
    check("phrase-declared-BEFORE-the-filing-allowed", True,
          "declaring after the BASELINE is selecting on information that "
          "existed; declaring after the CURRENT filing is selecting on the "
          "outcome")

    # The committed list, against a filing that predates it. Must refuse.
    real = lib.parse_phrases(open(PHRASE_FILE, "rb").read())
    ok, _ = refuses(lib.check_declared_before, real["phrases"][0], "2025-08-01")
    check("committed-list-refuses-a-2025-filing", ok,
          "the guard bites on the real list and the real dates, not only on "
          "fixtures")
    check("every-committed-phrase-has-a-reason",
          all(p["why"] for p in real["phrases"]),
          "a phrase nobody can justify in advance is a phrase chosen later")


# ── 2. 🔑 absent is not unknown ──────────────────────────────────────────────

def test_absent_is_not_unknown():
    print("\nabsent and unknown are never merged")

    check("zero-hits-in-an-indexed-filing-is-absent",
          lib.state_from_hits(0, True) == lib.ABSENT)
    check("zero-hits-in-an-UNINDEXED-filing-is-unknown",
          lib.state_from_hits(0, False) == lib.UNKNOWN,
          "🔑 the impossible-zero rule: a zero that cannot be non-zero is "
          "not a measurement")
    check("hits-in-an-indexed-filing-is-present",
          lib.state_from_hits(3, True) == lib.PRESENT)
    check("not-indexed-dominates-even-with-hits",
          lib.state_from_hits(1, False) == lib.UNKNOWN,
          "contradictory input resolves toward not-knowing, never toward a "
          "finding")

    P, A, U = lib.PRESENT, lib.ABSENT, lib.UNKNOWN
    check("present-then-absent-is-removed", lib.classify(P, A) == lib.REMOVED)
    check("absent-then-present-is-added", lib.classify(A, P) == lib.ADDED)
    check("present-both-is-retained", lib.classify(P, P) == lib.RETAINED)
    check("absent-both-is-neither", lib.classify(A, A) == lib.NEVER)

    # 🔑 Every path through an unknown.
    for prev, curr, label in ((U, A, "unknown-then-absent"),
                              (U, P, "unknown-then-present"),
                              (P, U, "present-then-unknown"),
                              (A, U, "absent-then-unknown"),
                              (U, U, "unknown-both")):
        check(f"{label}-is-indeterminate",
              lib.classify(prev, curr) == lib.INDETERMINATE)
    check("removed-is-unreachable-from-an-unknown",
          lib.classify(U, A) != lib.REMOVED,
          "🔑 a filing missing from the index would otherwise manufacture a "
          "removal for every declared phrase at once")

    c = lib.claim("GRPN", 1, "10-Q", _phrase(),
                  _side("a", "2026-02-01", U, "not in the index"),
                  _side("b", "2026-05-01", A))
    check("indeterminate-claim-is-not-notable", not c["notable"],
          "it must not reach a human as a finding")
    check("indeterminate-claim-keeps-the-reason",
          "index" in c["baseline"]["reason"])

    s = lib.summarise([c])
    check("indeterminate-is-counted-separately", s[lib.INDETERMINATE] == 1
          and s[lib.REMOVED] == 0)
    d = lib.digest_text({"day": "2026-05-02", "claims": [c], "summary": s,
                         "phrases": {"digest": "f" * 64, "count": 1}})
    check("digest-warns-about-indeterminate", "INDETERMINATE" in d)

    # 🔑 The same conflation, at the one place it would reach a human: a
    # brief empty because nothing was TESTABLE vs empty because nothing
    # CHANGED.
    empty = {"day": "2026-05-02", "claims": [], "summary": lib.summarise([]),
             "pairs_compared": 3, "phrases": {"digest": "f" * 64, "count": 10},
             "refused_lookahead": [{"phrase": "x"}] * 30}
    de = lib.digest_text(empty)
    check("all-refused-brief-says-nothing-was-TESTABLE",
          "not" in de.lower() and "nothing changed" in de.lower(),
          "30 refusals and 0 tests must not print as 'no phrase changed'")
    check("all-refused-brief-counts-the-refusals", "30 phrase test(s)" in de)
    check("all-refused-brief-does-not-claim-nothing-changed",
          "No declared phrase changed state" not in de)
    quiet = dict(empty, refused_lookahead=[])
    check("genuinely-empty-brief-says-no-pair-was-available",
          "No filing pair" in lib.digest_text(quiet),
          "zero claims with zero refusals is a third, different fact")
    check("digest-states-it-is-not-a-section-diff",
          "not a section diff" in d.lower(),
          "every output says what the instrument is")


# ── 2b. 🔑 "no baseline" has causes, and they are not merged ─────────────────

def test_baseline_causes():
    print("\na missing baseline has causes, and they are kept apart")
    rows = [{"form": "10-Q", "accession": "c", "filed": "2026-05-01"},
            {"form": "10-Q", "accession": "b", "filed": "2026-02-01"},
            {"form": "10-K", "accession": "k", "filed": "2025-12-01"},
            {"form": "10-Q", "accession": "a", "filed": "2025-11-01"}]

    g = lib.baseline_from_history(rows, "10-Q", "c")
    check("baseline-is-the-filing-immediately-before",
          g["ok"] and g["accession"] == "b" and g["filed"] == "2026-02-01")
    check("other-forms-are-not-a-baseline",
          lib.baseline_from_history(rows, "10-Q", "b")["accession"] == "a",
          "a 10-K filed in between is not the previous 10-Q")

    e = lib.baseline_from_history(rows, "10-Q", "a")
    check("earliest-in-window-is-not-a-first-filing",
          not e["ok"] and "window limit" in e["reason"],
          "an older filing may exist outside the history window")

    # 🔑 The guess this function exists to refuse.
    u = lib.baseline_from_history(rows, "10-Q", "not-in-history")
    check("unlocatable-current-filing-is-REFUSED", not u["ok"],
          "taking the second-newest filing instead would be a guess wearing "
          "the clothes of a lookup")
    check("refusal-says-it-is-refusing-to-assume",
          "refusing to assume" in u["reason"])

    n = lib.baseline_from_history(rows, "10-Q/A", "x")
    check("no-filings-of-the-form-is-its-own-reason",
          not n["ok"] and "no 10-Q/A" in n["reason"])
    check("empty-history-does-not-crash",
          not lib.baseline_from_history([], "10-Q", "x")["ok"])

    src = inspect.getsource(agent.build)
    check("failed-lookup-and-absent-history-are-separate-buckets",
          "unavailable if prev[\"failed_lookup\"] else no_baseline" in src,
          "merging them would let an outage report itself as 'this company "
          "has no prior filing'")
    check("an-all-unavailable-funnel-refuses-to-publish",
          "sys.exit(5)" in inspect.getsource(agent.main),
          "🔑 'no language changed' and 'we could not look' must never be "
          "the same output — specification 4's rehearsal was stopped for "
          "exactly this shape")


def test_selection_rule():
    print("\nthe cap does not quietly become a census of the alphabet")
    cand = {("AACO", "10-Q"): (1, "x", "2026-11-10"),
            ("AACOU", "10-Q"): (1, "x", "2026-11-10"),
            ("AACOW", "10-Q"): (1, "x", "2026-11-10"),
            ("ZZZZ", "10-Q"): (2, "y", "2026-11-09"),
            ("GRPN", "10-Q"): (3, "z", "2026-11-05")}
    picked, trunc, issuers, mine = agent.select(cand, set(), 2)
    check("share-classes-of-one-cik-count-once", issuers == 3,
          "EFTS display_names lists units and warrants as separate tickers")
    check("common-share-class-is-the-one-kept",
          "AACO" in {p[0] for p in agent.select(cand, set(), 9)[0]}
          and "AACOU" not in {p[0] for p in agent.select(cand, set(), 9)[0]})
    check("cap-is-reported-as-truncation", trunc and len(picked) == 2)
    check("cap-is-not-alphabetical",
          {p[0] for p in picked} != {"AACO", "GRPN"},
          f"took {sorted(p[0] for p in picked)} — most recent first, so a "
          f"cap is not a census of the start of the alphabet")

    # 🔑 An open thesis is never dropped by the cap.
    picked2, _, _, mine2 = agent.select(cand, {"GRPN"}, 1)
    check("open-thesis-name-survives-the-cap",
          [p[0] for p in picked2] == ["GRPN"] and mine2 == 1,
          "it is the oldest filing here and would otherwise be dropped — "
          "the thesis is the reason to care about the name")


# ── 3. 🔑 no document is ever scored ─────────────────────────────────────────

def test_nothing_is_scored():
    print("\nno document is ever scored")
    ids = _identifiers(lib) | _identifiers(agent)
    for banned in ("score", "rank", "weight", "rating", "grade", "priority",
                   "confidence", "severity"):
        hit = sorted(i for i in ids if banned in i.lower())
        check(f"no-{banned}-identifier", not hit,
              "the brief forbids a numeric score on a document"
              + (f" — found {hit}" if hit else ""))

    # 🔑 The shape of a claim, not the prose about it. A claim carries no
    # number a reader could sort filings by.
    c = lib.claim("GRPN", 42, "10-Q", _phrase(),
                  _side("a", "2026-02-01", lib.PRESENT),
                  _side("b", "2026-05-01", lib.ABSENT))
    numeric = sorted(k for k, v in c.items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool)
                     and k != "cik")
    check("claim-carries-no-number-but-the-cik", not numeric,
          f"found {numeric}" if numeric else
          "nothing in a claim can be sorted as a magnitude")
    check("claim-is-removed-and-notable",
          c["transition"] == lib.REMOVED and c["notable"])

    # Ordering is by KIND. Removals first, then additions, then names.
    added = lib.claim("AAPL", 1, "10-Q", _phrase("covenant"),
                      _side("a", "2026-02-01", lib.ABSENT),
                      _side("b", "2026-05-01", lib.PRESENT))
    order = [x["transition"] for x in lib.notable([added, c])]
    check("removals-are-listed-before-additions",
          order == [lib.REMOVED, lib.ADDED],
          "ordered by kind because there is no magnitude here to order by")
    check("retained-is-not-notable",
          not lib.claim("X", 1, "10-Q", _phrase(),
                        _side("a", "2026-02-01", lib.PRESENT),
                        _side("b", "2026-05-01", lib.PRESENT))["notable"])


# ── 4. section, exhibits, and honest linkage ─────────────────────────────────

def test_section_and_exhibits():
    print("\nsection is not observed, and exhibits are not the filing")
    c = lib.claim("GRPN", 1, "10-Q", _phrase(),
                  _side("a", "2026-02-01", lib.PRESENT),
                  _side("b", "2026-05-01", lib.ABSENT))
    check("section-is-None", c["section"] is None,
          "None means NOT OBSERVED — never 'no section'")
    check("section-says-why-it-is-unavailable",
          "403" in c["section_source"],
          "a null with no reason is indistinguishable from a bug")

    # 🔑 The exhibit guard. EFTS indexes documents, not filings.
    res = {"hits": [
        {"_id": "0000320193-25-000008:aapl-20241228.htm",
         "_source": {"file_type": "10-Q"}},
        {"_id": "0000320193-25-000008:a10-qexhibit10112282024.htm",
         "_source": {"file_type": "EX-10.1"}},
    ]}
    got = agent.primary_accessions(res, "10-Q")
    check("primary-document-is-matched", got == {"0000320193-25-000008"})
    check("exhibit-is-not-the-filing",
          agent.primary_accessions(
              {"hits": [res["hits"][1]]}, "10-Q") == set(),
          "language in EX-10.1 is not language in the 10-Q")
    check("primary-match-is-by-file_type-not-position",
          agent.primary_accessions({"hits": list(reversed(res["hits"]))},
                                   "10-Q") == got)

    c2 = lib.claim("GRPN", 1, "10-Q", _phrase(),
                   _side("a", "2026-02-01", lib.PRESENT),
                   _side("b", "2026-05-01", lib.ABSENT),
                   observation_ids=["2026-05-01:b"])
    check("claim-links-to-its-observations", c2["observation_ids"] == ["2026-05-01:b"]
          and c2["observed_by_observer"])
    check("unlinked-claim-says-so-rather-than-implying-none-exist",
          c["observation_ids"] == [] and c["observed_by_observer"] is False,
          "OBSERVER watches 8-K and Form 4, not 10-Q — the empty list is a "
          "fact about coverage, not about the filing")


# ── 5. 🔑 this module cannot decide ──────────────────────────────────────────

def test_cannot_decide():
    print("\nLIBRARIAN structures language; it cannot decide or spend a read")
    imported = _imports(agent) | _imports(lib)
    for m in ("qt.referee", "qt.wrc", "qt.calendar_time", "qt.prices",
              "yfinance", "alpaca", "alpaca_trade_api", "requests"):
        check(f"does-not-import-{m}", m not in imported,
              "collecting and structuring are free; measuring is scarce")

    roots = {m.split(".")[0] for m in _imports(lib)}
    check("pure-logic-module-is-stdlib-only",
          roots <= {"__future__", "hashlib", "json"},
          f"{sorted(roots)} — no network and no pandas means the suite "
          f"cannot reach anything, which is what makes 'this module cannot "
          f"decide' a demonstrated property rather than a promise")

    # 🔑 Host guard on URL LITERALS only. The prose in both modules discusses
    # www.sec.gov at length; a text search would match the comment that
    # explains the ban. This matches only strings that are URLs.
    bad = [s for s in _str_literals(agent) + _str_literals(lib)
           if s.startswith("http") and "www.sec.gov" in s]
    check("fetches-no-url-on-www-sec-gov", not bad,
          f"found {bad}" if bad else
          "that host answers 403; only efts.sec.gov and data.sec.gov answer")

    check("ledger-is-read-only-if-its-chain-verifies",
          "verify" in inspect.getsource(agent.thesis_tickers),
          "widening the watch list from an altered ledger would bless the "
          "alteration")
    check("empty-universe-is-refused-not-reported",
          "sys.exit(2)" in inspect.getsource(agent.main),
          "an empty watch list must not read as 'no language changed'")


def main():
    print("=" * 70)
    print("LIBRARIAN — pure logic (no network, no clock)")
    print("=" * 70)
    test_preregistration()
    test_absent_is_not_unknown()
    test_baseline_causes()
    test_selection_rule()
    test_nothing_is_scored()
    test_section_and_exhibits()
    test_cannot_decide()

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, no document scored.")


if __name__ == "__main__":
    main()
