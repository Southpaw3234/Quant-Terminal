#!/usr/bin/env python3
"""LIBRARIAN -- module 3 of the collective agent. Filter and structure.

See docs/COLLECTIVE_AGENT_BRIEF.md 3.2 and the long header in qt/librarian.py,
which records what this module can and cannot observe and why.

    OBSERVER's records for the newest filing day
      ->  10-Q / 10-K filings in the watched universe
      ->  each name's PRIOR filing of the same form
      ->  for every PRE-DECLARED phrase: present then, present now?
      ->  and, for every open thesis: which observations bear on it
      ->  briefs/<filing day>.json + .md

-----------------------------------------------------------------------------
🔑 IT READS OBSERVER, IT DOES NOT RE-COLLECT

The first version ran its own five-day scan of EDGAR every night. That was
wrong three ways: it duplicated OBSERVER's job, it re-tested the same
filings five nights running, and it discarded the `truncated` flag, so a
capped scan was reported as a census. The brief describes the LIBRARIAN as
deciding "which observations bear on an open thesis or a watched name" --
observations, the thing OBSERVER already produced. So that is the input,
and a brief is named for the FILING DAY it describes, not the day it ran.

If OBSERVER produced nothing, this module says so and stops. It does not
quietly fall back to collecting for itself: a missing upstream is a fact
the operator needs, not a gap to paper over.

-----------------------------------------------------------------------------
🔑 THIS IS NOT A SECTION DIFF, AND IT SAYS SO IN EVERY OUTPUT

The brief asks for a risk-factor and MD&A diff. That needs the filing body,
served from `www.sec.gov/Archives/`, which answers 403 to this client.
What is reachable is EDGAR full-text search, which answers one narrow
question exactly: does this phrase appear in this filing's primary document?
So the instrument is a phrase-presence diff over a pre-declared phrase list.

-----------------------------------------------------------------------------
🔑 WHY A PHRASE MAY BE REFUSED, AND WHY THAT IS THE FEATURE

A phrase is testable against a filing only if it was declared BEFORE that
filing was published. The line is drawn at the CURRENT filing, not the
baseline: declaring after the baseline is selecting on information that
existed; declaring after the current filing is selecting on the OUTCOME.
Refusals are COUNTED and reported, never skipped quietly.
"""

from __future__ import annotations

import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import agentio as io
from qt import edgar
from qt import librarian as lib

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time,
# qt.prices, and any broker SDK. LIBRARIAN structures language; it cannot
# spend a read, score a document, evaluate a criterion or place an order.
# validate_librarian_diff.py asserts this with `ast`.

OUT_DIR = os.environ.get("QT_LIB_DIR", "briefs")
PHRASES = os.environ.get("QT_LIB_PHRASES", "data/librarian/phrases.json")
LEDGER = os.environ.get("QT_THESIS_LEDGER", "data/theses/ledger.jsonl")
OBS_DIR = os.environ.get("QT_OBS_DIR", "observations")
FORMS = ("10-Q", "10-K")
MAX_NAMES = int(os.environ.get("QT_LIB_MAX_NAMES", "60"))
BEARING_FILES = int(os.environ.get("QT_LIB_BEARING_FILES", "5"))
OBS_MAX_AGE_DAYS = float(os.environ.get("QT_LIB_OBS_MAX_AGE", "4"))
SLEEP = 0.12
# Words on the cover page of every 10-Q and 10-K. If a filing's primary
# document does not match THIS, its text is not searchable, and no other
# phrase's absence from it means anything.
CONTROL_PHRASE = "Securities Exchange Act of 1934"


# -- reading OBSERVER ---------------------------------------------------------

