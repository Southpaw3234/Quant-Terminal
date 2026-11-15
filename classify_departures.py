#!/usr/bin/env python3
"""Why did each unpriceable departure leave? -- step 2 of the survivorship fix.

`build_delisted_roster.py` (step 1) inventories every exchange departure and
records what its common stock is doing now. `build_universe_v29.py` already
admits the ones that still have a price history. What is left is the group
this file exists for:

    status == "no-data"     the price source returns NOTHING for the name

518 of the 1,889 rows in delisted_roster_2015.csv. More than the 406 that
continued over the counter. They cannot be placed in a point-in-time
universe by any free source, because eligibility there is DEFINED on price
and volume, and for these names there is none to screen.

-----------------------------------------------------------------------------
🔑 WHAT THIS FILE CAN AND CANNOT DO ABOUT THAT

It cannot price them. Nothing free can: the second source tried (Stooq) now
refuses scripted access outright. So it does not put a single name into the
universe, and it computes no return.

What it CAN do is say WHY each one left, from the company's own filing
history on data.sec.gov, which is reachable. That matters because "no-data"
was being read as "died", and it is not one thing:

    acquired            paid out at a deal price. Not a loss.
    spac                redeemed or liquidated near trust value.
    fund                a closed-end fund merged or liquidated near NAV.
    still_listed        not a departure at all (a ticker or exchange change,
                        or a Form 25 for a warrant) -- a roster error.
    exchange_transfer   re-registered on another exchange the same month.
    continued_reporting kept filing annual and quarterly reports for more
                        than a year afterwards: the Form 25 removed ONE
                        security or ONE venue, not the company.
    bankruptcy          8-K item 1.03. The tail everyone means by "delisted".
    delisted_for_cause  exchange-initiated after a deficiency notice.
    deregistered        went dark voluntarily (Form 15).
    unclassified        none of the above could be established.
    lookup_failed       EDGAR did not answer. NOT a category of company.

A survivorship bias has a SIGN, and the sign depends on this mix. Missing
bankruptcies flatter a result; missing acquisitions understate one. Until
the mix is known, "the universe is biased upward" is an assumption. After
it, it is a count.

-----------------------------------------------------------------------------
🔑 EVERY RULE IS MECHANICAL AND STATED

`classify` reads form types, 8-K item numbers, the SIC code and today's
listing. It does not read prose, weigh anything, or emit a number. The
first rule that matches wins, in the order below, and the row records which
evidence matched -- so a classification can be checked against the filing
index by hand.

-----------------------------------------------------------------------------
🔑 A FORM 25 IS NOT A DEPARTURE, AND THE FIRST VERSION FORGOT THAT

The first run of this file put Hanesbrands, Masimo and Southwestern Energy
under `deregistered` for 2015-2018, and TRI Pointe and Kennedy-Wilson under
`acquired`. Every one of them traded for years afterwards. Two things had
gone wrong, and the roster builder's own docstring had warned about the
first:

  * A Form 25 removes A SECURITY -- a note series, a preferred, a warrant --
    and is also filed on an exchange TRANSFER. The company can be untouched.
  * These rows read `no-data` only because the company was acquired much
    LATER, so "is it listed today?" could not rescue them, and the rules
    fell through to whatever paperwork sat near the Form 25: a Form 15 for
    the old exchange, or the 425s an ACQUIRER files.

8-K item 3.01 has the same trap: it is headed "Notice of Delisting ...;
Transfer of Listing", so Iteris moving from NYSE MKT to Nasdaq read as a
delisting for cause.

So two rules now ask what the company did AFTERWARDS rather than what it
filed that month, and `acquired` requires that it did not carry on.

Writes data/universe/departures_classified.csv. Computes no return, spends
no K, and does not touch the frozen panel.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import agentio as io
from qt import edgar

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time,
# qt.prices. This inventories; it does not measure.

ROSTER = os.environ.get("QT_DEP_ROSTER", "data/universe/delisted_roster_2015.csv")
OUT_CSV = os.environ.get("QT_DEP_OUT", "data/universe/departures_classified.csv")
STATUSES = tuple(os.environ.get("QT_DEP_STATUSES", "no-data").split(","))
LIMIT = int(os.environ.get("QT_DEP_LIMIT", "0"))

CATEGORIES = ("bankruptcy", "still_listed", "exchange_transfer", "fund",
              "spac", "acquired", "delisted_for_cause",
              "continued_reporting", "deregistered", "unclassified",
              "lookup_failed")
# What each category implies about the holder of the common stock ON THE
# FORM 25 DATE. A grouping for reading the counts; nothing is computed from
# it and no magnitude is attached.
GROUPS = {
    "adverse": ("bankruptcy", "delisted_for_cause", "deregistered"),
    "benign": ("acquired", "spac", "fund"),
    "not_a_departure": ("still_listed", "exchange_transfer",
                        "continued_reporting"),
    "unknown": ("unclassified", "lookup_failed"),
}
PERIODIC_FORMS = {"10-K", "10-Q", "10-K405", "10-KT", "20-F", "40-F"}
REGISTRATION_FORMS = {"8-A12B", "8-A12B/A"}
CONTINUED_AFTER_DAYS = 400      # more than one full annual reporting cycle
MAJOR_EXCHANGES = ("NASDAQ", "NYSE", "CBOE", "BATS", "IEX")
FUND_FORMS = {"N-CSR", "N-CSRS", "NPORT-P", "N-CEN", "N-2", "N-PX", "N-Q",
              "N-MFP", "N-30D"}
MERGER_FORMS = {"425", "DEFM14A", "PREM14A", "DEFM14C", "SC TO-T", "SC TO-I",
                "SC 14D9", "SC 13E3", "SC14D9", "SCHEDULE TO-T"}
DEREG_FORMS = {"15-12G", "15-12B", "15-15D", "15F-12G", "15F-12B", "15F-15D"}
SPAC_SIC = "6770"


def _days(a: str, b: str) -> "int | None":
    """a minus b in days, or None if either is not a date."""
    try:
        return (dt.date.fromisoformat(str(a)[:10])
                - dt.date.fromisoformat(str(b)[:10])).days
    except ValueError:
        return None


def filings_of(sub: dict) -> list:
    """[(form, date, [8-K items])] from a data.sec.gov submissions payload."""
    rec = ((sub or {}).get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    dates = rec.get("filingDate") or []
    items = rec.get("items") or [""] * len(forms)
    out = []
    for f, d, it in zip(forms, dates, items):
        out.append((str(f or "").strip(), str(d or "")[:10],
                    [x.strip() for x in str(it or "").split(",") if x.strip()]))
    return out


def classify(sub: dict, form25_date: str) -> dict:
    """-> {"category", "evidence"}. The first matching rule wins.

    `sub` is the company's submissions payload; `form25_date` is the day it
    left the exchange. Windows are measured in days from that date.
    """
    rows = filings_of(sub)
    if not rows:
        return {"category": "unclassified",
                "evidence": "no filing history in the submissions payload"}

    def within(form_set=None, item=None, lo=-365, hi=365) -> list:
        out = []
        for f, d, items in rows:
            gap = _days(d, form25_date)
            if gap is None or not (lo <= gap <= hi):
                continue
            if form_set is not None and f in form_set:
                out.append(f"{f} {d}")
            if item is not None and f.startswith("8-K") and item in items:
                out.append(f"8-K item {item} {d}")
        return out

    # 1. Bankruptcy. Checked first because it is the tail that matters most
    #    and because a bankrupt company can also look like every other rule.
    hit = within(item="1.03", lo=-365, hi=180)
    if hit:
        return {"category": "bankruptcy", "evidence": "; ".join(hit[:3])}

    # 2. Still listed on a major exchange TODAY: not a departure. A ticker
    #    change, an exchange transfer, or a Form 25 for a warrant or a unit.
    exch = [str(x or "") for x in (sub.get("exchanges") or [])]
    tks = [t for t in (sub.get("tickers") or []) if str(t).strip()]
    major = [e for e in exch if any(m in e.upper() for m in MAJOR_EXCHANGES)]
    if tks and major:
        return {"category": "still_listed",
                "evidence": f"lists {', '.join(tks[:3])} on "
                            f"{', '.join(sorted(set(major)))} today"}

    # 3. Re-registered on an exchange the same month: a transfer.
    moved = within(form_set=REGISTRATION_FORMS, lo=-45, hi=45)
    if moved:
        return {"category": "exchange_transfer",
                "evidence": "; ".join(moved[:2])}

    # What did the company do AFTERWARDS? Two or more annual or quarterly
    # reports filed over a year past the Form 25 mean the registrant carried
    # on, so whatever was removed that day was not the company.
    later = [f"{f} {d}" for f, d, _ in rows
             if f in PERIODIC_FORMS
             and (_days(d, form25_date) or 0) > CONTINUED_AFTER_DAYS]
    continued = len(later) >= 2

    # 3b. A registered fund.
    fund = sorted({f for f, _, _ in rows if f in FUND_FORMS})
    if fund:
        return {"category": "fund",
                "evidence": "files " + ", ".join(fund[:4])}

    # 4. A blank-check company.
    if str(sub.get("sic") or "").strip() == SPAC_SIC:
        return {"category": "spac", "evidence": "SIC 6770 (blank check)"}

    # 5. Acquired: a completed acquisition or change of control reported
    #    around the departure, or a run of merger filings before it.
    #    🔑 And it must NOT have carried on. The buyer files 425s and an
    #    8-K 2.01 as well, and is still there the following year.
    done = within(item="2.01", lo=-60, hi=60) + within(item="5.01", lo=-60, hi=60)
    merger = within(form_set=MERGER_FORMS, lo=-400, hi=30)
    if (done or len(merger) >= 2) and not continued:
        return {"category": "acquired",
                "evidence": "; ".join((done + merger)[:3])
                            + (f" (+{len(merger)} merger filings)"
                               if len(merger) > 3 else "")}

    # 6. Exchange-initiated after a deficiency notice.
    #    (A transfer of listing also files item 3.01; rule 3 took those.)
    cause = within(item="3.01", lo=-365, hi=30)
    if cause:
        return {"category": "delisted_for_cause",
                "evidence": "; ".join(cause[:3])
                            + (" — kept reporting afterwards, so likely "
                               "over the counter" if continued else "")}

    # 6b. Carried on, with no bankruptcy, deficiency or transfer on record.
    if continued:
        return {"category": "continued_reporting",
                "evidence": f"{len(later)} periodic report(s) more than "
                            f"{CONTINUED_AFTER_DAYS} days later, e.g. "
                            f"{later[-1]}"}

    # 7. Went dark.
    dereg = within(form_set=DEREG_FORMS, lo=-30, hi=365)
    if dereg:
        return {"category": "deregistered", "evidence": "; ".join(dereg[:2])}

    # The payload's `recent` block holds about a thousand filings. For a
    # heavy filer that may not reach back to the Form 25, and then the
    # absence of evidence above is an absence of HISTORY, which is said.
    oldest = min((d for _, d, _ in rows if d), default="")
    short = bool(oldest) and (_days(oldest, form25_date) or 0) > -30
    return {"category": "unclassified",
            "evidence": (f"filing history on hand starts {oldest}, after or "
                         f"too close to the Form 25 — not enough to judge"
                         if short else
                         "no bankruptcy, merger, deficiency or "
                         "deregistration filing in the window")}


def summarise(rows: list) -> dict:
    """Counts per category. A count, and nothing derived from it."""
    out = {c: 0 for c in CATEGORIES}
    for r in rows or []:
        out[r["category"]] = out.get(r["category"], 0) + 1
    out["total"] = len(rows or [])
    return out


def grouped(rows: list) -> dict:
    """Counts per GROUP. Every category belongs to exactly one."""
    out = {g: 0 for g in GROUPS}
    for r in rows or []:
        for g, cats in GROUPS.items():
            if r["category"] in cats:
                out[g] += 1
    return out


def read_roster(path: str, statuses: tuple) -> list:
    """Roster rows with the wanted statuses, one per CIK (the first seen).

    Read with the csv module: a ticker column read by pandas turns `NA`
    into NaN.
    """
    seen, out = set(), []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") not in statuses:
                continue
            cik = str(r.get("cik") or "").strip()
            if not cik or cik in seen:
                continue
            seen.add(cik)
            out.append(r)
    return out


def main() -> None:
    now = io.now_utc()
    roster = read_roster(ROSTER, STATUSES)
    if LIMIT:
        roster = roster[:LIMIT]
    print(f"[departures] {len(roster)} distinct CIK(s) with status "
          f"{'/'.join(STATUSES)} in {ROSTER}")
    if not roster:
        print("[departures] nothing to classify — refusing to write an "
              "empty file that would read as 'no departures'")
        sys.exit(2)

    sess = edgar.session(os.environ.get("SEC_USER_AGENT", ""))
    out = []
    for i, r in enumerate(roster, 1):
        try:
            got = edgar.fetch_json(
                sess, edgar.SUBMISSIONS.format(cik=int(r["cik"])), attempts=3)
        except (TypeError, ValueError):
            got = {"ok": False, "json": {}, "reason": "not a CIK"}
        if got["ok"]:
            sub = got["json"]
            c = classify(sub, r.get("form25_date") or "")
        else:
            # 🔑 A failed lookup is its own row, never "unclassified": one is
            # a fact about EDGAR tonight, the other a fact about a company.
            sub, c = {}, {"category": "lookup_failed",
                          "evidence": got["reason"]}
        out.append({
            "cik": r["cik"], "ticker": r.get("ticker") or "",
            "issuer_edgar": r.get("issuer_edgar") or "",
            "form": r.get("form") or "",
            "form25_date": r.get("form25_date") or "",
            "roster_status": r.get("status") or "",
            "category": c["category"], "evidence": c["evidence"],
            "sic": str(sub.get("sic") or ""),
            "sic_description": str(sub.get("sicDescription") or ""),
            "tickers_now": "|".join(sub.get("tickers") or []),
            "exchanges_now": "|".join(str(x) for x in
                                      (sub.get("exchanges") or [])),
            "classified_at": now,
        })
        if i % 50 == 0:
            print(f"[departures] {i}/{len(roster)}")

    s = summarise(out)
    failed = s["lookup_failed"]
    # 🔑 Refuse to write a classification that is mostly failures: a file of
    # "lookup_failed" rows is an outage with a CSV header.
    if failed > 0.2 * len(out):
        print(f"\n[departures] REFUSING TO WRITE — {failed} of {len(out)} "
              f"lookups failed. That is EDGAR being unavailable, not a "
              f"finding about these companies.")
        print(f"::error::departures: {failed} of {len(out)} lookups failed")
        sys.exit(5)

    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()),
                           lineterminator="\n")
        w.writeheader()
        w.writerows(out)

    print(f"\n[departures] wrote {OUT_CSV} — {len(out)} rows")
    for c in CATEGORIES:
        if s[c]:
            print(f"  {c:<20} {s[c]:>4}  {s[c] / s['total']:>6.1%}")
    g = grouped(out)
    print("\n[departures] read as")
    for name, n in g.items():
        print(f"  {name:<16} {n:>4}  {n / s['total']:>6.1%}")
    print("\n[departures] classified only. No name was priced, no return "
          "computed, the frozen panel untouched, K unchanged.")


if __name__ == "__main__":
    main()
