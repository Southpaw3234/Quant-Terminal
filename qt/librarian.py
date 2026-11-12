"""LIBRARIAN -- pure logic for the filing phrase diff. Stdlib only.

See docs/COLLECTIVE_AGENT_BRIEF.md 3.2. The brief asks module 3 to diff
10-K/10-Q risk factors and MD&A quarter over quarter, because **removed
language is frequently more informative than added language.**

-----------------------------------------------------------------------------
🔑 WHAT THIS MODULE CANNOT DO, ESTABLISHED BY MEASUREMENT

A full text diff needs the filing BODY. Filing bodies live under
`www.sec.gov/Archives/`, and that host answers **403** to this client. The
403 page is titled "Request Rate Threshold Exceeded" and it is returned on
the FIRST request of a cold session, so it is not a function of our request
rate and backing off does not clear it.

So this module does NOT diff documents. It does something narrower, and
states the difference everywhere it reports:

    for a phrase declared IN ADVANCE, ask EDGAR full-text search whether
    that phrase appears in a company's filing -- and compare the answer for
    this quarter against the previous one.

That is a **phrase-presence diff over the primary document**, not a section
diff. Three consequences are reported, never hidden:

  1. SECTION IS NOT OBSERVED. A phrase cannot be located to Item 1A rather
     than Item 7, so `section` is always None. "A risk factor was removed"
     is a claim this module is not entitled to make. "This phrase left the
     filing" is.
  2. ONLY DECLARED PHRASES ARE SEEN. Language removed that nobody declared
     in advance is invisible here. A brief is a sample of a question, not a
     census of a document.
  3. ABSENT IS NOT ABSENT UNLESS THE FILING WAS INDEXED. See below.

-----------------------------------------------------------------------------
🔑 THE IMPOSSIBLE-ZERO RULE, APPLIED WHERE IT MATTERS MOST

Zero hits for a phrase has two causes that look identical in the response:
the phrase is genuinely gone, or the filing is not in the full-text index at
all. Those are reported as `absent` and `unknown`, never merged.

This is the most important line of defence in the module, because REMOVALS
are the headline signal. A filing missing from the index would otherwise
manufacture a removal for every declared phrase at once -- the most
confident and the most wrong output this agent could produce. `classify`
therefore returns `indeterminate` whenever either side is unknown, and can
never reach `removed` from an unknown.

-----------------------------------------------------------------------------
🔑 THE PHRASE LIST IS PRE-REGISTERED, AND THAT IS NOT DECORATION

If phrases may be added after reading the filing, "look for removed
language" becomes "add phrases until one of them shows a removal" -- the
exact data-snooping this repository exists to prevent, moved from
specifications to documents.

Two guards, both refusals rather than warnings:

  * every phrase carries `declared_at`, and `check_declared_before` REFUSES
    a phrase declared on or after the filing it is being tested against. A
    phrase chosen after the document was published could have been chosen
    BY READING IT.
  * the brief records the phrase file's SHA-256, so a later edit to the list
    is visible against any brief already published.

This module assigns no score to any document, and there is no code here that
could: no ranking, no weighting, no numeric judgment of a filing. Counts of
claims are counts, not scores.
"""

from __future__ import annotations

import hashlib
import json

# A phrase's state in ONE filing.
PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"

# A phrase's transition BETWEEN two filings.
REMOVED, ADDED, RETAINED, NEVER, INDETERMINATE = (
    "removed", "added", "retained", "absent_both", "indeterminate")

# 🔑 Transitions worth a human's attention. `removed` is first on purpose.
NOTABLE = (REMOVED, ADDED)


# -- the pre-registered phrase list -------------------------------------------

def digest_phrases(raw_bytes: bytes) -> str:
    """SHA-256 of the phrase file exactly as committed.

    Recorded in every brief, so that editing the list later is visible
    against briefs already published.
    """
    return hashlib.sha256(raw_bytes).hexdigest()


def parse_phrases(raw_bytes: bytes) -> dict:
    """Parse and VALIDATE the declared phrase list. Refuses a bad list.

    Every phrase needs text and a `declared_at`. A phrase with no
    declaration date cannot be checked for look-ahead, and a check that
    cannot fail is not a check.
    """
    doc = json.loads(raw_bytes.decode("utf-8"))
    out, seen = [], set()
    for i, p in enumerate(doc.get("phrases") or []):
        text = str(p.get("text", "")).strip()
        when = str(p.get("declared_at", "")).strip()
        if not text:
            raise ValueError(f"phrase {i} has no text")
        if not when:
            raise ValueError(
                f"phrase {i} ({text!r}) has no declared_at. A phrase with no "
                f"declaration date cannot be checked for look-ahead, and the "
                f"whole point of declaring is that the date is checkable.")
        key = text.lower()
        if key in seen:
            raise ValueError(f"phrase {text!r} is declared twice -- a "
                             f"duplicate would be counted twice")
        seen.add(key)
        out.append({"text": text, "declared_at": when,
                    "why": str(p.get("why", "")).strip()})
    if not out:
        raise ValueError("no phrases declared -- refusing to report a brief "
                         "with nothing in it as 'nothing changed'")
    return {"phrases": out, "digest": digest_phrases(raw_bytes),
            "note": str(doc.get("note", "")).strip()}