def candidates_from_observations(records: list) -> dict:
    """{(ticker, form): (cik, accession, filed)} from one day's records.

    Original filings only. An amendment (10-Q/A) restates a filing that
    already had its own day; diffing it against "the previous 10-Q" would
    compare a document with an earlier version of itself.
    """
    out = {}
    for r in records or []:
        if r.get("kind", "filing") != "filing":
            continue
        form = r.get("form")
        if form not in FORMS or r.get("root_form", form) != form:
            continue
        if not r.get("ticker") or r.get("cik") is None or not r.get("accession"):
            continue
        out.setdefault((r["ticker"], form),
                       (int(r["cik"]), r["accession"], r.get("filed")))
    return out


def bearing_on_theses(theses: list, recent: dict) -> list:
    """For each open thesis, the observations that name its ticker.

    `recent` is {day: [records]}. This selects by TICKER and nothing else --
    it does not judge whether a filing matters. Deciding that an 8-K bears
    on a falsifier is the operator's reading; surfacing that the 8-K exists
    is this agent's job.
    """
    out = []
    for t in theses or []:
        tk = (t.get("ticker") or "").strip().upper()
        hits = []
        for day in sorted(recent, reverse=True):
            for r in recent[day]:
                if tk and r.get("ticker") == tk:
                    hits.append({
                        "id": r.get("id"), "day": day,
                        "kind": r.get("kind", "filing"),
                        "form": r.get("form"), "items": r.get("items") or [],
                        "direction": r.get("direction"),
                        "event_type": r.get("event_type"),
                        "url": r.get("url")})
        out.append({"thesis_id": t.get("thesis_id"), "ticker": tk or None,
                    "review_date": t.get("review_date"),
                    "observations": hits,
                    "days_searched": sorted(recent)})
    return out


# -- EDGAR full-text search, the only reachable instrument --------------------

def _fts(sess, **params) -> dict:
    """One full-text query. -> {"ok", "total", "hits", "reason"}. Never raises.

    🔑 A query that could not be answered returns ok=False, and every caller
    turns that into `unknown` -- never into "no hits". The first live run
    crashed on one transient 500 after 600 requests; the worse outcome would
    have been to swallow it and report the phrase as absent.
    """
    p = {"q": "", "from": 0, "size": edgar.EFTS_PAGE}
    p.update(params)
    got = edgar.fetch_json(sess, edgar.EFTS_URL, p)
    if not got["ok"]:
        return {"ok": False, "total": 0, "hits": [], "reason": got["reason"]}
    hits = (got["json"].get("hits") or {})
    return {"ok": True, "reason": "ok",
            "total": ((hits.get("total") or {}).get("value") or 0),
            "hits": hits.get("hits") or []}


def primary_accessions(res: dict, form: str) -> set:
    """Accessions whose PRIMARY document matched.

    🔑 The exhibit guard. A phrase query matches documents, so a phrase
    inside EX-10.1 would otherwise be reported as language in the 10-Q. The
    primary document's `file_type` equals the form; an exhibit's does not.
    """
    out = set()
    for h in res["hits"]:
        src = h.get("_source") or {}
        if (src.get("file_type") or "") == form:
            out.add(edgar.accession_of(h))
    return out


def indexed_accessions(sess, cik: int, form: str, start: str, end: str) -> set:
    """🔑 Which of this company's filings have SEARCHABLE TEXT.

    Asked with a control phrase that every 10-Q and 10-K carries on its
    cover page. Without it, zero hits for a phrase and a filing whose text
    is not in the index are the same observation -- and one of them would
    manufacture a removal for every declared phrase at once.

    The first version asked with an empty query, which proves only that the
    filing's METADATA is listed. A filing published this afternoon can be
    listed before its text is searchable, and "listed" would then have
    vouched for every absence found in it. The control phrase tests the
    thing that actually matters: can a phrase be found in this document at
    all. (On the first live run all 114 filings passed both checks; the
    difference is which failure the guard can see.)

    A FAILED query returns the empty set, which makes both filings
    `unknown`: not knowing whether a filing is searchable is treated
    exactly like knowing it is not.
    """
    res = _fts(sess, q=f'"{CONTROL_PHRASE}"', forms=form,
               ciks=f"{int(cik):010d}", startdt=start, enddt=end)
    return primary_accessions(res, form) if res["ok"] else set()


