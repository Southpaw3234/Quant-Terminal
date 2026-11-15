#!/usr/bin/env python3
"""LIBRARIAN -- module 3 of the collective agent. Filing language, diffed.

See docs/COLLECTIVE_AGENT_BRIEF.md 3.2 and the long header in qt/librarian.py,
which records what this module can and cannot observe and why.

    recent 10-Q / 10-K filers  ->  intersect the watched universe
    ->  find each name's PRIOR filing of the same form
    ->  for every PRE-DECLARED phrase, present then and present now?
    ->  briefs/YYYY-MM-DD.json  ->  digest -> Discord

-----------------------------------------------------------------------------
🔑 THIS IS NOT A SECTION DIFF, AND IT SAYS SO IN EVERY OUTPUT

The brief asks for a risk-factor and MD&A diff. That needs the filing body,
and filing bodies are served from `www.sec.gov/Archives/`, which answers 403
to this client -- re-measured, not inherited: the 403 page is titled
"Request Rate Threshold Exceeded" and arrives on the FIRST request of a cold
session, so backing off does not clear it.

What is reachable is EDGAR full-text search, which can answer one narrow
question exactly: does this phrase appear in this filing's primary document?
So the instrument is a phrase-presence diff over a pre-declared phrase list.
Narrower than the brief asked for, and honest about which.

-----------------------------------------------------------------------------
🔑 WHY A PHRASE MAY BE REFUSED, AND WHY THAT IS THE FEATURE

A phrase is testable against a filing only if it was declared BEFORE that
filing was published. Otherwise the phrase could have been chosen by reading
the document whose change it is about to "discover".

The line is drawn at the CURRENT filing, not the baseline, and that is a
deliberate distinction:

  * declaring after the BASELINE filing is selecting on information that
    existed when you declared. Legitimate -- you know the phrase was there;
    you do not know whether it will leave.
  * declaring after the CURRENT filing is selecting on the OUTCOME. That is
    the thing this repository exists to prevent.

Refusals are COUNTED and reported, never skipped quietly, so the cost of
having declared late is visible in the brief rather than invisible.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys
import time
import urllib.parse as up

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import edgar
from qt import librarian as lib
from qt import tickers as qtk

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time,
# qt.prices, and any broker SDK. LIBRARIAN structures language; it cannot
# spend a read, score a document, evaluate a criterion or place an order.
# validate_librarian_diff.py asserts this with `ast`.

OUT_DIR = os.environ.get("QT_LIB_DIR", "briefs")
PHRASES = os.environ.get("QT_LIB_PHRASES", "data/librarian/phrases.json")
UNIVERSE = os.environ.get("QT_LIB_UNIVERSE", "")
PANEL = os.environ.get("QT_LIB_PANEL", "data/universe/v29_universe_pit.csv")
LEDGER = os.environ.get("QT_THESIS_LEDGER", "data/theses/ledger.jsonl")
OBS_DIR = os.environ.get("QT_OBS_DIR", "observations")
FORMS = ("10-Q", "10-K")
LOOKBACK = int(os.environ.get("QT_LIB_LOOKBACK", "5"))
MAX_NAMES = int(os.environ.get("QT_LIB_MAX_NAMES", "25"))
SLEEP = 0.12


# -- who we are reading -------------------------------------------------------

def watched() -> set:
    """Names in the universe, WIDENED by every open thesis.

    A name carrying an open thesis is read even if it has dropped out of the
    screen: the thesis is the reason to care, and a position does not stop
    mattering because a liquidity filter moved.
    """
    out = set()
    if UNIVERSE and os.path.exists(UNIVERSE):
        try:
            d = json.loads(open(UNIVERSE, encoding="utf-8").read())
            out |= {str(t).strip().upper()
                    for t in ((d.get("universe") or {}).get("eligible_tickers")
                              or []) if str(t).strip()}
        except (OSError, ValueError):
            pass
    if not out and os.path.exists(PANEL):
        df = qtk.read_csv(PANEL, usecols=["ticker"], dtype={"ticker": str})
        out |= set(qtk.normalise(df["ticker"]).tolist())
    out |= thesis_tickers()
    return {t for t in out if t}


def thesis_tickers() -> set:
    """Tickers with an OPEN thesis, read straight from the ledger."""
    try:
        from qt import thesis as th
    except ImportError:
        return set()
    entries = th.load(LEDGER)
    if not th.verify(entries)["ok"]:
        # Do not silently read a ledger whose history does not verify.
        print("[librarian] ⚠️  thesis ledger chain does not verify — not "
              "widening the watch list from it")
        return set()
    return {s["ticker"].strip().upper()
            for s in th.state(entries).values()
            if s["status"] == "open" and s.get("ticker")}


def observation_index() -> dict:
    """accession -> [observation ids] from OBSERVER's records.

    OBSERVER (module 1) watches 8-K and Form 4, NOT 10-Q/10-K, so a 10-Q
    accession will usually not appear here. That is expected: this links a
    claim to an observation WHEN one exists, and `observed_by_observer`
    reports plainly when none does. It is not a claim that one should.
    """
    out = {}
    for path in sorted(glob.glob(os.path.join(OBS_DIR, "*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    acc = r.get("accession")
                    if acc:
                        out.setdefault(acc, []).append(
                            f"{os.path.basename(path)[:-6]}:{acc}")
        except (OSError, ValueError):
            continue
    return out


# -- EDGAR full-text search, the only reachable instrument --------------------

def _fts(sess, **params) -> dict:
    p = {"q": "", "from": 0, "size": edgar.EFTS_PAGE}
    p.update(params)
    r = sess.get(edgar.EFTS_URL, timeout=60, params=p)
    r.raise_for_status()
    time.sleep(SLEEP)
    hits = ((r.json() or {}).get("hits") or {})
    return {"total": ((hits.get("total") or {}).get("value") or 0),
            "hits": hits.get("hits") or []}


def primary_accessions(res: dict, form: str) -> set:
    """Accessions whose PRIMARY document matched.

    🔑 The exhibit guard. EFTS indexes documents, not filings, so a phrase
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
    """🔑 Which of this company's filings are in the index AT ALL.

    Empty `q`, so this is a census of the window rather than a sample biased
    by a keyword. Without this, zero hits for a phrase and a filing missing
    from the index are the same observation -- and one of them would
    manufacture a removal for every declared phrase at once.
    """
    return primary_accessions(
        _fts(sess, forms=form, ciks=f"{int(cik):010d}",
             startdt=start, enddt=end), form)