def check_declared_before(phrase: dict, filed: str) -> None:
    """🔑 REFUSE a phrase declared on or after the filing it is tested on.

    The look-ahead guard. A phrase declared after the document was published
    could have been chosen by reading that document, which would make a
    "removal" a thing we went looking for rather than a thing we found.
    """
    when = str(phrase.get("declared_at", "")).strip()
    if not when:
        raise ValueError(f"phrase {phrase.get('text')!r} has no declared_at")
    if when >= str(filed):
        raise ValueError(
            f"phrase {phrase.get('text')!r} was declared {when}, on or after "
            f"the filing it is being tested against ({filed}). A phrase "
            f"chosen after the document was published could have been chosen "
            f"by reading it.")


# -- the diff -----------------------------------------------------------------

def baseline_from_history(rows: list, form: str, current_acc: str) -> dict:
    """The filing to compare against. -> {"ok", "accession", "filed", "reason"}

    🔑 Four outcomes, kept distinct. "No baseline" has causes that an empty
    return value would render identical, and only one of them means the
    company is filing this form for the first time:

      * the current filing is not in the recent history -> we cannot say
        which filing precedes it, so we REFUSE rather than take the
        second-newest and call it the baseline. That fallback is a guess
        wearing the clothes of a lookup.
      * the current filing IS the earliest in the window -> there may well
        be an older one outside it. Reported as a window limit, not as a
        first filing.
      * no filings of this form at all -> genuinely nothing to compare.
      * otherwise -> the filing immediately before it.
    """
    same = sorted(({"accession": r["accession"], "filed": r["filed"]}
                   for r in rows or [] if r.get("form") == form),
                  key=lambda r: (r["filed"], r["accession"]), reverse=True)
    if not same:
        return {"ok": False, "accession": "", "filed": "",
                "reason": f"no {form} in the recent filing history"}
    at = next((i for i, r in enumerate(same)
               if r["accession"] == current_acc), None)
    if at is None:
        return {"ok": False, "accession": "", "filed": "",
                "reason": f"the current filing {current_acc} is not in the "
                          f"recent history, so its predecessor cannot be "
                          f"identified — refusing to assume one"}
    if at + 1 >= len(same):
        return {"ok": False, "accession": "", "filed": "",
                "reason": f"this is the earliest {form} in the recent "
                          f"history window — an older one may exist outside "
                          f"it, so this is a window limit, not a first filing"}
    nxt = same[at + 1]
    return {"ok": True, "accession": nxt["accession"], "filed": nxt["filed"],
            "reason": "ok"}


def state_from_hits(primary_hits: int, filing_indexed: bool) -> str:
    """One phrase's state in one filing.

    🔑 Zero hits in a filing that is NOT in the index is `unknown`, not
    `absent`. The two are indistinguishable in the response and opposite in
    meaning.
    """
    if not filing_indexed:
        return UNKNOWN
    return PRESENT if primary_hits > 0 else ABSENT


def classify(prev: str, curr: str) -> str:
    """The transition between two filings.

    🔑 Returns `indeterminate` whenever either side is unknown. It never
    guesses toward `removed`, because a filing missing from the index would
    otherwise manufacture a removal for every declared phrase at once.
    """
    if prev == UNKNOWN or curr == UNKNOWN:
        return INDETERMINATE
    if prev == PRESENT and curr == ABSENT:
        return REMOVED
    if prev == ABSENT and curr == PRESENT:
        return ADDED
    if prev == PRESENT and curr == PRESENT:
        return RETAINED
    return NEVER