def phrase_accessions(sess, cik: int, form: str, phrase: str,
                      start: str, end: str) -> "set | None":
    """Accessions whose primary document contains `phrase` (exact phrase).

    🔑 None when the query FAILED. None is not the empty set: an empty set
    says the phrase is in neither filing, None says nobody could look.
    """
    res = _fts(sess, q=f'"{phrase}"', forms=form, ciks=f"{int(cik):010d}",
               startdt=start, enddt=end)
    return primary_accessions(res, form) if res["ok"] else None


def prior_filing(sess, cik: int, form: str, before_acc: str) -> dict:
    """The company's previous filing of the same form.

    Read from data.sec.gov, which answers, rather than from the browse-edgar
    pages on www.sec.gov, which do not. A FAILED lookup is reported as
    failure, never as "this company has no prior filing".
    """
    h = edgar.filing_history(sess, cik)
    if not h["ok"]:
        return {"ok": False, "accession": "", "filed": "",
                "failed_lookup": True,
                "reason": f"filing history unavailable: {h['reason']}"}
    out = lib.baseline_from_history(h["rows"], form, before_acc)
    out["failed_lookup"] = False
    return out


def select(candidates: dict, thesis: set, cap: int) -> tuple:
    """Which filings to read, and the rule, stated.

    🔑 Two defects this exists to avoid.

    One: a filing can list every share class, so a SPAC's units and warrants
    arrive as three tickers on one CIK. Deduped on (CIK, form) -- otherwise
    a cap spends itself three times on one company.

    Two: an alphabetical cap is a census of the start of the alphabet. Names
    carrying an OPEN THESIS are taken first, because they are the reason to
    care; the remainder is taken most-recently-filed first, which is
    deterministic without being sorted by name.
    """
    by_issuer = {}
    for (tk, form), (cik, acc, filed) in candidates.items():
        key = (cik, form)
        prev = by_issuer.get(key)
        # A thesis name always wins its CIK; otherwise the shortest ticker,
        # which is the common share class rather than the unit or warrant.
        better = (prev is None or (tk in thesis and prev[0] not in thesis)
                  or (len(tk) < len(prev[0])
                      and not (prev[0] in thesis and tk not in thesis)))
        if better:
            by_issuer[key] = (tk, cik, acc, filed, form)
    rows = list(by_issuer.values())
    mine = sorted((r for r in rows if r[0] in thesis),
                  key=lambda r: (r[3] or "", r[0]), reverse=True)
    rest = sorted((r for r in rows if r[0] not in thesis),
                  key=lambda r: (r[3] or "", r[0]), reverse=True)
    ordered = mine + rest
    return ordered[:cap], len(ordered) > cap, len(rows), len(mine)


# -- the run ------------------------------------------------------------------

