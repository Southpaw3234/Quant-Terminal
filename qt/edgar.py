"""EDGAR access, with the traps this project has already paid for.

Network imports are lazy, inside the functions that need them, so a hermetic
suite can import this module with no `requests` installed and still test
every pure function here.

🔑 THREE THINGS THAT ARE NOT OBVIOUS AND HAVE EACH COST A RUN

1. **`www.sec.gov` answers 403.** From CI and from the operator's machine
   alike. That rules out `company_tickers.json`, the daily form index, and
   every document body under `/Archives/`. Only `efts.sec.gov` (full-text
   search) and `data.sec.gov` (submissions) answer.

2. **EFTS indexes DOCUMENTS, not filings.** One 8-K with two exhibits is
   three hits. Counting hits reports roughly triple the filings that
   happened, and the number looks plausible enough never to be questioned.
   Dedupe on the accession number.

3. **The query term is EMPTY on purpose.** EFTS accepts `forms` + dates with
   no `q`. Any keyword silently selects the filings containing that word,
   turning a census of the day into a biased sample that still looks like a
   census.
"""

from __future__ import annotations

import re
import time

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EFTS_PAGE = 100                 # the API's maximum
SLEEP = 0.15

_PAREN = re.compile(r"\(([^)]*)\)")


# ── pure ─────────────────────────────────────────────────────────────────────

def tickers_from_display_names(names) -> set:
    """Ticker symbols out of an EFTS `display_names` entry.

        'BIOVIE INC.  (BIVI, BIVIW)  (CIK 0001580149)'

    Two traps, both live:
      1. the LAST parenthetical is the CIK. Taking "the thing in brackets"
         files every company in the market under a ticker of "CIK".
      2. one company can list SEVERAL tickers -- share class, warrant --
         comma-separated inside one parenthetical.

    A company with no listed ticker yields nothing rather than a placeholder:
    an empty set is a true statement, and "" is a ticker that would later
    match a blank field somewhere.

    ⚠️ Form 4 display_names carry NO ticker at all -- the reporting person
    and the issuer are both bare "(CIK ...)". Use `pick_issuer` for those.
    """
    out = set()
    for n in names or []:
        for grp in _PAREN.findall(str(n)):
            g = grp.strip()
            if g.upper().startswith("CIK"):
                continue
            for part in g.split(","):
                t = part.strip().upper()
                if t and len(t) <= 6 and t.replace(".", "").replace("-", "").isalnum():
                    out.add(t)
    return out


def accession_of(hit) -> str:
    """The filing's accession number from an EFTS hit id.

    `_id` is "<accession>:<document filename>". The accession is what
    identifies a FILING; the document is not.
    """
    return str((hit or {}).get("_id") or "").split(":")[0]


def dedupe_filings(hits) -> dict:
    """-> {accession: first hit for it}. See trap 2 in the module docstring."""
    out = {}
    for h in hits or []:
        acc = accession_of(h)
        if acc and acc not in out:
            out[acc] = h
    return out


def pick_issuer(ciks, info_by_cik: dict):
    """Which of a Form 4's CIKs is the ISSUER. -> (cik, ticker) or (None, None).

    A Form 4 names a reporting person and an issuer, and EFTS gives both in
    `ciks` with no label. Position is not reliable, so the question is asked
    of the data: an operating company has tickers, a human does not.

        Groupon, Inc.   tickers ['GRPN']  entityType operating
        Mittal Ajay V.  tickers []        entityType other

    `info_by_cik` maps int CIK -> the submissions payload (or a dict with at
    least `tickers`). A CIK that has not been looked up is skipped rather
    than guessed, so a missing cache entry can never silently pick a human.
    """
    for c in ciks or []:
        try:
            cik = int(c)
        except (TypeError, ValueError):
            continue
        info = info_by_cik.get(cik)
        if not info:
            continue
        tk = [str(t).strip().upper() for t in (info.get("tickers") or [])
              if str(t).strip()]
        if tk:
            return cik, tk[0]
    return None, None


# ── network ──────────────────────────────────────────────────────────────────

def session(user_agent: str = ""):
    import requests
    s = requests.Session()
    # EDGAR wants a descriptive User-Agent. A generic client name is used when
    # none is configured, rather than inventing contact details.
    s.headers.update({"User-Agent": (user_agent or "").strip()
                      or "Quant-Terminal research client",
                      "Accept-Encoding": "gzip, deflate"})
    return s


def efts_page(sess, form: str, day, frm: int = 0) -> dict:
    """One page of full-text search for a form on a date. Empty `q` — see
    trap 3."""
    r = sess.get(EFTS_URL, timeout=60, params={
        "q": "", "forms": form, "startdt": str(day), "enddt": str(day),
        "from": frm, "size": EFTS_PAGE})
    r.raise_for_status()
    time.sleep(SLEEP)
    hits = ((r.json() or {}).get("hits") or {})
    return {"total": ((hits.get("total") or {}).get("value") or 0),
            "hits": hits.get("hits") or []}


def efts_day(sess, form: str, day, max_pages: int = 12) -> dict:
    """Every hit for a form on a day, up to a page cap.

    Returns `truncated` so a capped day is reported as a sample rather than
    presented as a complete census.
    """
    out, total, pages = [], None, 0
    while pages < max_pages:
        p = efts_page(sess, form, day, pages * EFTS_PAGE)
        if total is None:
            total = p["total"]
        out.extend(p["hits"])
        pages += 1
        if len(p["hits"]) < EFTS_PAGE or pages * EFTS_PAGE >= (total or 0):
            break
    return {"total_documents": total or 0, "hits": out,
            "truncated": (total or 0) > pages * EFTS_PAGE}


def submissions(sess, cik: int) -> dict:
    """data.sec.gov submissions for one CIK. {} on any failure.

    Failure returns empty rather than raising so one bad CIK cannot take out
    a day's observation run -- but it returns EMPTY, never a guess, so a
    caller cannot mistake a failed lookup for a resolved one.
    """
    try:
        r = sess.get(SUBMISSIONS.format(cik=int(cik)), timeout=60)
        if r.status_code != 200:
            return {}
        time.sleep(SLEEP)
        d = r.json() or {}
        return {"name": d.get("name") or "",
                "tickers": d.get("tickers") or [],
                "entityType": d.get("entityType") or ""}
    except Exception:              # noqa: BLE001 - absence, never a guess
        return {}