def phrase_accessions(sess, cik: int, form: str, phrase: str,
                      start: str, end: str) -> set:
    """Accessions whose primary document contains `phrase` (exact phrase)."""
    return primary_accessions(
        _fts(sess, q=f'"{phrase}"', forms=form, ciks=f"{int(cik):010d}",
             startdt=start, enddt=end), form)


def recent_filers(sess, form: str, days: int) -> dict:
    """{ticker: (cik, accession, filed)} for recent filings of one form."""
    out = {}
    today = dt.date.today()
    for back in range(days):
        day = today - dt.timedelta(days=back)
        res = edgar.efts_day(sess, form, day, 12)
        for acc, hit in edgar.dedupe_filings(res["hits"]).items():
            src = hit.get("_source") or {}
            if (src.get("file_type") or "") != form:
                continue
            tks = edgar.tickers_from_display_names(src.get("display_names"))
            ciks = src.get("ciks") or []
            if not tks or not ciks:
                continue
            for t in tks:
                out.setdefault(t, (int(ciks[0]), acc, src.get("file_date")))
    return out


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


# -- the run ------------------------------------------------------------------

def select(candidates: dict, thesis: set, cap: int) -> tuple:
    """Which filings to read, and the rule, stated.

    🔑 Two defects this exists to avoid.

    One: EFTS `display_names` lists every share class, so a SPAC's units and
    warrants arrive as three tickers on one CIK. Deduped on (CIK, form) —
    otherwise a cap spends itself three times on one company.

    Two: an alphabetical cap is a census of the start of the alphabet. Names
    carrying an OPEN THESIS are taken first, because they are the reason to
    care; the remainder is taken most-recently-filed first, which is
    deterministic without being sorted by name.
    """
    by_issuer = {}
    for (tk, form), (cik, acc, filed) in candidates.items():
        key = (cik, form)
        prev = by_issuer.get(key)
        # Shortest ticker on a CIK is the common share class, not the unit
        # or the warrant.
        if prev is None or len(tk) < len(prev[0]):
            by_issuer[key] = (tk, cik, acc, filed, form)
    rows = list(by_issuer.values())
    mine = sorted((r for r in rows if r[0] in thesis),
                  key=lambda r: (r[3], r[0]), reverse=True)
    rest = sorted((r for r in rows if r[0] not in thesis),
                  key=lambda r: (r[3], r[0]), reverse=True)
    ordered = mine + rest
    return ordered[:cap], len(ordered) > cap, len(rows), len(mine)