def diff_filings(sess, phrases: dict, picked: list) -> dict:
    """The network half: one baseline lookup and one phrase pass per issuer."""
    claims, refused, pairs = [], [], 0
    no_baseline, unavailable = [], []
    for tk, cik, acc, filed, form in picked:
        prev = prior_filing(sess, cik, form, acc)
        if not prev["ok"]:
            # 🔑 A failed lookup and a genuine absence of history are kept in
            # separate buckets. Merging them would let an outage report
            # itself as "this company has no prior filing".
            row = {"ticker": tk, "form": form, "filed": filed,
                   "reason": prev["reason"]}
            (unavailable if prev["failed_lookup"] else no_baseline).append(row)
            continue
        prev_acc, prev_filed = prev["accession"], prev["filed"]
        pairs += 1
        start, end = min(prev_filed, filed), max(prev_filed, filed)
        idx = indexed_accessions(sess, cik, form, start, end)
        print(f"[librarian] {tk:<6} {form}  {prev_filed} -> {filed}  "
              f"text searchable {len(idx & {prev_acc, acc})}/2")

        for ph in phrases["phrases"]:
            try:
                lib.check_declared_before(ph, filed)
            except ValueError as e:
                refused.append({"ticker": tk, "form": form,
                                "phrase": ph["text"],
                                "declared_at": ph["declared_at"],
                                "filed": filed, "reason": str(e)})
                continue
            hit = phrase_accessions(sess, cik, form, ph["text"], start, end)
            sides = {}
            for label, a, f in (("prev", prev_acc, prev_filed),
                                ("curr", acc, filed)):
                # A failed phrase query makes the state unknown, exactly as
                # an unindexed filing does. It must never read as "absent".
                ok = a in idx and hit is not None
                sides[label] = {
                    "accession": a, "filed": f,
                    "state": lib.state_from_hits(
                        1 if hit and a in hit else 0, ok),
                    "reason": "" if ok else (
                        "the phrase query failed after retries"
                        if hit is None else
                        "the control phrase was not found in this filing, "
                        "so its text is not searchable in the index"),
                }
            # The current filing IS an OBSERVER record: that is where the
            # candidate came from, so the link is by construction.
            claims.append(lib.claim(tk, cik, form, ph, sides["prev"],
                                    sides["curr"], [f"edgar:{acc}"]))
    return {"claims": claims, "refused": refused, "pairs": pairs,
            "no_baseline": no_baseline, "unavailable": unavailable}