def claim(ticker: str, cik, form: str, phrase: dict, prev: dict, curr: dict,
          observation_ids=None) -> dict:
    """One structured claim: who, what, when, versus what baseline.

    No score. `section` is None because section is not observable from the
    full-text index, and None here means NOT OBSERVED -- never "no section".
    """
    transition = classify(prev["state"], curr["state"])
    return {
        "ticker": ticker,
        "cik": int(cik) if cik is not None else None,
        "form": form,
        "phrase": phrase["text"],
        "phrase_declared_at": phrase["declared_at"],
        "why_declared": phrase.get("why", ""),
        "transition": transition,
        "notable": transition in NOTABLE,
        # 🔑 None = not observed. The full-text index cannot say whether a
        # phrase sat in Item 1A or in Item 7.
        "section": None,
        "section_source": "unavailable: section attribution needs the filing "
                          "body, served from www.sec.gov, which answers 403",
        "baseline": {"accession": prev["accession"], "filed": prev["filed"],
                     "state": prev["state"], "reason": prev.get("reason", "")},
        "current": {"accession": curr["accession"], "filed": curr["filed"],
                    "state": curr["state"], "reason": curr.get("reason", "")},
        "observation_ids": sorted(observation_ids or []),
        "observed_by_observer": bool(observation_ids),
    }


def summarise(claims: list) -> dict:
    """Counts per transition. Counts, not scores: nothing here orders one
    filing above another."""
    out = {k: 0 for k in (REMOVED, ADDED, RETAINED, NEVER, INDETERMINATE)}
    for c in claims or []:
        out[c["transition"]] = out.get(c["transition"], 0) + 1
    out["claims"] = len(claims or [])
    out["names"] = len({c["ticker"] for c in claims or []})
    return out


def notable(claims: list) -> list:
    """Removals first, then additions. Ordered by KIND, not by magnitude --
    there is no magnitude here to order by."""
    order = {REMOVED: 0, ADDED: 1}
    return sorted((c for c in claims or [] if c["notable"]),
                  key=lambda c: (order.get(c["transition"], 9),
                                 c["ticker"], c["phrase"]))


def digest_text(brief: dict, max_lines: int = 40) -> str:
    """The human-readable brief. States what was not observable."""
    s = brief["summary"]
    lines = [f"**Filing language -- {brief['day']}**",
             f"{s['claims']} claim(s) across {s['names']} name(s) - "
             f"{s[REMOVED]} removed - {s[ADDED]} added - "
             f"{s[INDETERMINATE]} indeterminate"]

    # 🔑 A brief with no claims because nothing was TESTABLE must never read
    # as a brief with no claims because nothing CHANGED. This is the same
    # conflation the whole module is built against, and the digest is where
    # it would actually reach a human.
    n_refused = len(brief.get("refused_lookahead") or [])
    if not brief["claims"] and n_refused:
        lines.append("")
        lines.append(f"🔑 **Nothing was testable today — this is not "
                     f"'nothing changed'.** All {n_refused} phrase test(s) "
                     f"were REFUSED because the phrase was declared on or "
                     f"after the filing it would have been tested against. "
                     f"A phrase chosen after the document was published "
                     f"could have been chosen by reading it.")
        lines.append(f"The list becomes testable against filings published "
                     f"after its declaration date. Declaring is free; "
                     f"applying it backwards is not allowed.")
        if brief.get("pairs_compared"):
            lines.append(f"({brief['pairs_compared']} filing pair(s) were "
                         f"located and indexed — the baseline machinery "
                         f"worked; only the phrases were too young.)")

    nb = notable(brief["claims"])
    if nb:
        lines.append("")
        lines.append("__Removed language is the signal worth reading first__")
        for c in nb[:max_lines]:
            mark = "🔻 REMOVED" if c["transition"] == REMOVED else "🔹 added  "
            lines.append(f"- {mark} `{c['ticker']}` {c['form']} -- "
                         f"\"{c['phrase']}\"  "
                         f"({c['baseline']['filed']} -> {c['current']['filed']})")
        if len(nb) > max_lines:
            lines.append(f"_...and {len(nb) - max_lines} more_")
    elif brief["claims"]:
        lines.append("")
        lines.append(f"_{s['claims']} phrase(s) tested; none changed state._")
    elif not n_refused:
        lines.append("")
        lines.append("_No filing pair was available to compare._")

    if s[INDETERMINATE]:
        lines.append("")
        lines.append(f"⚠️ **{s[INDETERMINATE]} claim(s) are INDETERMINATE** -- "
                     f"the filing was not in the full-text index, so zero "
                     f"hits does not mean the language is gone.")
    lines.append("")
    lines.append("⚠️ **This is a phrase-presence diff, not a section diff.** "
                 "Only phrases declared in advance are tested; language "
                 "nobody declared is invisible here. Section (Item 1A vs "
                 "Item 7) is **not observed** -- filing bodies are served "
                 "from a host that refuses this client.")
    lines.append(f"Phrase list `{brief['phrases']['digest'][:16]}...` "
                 f"({brief['phrases']['count']} declared).")
    return "\n".join(lines)