def build(sess, phrases: dict, univ: set, obs: dict) -> dict:
    day = dt.date.today().isoformat()
    claims, refused, pairs = [], [], 0
    no_baseline, unavailable = [], []

    candidates = {}
    for form in FORMS:
        for tk, (cik, acc, filed) in recent_filers(sess, form, LOOKBACK).items():
            if tk in univ:
                candidates.setdefault((tk, form), (cik, acc, filed))
    thesis = thesis_tickers()
    picked, truncated, n_issuers, n_mine = select(candidates, thesis, MAX_NAMES)
    print(f"[librarian] {len(candidates)} recent {'/'.join(FORMS)} filing(s) "
          f"in the watched universe of {len(univ)} -> {n_issuers} issuer(s) "
          f"-> reading {len(picked)} ({n_mine} with an open thesis)")

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
              f"indexed {len(idx & {prev_acc, acc})}/2")

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
                ok = a in idx
                sides[label] = {
                    "accession": a, "filed": f,
                    "state": lib.state_from_hits(1 if a in hit else 0, ok),
                    "reason": "" if ok else "filing not in the EDGAR "
                                            "full-text index for this window",
                }
            claims.append(lib.claim(tk, cik, form, ph, sides["prev"],
                                    sides["curr"],
                                    obs.get(acc, []) + obs.get(prev_acc, [])))

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc)
                          .isoformat(timespec="seconds"),
        "day": day,
        "source": "sec-edgar-fts",
        "instrument": "phrase-presence diff over the primary document",
        "not_a_section_diff": (
            "Section attribution (Item 1A vs Item 7) is NOT OBSERVED: filing "
            "bodies are served from www.sec.gov, which answers 403 to this "
            "client. Only pre-declared phrases are tested, so language "
            "nobody declared in advance is invisible here."),
        "phrases": {"digest": phrases["digest"],
                    "count": len(phrases["phrases"]),
                    "note": phrases["note"]},
        "watched": len(univ),
        "issuers_eligible": n_issuers,
        "issuers_read": len(picked),
        "with_open_thesis": n_mine,
        "pairs_compared": pairs,
        "names_truncated": truncated,
        "selection_rule": ("deduped on (CIK, form) so share classes count "
                           "once; open-thesis names first, then most "
                           "recently filed. Not alphabetical."),
        "max_names": MAX_NAMES,
        "no_baseline": no_baseline,
        # 🔑 Kept apart from no_baseline on purpose: an outage must not be
        # able to report itself as an absence of filing history.
        "baseline_unavailable": unavailable,
        # 🔑 Counted, not skipped. The cost of declaring late is visible.
        "refused_lookahead": refused,
        "claims": claims,
        "summary": lib.summarise(claims),
    }


def main() -> None:
    raw = open(PHRASES, "rb").read()
    phrases = lib.parse_phrases(raw)
    print(f"[librarian] {len(phrases['phrases'])} declared phrase(s), "
          f"list {phrases['digest'][:16]}…")

    univ = watched()
    if not univ:
        print("[librarian] no universe — refusing to report an empty watch "
              "list as 'no language changed'")
        sys.exit(2)

    sess = edgar.session(os.environ.get("SEC_USER_AGENT", ""))
    brief = build(sess, phrases, univ, observation_index())

    # 🔑 REFUSE TO PUBLISH A FUNNEL THAT ENDED IN NOTHING FOR A REASON WE
    # CANNOT SEE. If every filing we picked lost its baseline to a failed
    # lookup, then "no language changed" is not a finding, it is an outage
    # wearing a finding's clothes. Specification 4's rehearsal was stopped
    # for exactly this shape and the stop was right.
    unavail, read = len(brief["baseline_unavailable"]), brief["issuers_read"]
    if read and brief["pairs_compared"] == 0 and unavail == read:
        print(f"\n[librarian] REFUSING TO PUBLISH THIS BRIEF")
        print(f"::error::not one of {read} filing(s) kept a baseline — all "
              f"{unavail} lost it to a FAILED LOOKUP, not to an absence of "
              f"filing history. An empty brief here would read as 'no "
              f"language changed' when the truth is 'we could not look'.")
        for r in brief["baseline_unavailable"][:5]:
            print(f"  {r['ticker']:<6} {r['form']}  {r['reason']}")
        sys.exit(5)

    os.makedirs(OUT_DIR, exist_ok=True)
    jpath = os.path.join(OUT_DIR, f"{brief['day']}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(brief, f, indent=1, sort_keys=True)
        f.write("\n")
    text = lib.digest_text(brief)
    with open(os.path.join(OUT_DIR, f"{brief['day']}.md"), "w",
              encoding="utf-8") as f:
        f.write(text + "\n")

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
    print("\n" + text[:1400])
    print("\n[librarian] structured only. No document scored, nothing "
          "recommended, no order placed, K unchanged.")


if __name__ == "__main__":
    main()