def main() -> None:
    now = io.now_utc()
    raw = open(PHRASES, "rb").read()
    phrases = lib.parse_phrases(raw)
    print(f"[librarian] {len(phrases['phrases'])} declared phrase(s), "
          f"list {phrases['digest'][:16]}…")

    # 🔑 The upstream check comes first. No observations is OBSERVER being
    # down, and it must read as that -- not as "no filings today".
    files = io.day_files(OBS_DIR, ".jsonl")
    if not files:
        print(f"[librarian] REFUSING TO RUN — no observation files in "
              f"{OBS_DIR}/. OBSERVER has not produced anything to read.")
        print("::error::librarian: OBSERVER produced no observations; a "
              "brief built on nothing would read as 'no filings changed'")
        sys.exit(6)
    day, path = files[-1]
    meta = io.read_json(os.path.join(OBS_DIR, f"{day}.meta.json"))
    upstream = io.artifact_status(
        "observations", meta or {"filing_day": day},
        (meta or {}).get("generated_at") or day, OBS_MAX_AGE_DAYS, now)
    records = io.read_jsonl(path)
    print(f"[librarian] reading OBSERVER's {day}: {len(records)} record(s) "
          f"— {upstream['status']}")
    if not upstream["usable"]:
        print(f"[librarian] REFUSING TO RUN — {upstream['why']}")
        print(f"::error::librarian: {upstream['why']}")
        sys.exit(6)

    th = io.open_theses(LEDGER)
    if not th["ok"]:
        print(f"[librarian] ⚠️  thesis ledger unusable — {th['why']}. "
              f"No thesis is read from it.")
    thesis = {s["ticker"].strip().upper() for s in th["theses"]
              if s.get("ticker")}

    candidates = candidates_from_observations(records)
    picked, truncated, n_issuers, n_mine = select(candidates, thesis, MAX_NAMES)
    print(f"[librarian] {len(candidates)} {'/'.join(FORMS)} filing(s) "
          f"observed on {day} -> {n_issuers} issuer(s) -> reading "
          f"{len(picked)} ({n_mine} with an open thesis)")

    sess = edgar.session(os.environ.get("SEC_USER_AGENT", ""))
    d = diff_filings(sess, phrases, picked)

    recent = {dd: io.read_jsonl(pp) for dd, pp in files[-BEARING_FILES:]}
    brief = {
        "generated_at": now,
        "day": day,
        "describes": f"filings published {day}",
        "source": "OBSERVER records + sec-edgar-fts",
        "upstream": upstream,
        "instrument": "phrase-presence diff over the primary document",
        "not_a_section_diff": (
            "Section attribution (Item 1A vs Item 7) is NOT OBSERVED: filing "
            "bodies are served from www.sec.gov, which answers 403 to this "
            "client. Only pre-declared phrases are tested, so language "
            "nobody declared in advance is invisible here."),
        "phrases": {"digest": phrases["digest"],
                    "count": len(phrases["phrases"]),
                    "note": phrases["note"]},
        "issuers_eligible": n_issuers,
        "issuers_read": len(picked),
        "with_open_thesis": n_mine,
        "pairs_compared": d["pairs"],
        "names_truncated": truncated,
        "selection_rule": ("deduped on (CIK, form) so share classes count "
                           "once; open-thesis names first, then most "
                           "recently filed. Not alphabetical."),
        "max_names": MAX_NAMES,
        "no_baseline": d["no_baseline"],
        # 🔑 Kept apart from no_baseline on purpose: an outage must not be
        # able to report itself as an absence of filing history.
        "baseline_unavailable": d["unavailable"],
        # 🔑 Counted, not skipped. The cost of declaring late is visible.
        "refused_lookahead": d["refused"],
        "claims": d["claims"],
        "summary": lib.summarise(d["claims"]),
        "ledger": {"ok": th["ok"], "why": th["why"]},
        "bearing_on_theses": bearing_on_theses(th["theses"], recent),
    }

    # 🔑 REFUSE TO PUBLISH A FUNNEL THAT ENDED IN NOTHING FOR A REASON WE
    # CANNOT SEE. If every filing we picked lost its baseline to a failed
    # lookup, then "no language changed" is not a finding, it is an outage
    # wearing a finding's clothes.
    unavail, read = len(brief["baseline_unavailable"]), brief["issuers_read"]
    if read and brief["pairs_compared"] == 0 and unavail == read:
        print("\n[librarian] REFUSING TO PUBLISH THIS BRIEF")
        print(f"::error::not one of {read} filing(s) kept a baseline — all "
              f"{unavail} lost it to a FAILED LOOKUP, not to an absence of "
              f"filing history. An empty brief here would read as 'no "
              f"language changed' when the truth is 'we could not look'.")
        for r in brief["baseline_unavailable"][:5]:
            print(f"  {r['ticker']:<6} {r['form']}  {r['reason']}")
        sys.exit(5)

    jpath = os.path.join(OUT_DIR, f"{day}.json")
    io.write_json(jpath, brief)
    text = lib.digest_text(brief)
    io.write_text(os.path.join(OUT_DIR, f"{day}.md"), text)

    s = brief["summary"]
    print(f"\n[librarian] wrote {jpath}")
    print(f"  pairs compared      {brief['pairs_compared']}")
    print(f"  claims              {s['claims']} over {s['names']} name(s)")
    print(f"  removed             {s[lib.REMOVED]}")
    print(f"  added               {s[lib.ADDED]}")
    print(f"  retained            {s[lib.RETAINED]}")
    print(f"  indeterminate       {s[lib.INDETERMINATE]}  "
          f"(filing not in the index — NOT a removal)")
    print(f"  refused look-ahead  {len(brief['refused_lookahead'])}  "
          f"(phrase declared on or after the filing)")
    print(f"  no baseline         {len(brief['no_baseline'])}  "
          f"(genuinely no prior filing, or outside the history window)")
    print(f"  baseline UNAVAIL    {unavail}  "
          f"(the lookup failed — NOT an absence of history)")
    print(f"  open theses         {len(brief['bearing_on_theses'])}")
    print("\n" + text[:1400])
    print("\n[librarian] structured only. No document scored, nothing "
          "recommended, no order placed, K unchanged.")


if __name__ == "__main__":
    main()
