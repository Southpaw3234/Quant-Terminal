#!/usr/bin/env python3
"""OBSERVER — module 1 of the collective agent. Filings.

See docs/COLLECTIVE_AGENT_BRIEF.md §3.1. This agent COLLECTS. It does not
score, rank, recommend, or drop a record because it looks unimportant.

    EDGAR full-text search  ->  observations/YYYY-MM-DD.jsonl   (append-only)
    Finnhub insider feed    ->  Form 4 direction, purchases, cluster buys
                            ->  observations/YYYY-MM-DD.meta.json + .md

Forms: 8-K, 4, 10-Q, 10-K, 424B5. Every record carries `id`, `observed_at`,
`source`, `url` and the `raw` fields it was built from.

-----------------------------------------------------------------------------
🔑 FORM 4 DIRECTION: WHAT IS KNOWN, AND FROM WHERE

EDGAR's full-text index lists a Form 4's insider and issuer as bare CIKs.
Whether the insider BOUGHT or SOLD is in the ownership XML under
`www.sec.gov/Archives/`, which answers 403 to this client.

So direction comes from a second source, Finnhub's insider-transactions
feed, matched to the filing by ACCESSION NUMBER -- not by date or name --
and it is reported with its provenance:

  direction: "purchase" | "sale" | "mixed" | "other"   a transaction code
                                                       was read
  direction: null                                      NOT OBSERVED, with
                                                       the reason

`null` never means "no purchase". Insider SALES vastly outnumber purchases,
so presenting undirected Form 4 activity as buying would be wrong in the
most flattering possible direction, every single day. The first version of
this file CLAIMED the Finnhub path in its docstring and did not contain it:
every Form 4 it ever recorded has `direction: null`.

Finnhub lags EDGAR, so a filing observed today may get its direction
tomorrow. Records from the last few day files whose direction is still null
are re-checked on every run, and a direction is filled in ONCE, stamped
with when it was observed.

-----------------------------------------------------------------------------
🔑 CLUSTER BUYS USE THE FROZEN DEFINITION, BY IMPORT

A cluster buy here is whatever `extract_form4.cluster` says it is -- the
function that built the events behind the pre-registered form4_cluster_buy
reads -- called with its declared defaults. It is imported, not restated,
so an observed cluster and the historical base rate ACTUARY reports for it
are the same kind of event by construction rather than by resemblance.

-----------------------------------------------------------------------------
🔑 APPEND-ONLY MEANS A RE-RUN CANNOT SHRINK A DAY

The first version rewrote the day file from scratch on every run. A second
run with a partial EDGAR response would have replaced a complete record
with a shorter one, silently. `merge_day` keeps every record already
written, exactly as written, and only adds.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import extract_form4 as f4
from qt import agentio as io
from qt import edgar

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time, and any
# broker SDK. OBSERVER collects; it cannot spend a read, evaluate a criterion
# or place an order. validate_observer_filings.py asserts this with `ast`.

OUT_DIR = os.environ.get("QT_OBS_DIR", "observations")
UNIVERSE = os.environ.get("QT_OBS_UNIVERSE", "")      # daily.json, optional
PANEL = os.environ.get("QT_OBS_PANEL", "data/universe/v29_universe_pit.csv")
LEDGER = os.environ.get("QT_THESIS_LEDGER", "data/theses/ledger.jsonl")
CACHE = os.environ.get("QT_OBS_CACHE", os.path.join(OUT_DIR, "cik_cache.json"))
FORMS = ("8-K", "4", "10-Q", "10-K", "424B5")
MAX_PAGES = int(os.environ.get("QT_OBS_PAGES", "40"))
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
F4_LOOKBACK_DAYS = int(os.environ.get("QT_OBS_F4_LOOKBACK", "1095"))
F4_RECHECK_FILES = int(os.environ.get("QT_OBS_F4_RECHECK", "5"))
F4_MAX_TICKERS = int(os.environ.get("QT_OBS_F4_MAX", "120"))

NO_BODY = ("Form 4 bodies are on www.sec.gov, which answers 403 to this "
           "client")


# ── pure shaping ─────────────────────────────────────────────────────────────

def filing_url(cik, accession: str) -> str:
    """A link a human can click. Dashes stripped: EDGAR's path form."""
    return FILING_URL.format(cik=int(cik), acc=str(accession).replace("-", ""))


def is_form4(record: dict) -> bool:
    return str(record.get("root_form") or record.get("form") or "") \
        .startswith("4") and record.get("kind", "filing") == "filing"


def observation(form: str, hit: dict, ticker: str, cik, observed_at: str,
                direction=None, direction_source: str = "") -> dict:
    """One filing record. Source-linked, and honest about direction."""
    src = hit.get("_source") or {}
    acc = edgar.accession_of(hit)
    own = str(form).startswith("4")
    return {
        "id": f"edgar:{acc}",
        "kind": "filing",
        "observed_at": observed_at,
        "source": "sec-edgar-fts",
        # The form EDGAR recorded, so an amendment reads as 8-K/A rather
        # than being folded into the form that was queried.
        "form": src.get("form") or src.get("file_type") or form,
        "root_form": form,
        "accession": acc,
        "filed": src.get("file_date"),
        "ticker": ticker,
        "cik": int(cik) if cik is not None else None,
        "entity": (src.get("display_names") or [None])[0],
        "items": src.get("items") or [],
        "url": filing_url(cik, acc) if cik is not None else None,
        # null means NOT OBSERVED. It never means "no purchase".
        "direction": direction if own else None,
        "direction_source": (direction_source or f"unavailable: {NO_BODY}")
        if own else "n/a: not an ownership form",
        "direction_observed_at": None,
        "raw": {"display_names": src.get("display_names") or [],
                "period_ending": src.get("period_ending"),
                "sics": src.get("sics") or [],
                "file_description": src.get("file_description")},
    }


def upgrade(record: dict) -> dict:
    """Give a record written by the first version of this file the keys the
    current one relies on. Adds; never changes a value already present.

    The night-one records carry no `id`, `kind` or `root_form`. Without an
    id the LIBRARIAN cannot link a claim to them, and a re-collected day
    would append every one of them a second time. `observed_at` and every
    other original field are left exactly as written.
    """
    if not record.get("kind"):
        record["kind"] = "filing"
    if not record.get("root_form") and record.get("form"):
        record["root_form"] = str(record["form"]).split("/")[0]
    if not record.get("id") and record.get("accession"):
        record["id"] = f"edgar:{record['accession']}"
    return record


def merge_day(existing: list, new: list) -> tuple:
    """Append-only merge of one day's records. -> (merged, n_added)

    🔑 Every record already written is kept EXACTLY as written, including
    its original `observed_at`. New ids are appended. A re-run can therefore
    add to a day and can never shrink or restamp it.
    """
    existing = [upgrade(r) for r in existing]
    seen = {r.get("id") for r in existing}
    added = []
    for r in new:
        if r.get("id") not in seen:
            seen.add(r.get("id"))
            added.append(r)
    return list(existing) + added, len(added)


def direction_of(rows: list) -> str:
    """The direction of ONE filing from its transaction rows.

    Derivative rows are excluded on Finnhub's own flag rather than inferred
    from the code. P is an open-market purchase and S an open-market sale;
    everything else -- grants (A), option exercises (M), tax withholding
    (F), gifts (G) -- is "other", because calling an option exercise a
    purchase is the exact flattery this field exists to refuse.
    """
    codes = {r["code"] for r in rows or [] if not r.get("is_derivative")}
    buy, sell = "P" in codes, "S" in codes
    if buy and sell:
        return "mixed"
    if buy:
        return "purchase"
    if sell:
        return "sale"
    return "other"


def purchase_value(rows: list) -> "float | None":
    """Dollar value of the open-market purchases in one filing, or None when
    no row carries a usable price. None is 'not stated', never zero."""
    vals = [r["value"] for r in f4.filter_open_market_purchases(rows or [])]
    return round(sum(vals), 2) if vals else None


def enrich_direction(records: list, rows_by_ticker: dict, at: str,
                     unavailable: dict = None) -> int:
    """Fill a null Form 4 direction from Finnhub rows. -> number filled.

    Matched on ACCESSION, never on date or name: two insiders can file on
    the same day, and a same-day match would hand one filing the other's
    direction. A direction is written ONCE; a record that already has one
    is never revisited.
    """
    unavailable = unavailable or {}
    filled = 0
    for r in records:
        if not is_form4(r) or r.get("direction"):
            continue
        tk = r.get("ticker")
        if tk in unavailable:
            r["direction_source"] = f"unavailable: {unavailable[tk]}"
            continue
        if tk not in rows_by_ticker:
            continue
        mine = [x for x in rows_by_ticker[tk]
                if x.get("tx_id") and x.get("tx_id") == r.get("accession")]
        if not mine:
            r["direction_source"] = (
                "unavailable: this accession is not in Finnhub's insider "
                f"feed yet (checked {at}); re-checked on later runs")
            continue
        r["direction"] = direction_of(mine)
        r["direction_source"] = "finnhub insider-transactions, by accession"
        r["direction_observed_at"] = at
        r["purchase_value"] = purchase_value(mine)
        r["insiders"] = sorted({x["name"] for x in mine if x["name"]})
        filled += 1
    return filled


def first_purchases(rows: list, day: str) -> list:
    """Insiders whose open-market purchase filed on `day` is their first in
    the history Finnhub returned.

    🔑 "First in the available history", never "first ever". `history_from`
    is the earliest filing date actually present for the ticker, so a
    three-year feed cannot be read as a six-year claim.
    """
    buys = f4.filter_open_market_purchases(rows or [])
    if not buys:
        return []
    dates = [r["filing_date"] for r in rows if r["filing_date"]]
    history_from = min(dates) if dates else None
    out = {}
    for r in buys:
        if r["filing_date"] != day or not r["name"]:
            continue
        earlier = [b for b in buys if b["name"] == r["name"]
                   and b["filing_date"] < day]
        if not earlier:
            out.setdefault(r["name"], {"insider": r["name"], "value": 0.0,
                                       "history_from": history_from})
            out[r["name"]]["value"] = round(out[r["name"]]["value"]
                                            + r["value"], 2)
    return sorted(out.values(), key=lambda x: x["insider"])


def cluster_key(record: dict) -> tuple:
    """A cluster's identity: the ticker and the day its first filing landed.

    🔑 Not the event id. The id carries the cluster's LAST filing date, and
    a cluster still inside its five-day window moves that date every time
    another insider joins. Keyed on the id, PAL's one cluster was reported
    on 11-18, 11-19 and 11-23 -- three events against a base rate that
    counts it once.
    """
    return (str(record.get("ticker") or ""), str(record.get("first_filing") or ""))


def finalize_pending(records: list, at: str, rechecks: int) -> int:
    """Mark Form 4s still without a direction as FINAL. -> number marked.

    Called on a day file that will not be rechecked again. The first
    version left these saying "re-checked on later runs" after the last
    recheck had passed -- a promise the code no longer kept. Twenty were
    stranded that way in the first ten days.
    """
    n = 0
    for r in records:
        if is_form4(r) and not r.get("direction") and not r.get("direction_final"):
            r["direction_final"] = True
            r["direction_source"] = (
                f"not observed: absent from Finnhub's insider feed across "
                f"{rechecks} nightly rechecks (final as of {at}). The "
                f"direction of this filing is unknown, not 'no purchase'.")
            n += 1
    return n


def cluster_events(rows: list, day: str, at: str) -> list:
    """Cluster buys that COMPLETED on `day`, by the frozen definition.

    `extract_form4.cluster` with its declared defaults, on the purchases
    filed in the run-up to `day`. Kept only if the cluster's event date is
    `day` itself -- the day the full cluster became visible.
    """
    try:
        end = dt.date.fromisoformat(day)
    except ValueError:
        return []
    start = (end - dt.timedelta(days=3 * f4.WINDOW_DAYS)).isoformat()
    window = [r for r in f4.filter_open_market_purchases(rows or [])
              if start <= r["filing_date"] <= day]
    out = []
    for ev in f4.cluster(window):
        if ev["event_ts"] != day:
            continue
        out.append({
            "id": f"derived:{ev['event_id']}",
            "kind": "derived_event",
            "event_type": ev["event_type"],
            "observed_at": at,
            "source": "finnhub insider-transactions",
            "derived_by": "extract_form4.cluster (the frozen definition)",
            "definition": {"min_insiders": f4.MIN_INSIDERS,
                           "window_days": f4.WINDOW_DAYS,
                           "min_value": f4.MIN_VALUE},
            "form": "4", "root_form": "4",
            "ticker": ev["ticker"], "filed": day,
            "n_insiders": ev["n_insiders"], "n_filings": ev["n_filings"],
            "total_value": ev["total_value"],
            "first_filing": ev["first_filing"],
            "url": None, "direction": "purchase",
            "direction_source": "by construction: a cluster of code-P rows",
        })
    return out


def digest(records: list, day: str, meta: dict) -> str:
    """The human-readable summary. States what was not observable."""
    totals = meta.get("totals") or {}
    filings = [r for r in records if r.get("kind", "filing") == "filing"]
    derived = [r for r in records if r.get("kind") == "derived_event"]
    lines = [f"**Filings for {day}** — {len(filings)} in the watched "
             f"universe of {meta.get('watched', '?')}"]

    if derived:
        lines.append("")
        lines.append("__Form 4 cluster events (frozen definition: "
                     f"≥{f4.MIN_INSIDERS} insiders, {f4.WINDOW_DAYS} days, "
                     f"≥${f4.MIN_VALUE:,.0f})__")
        for r in sorted(derived, key=lambda x: x["ticker"]):
            lines.append(f"◆ `{r['ticker']}` — {r['n_insiders']} insiders, "
                         f"${r['total_value']:,.0f}")

    for form in FORMS:
        rs = [r for r in filings if (r.get("root_form") or r.get("form")) == form]
        t = totals.get(form, {})
        lines.append("")
        lines.append(f"__{form}__ — {len(rs)} of {t.get('filings', '?')} "
                     f"filings market-wide"
                     + ("  _(sampled, not a full census)_"
                        if t.get("truncated") else ""))
        if not rs:
            lines.append("_none_")
            continue
        for r in sorted(rs, key=lambda x: (x["ticker"], x["accession"]))[:25]:
            bits = [f"`{r['ticker']}`"]
            if r.get("items"):
                bits.append("item " + ", ".join(str(i) for i in r["items"][:4]))
            if r.get("direction"):
                d = f"**{r['direction']}**"
                if r.get("purchase_value"):
                    d += f" ${r['purchase_value']:,.0f}"
                bits.append(d)
            if r.get("url"):
                bits.append(f"<{r['url']}>")
            lines.append("· " + " — ".join(bits))
        if len(rs) > 25:
            lines.append(f"_…and {len(rs) - 25} more_")

    unknown = sum(1 for r in filings if is_form4(r) and not r.get("direction"))
    if unknown:
        note = (meta.get("direction") or {}).get("note") or NO_BODY
        lines.append("")
        lines.append(f"⚠️ **{unknown} Form 4 filing(s) have no observed "
                     f"direction.** {note}. These are filings, **not** "
                     f"purchases — insider sales outnumber buys, so do not "
                     f"read them as buying.")
    return "\n".join(lines)


# ── network ──────────────────────────────────────────────────────────────────

def last_filing_day(sess, probe_form: str = "8-K") -> "str | None":
    """Walk back to the most recent day that has filings.

    A weekend, a holiday and "not published yet" are the same observation
    from here: zero hits.
    """
    day = dt.datetime.now(dt.timezone.utc).date()
    for _ in range(7):
        # A RuntimeError here means EDGAR did not answer after retries. It
        # is allowed to propagate: "EDGAR is down" must not be reported as
        # "no filings in the last seven days".
        if edgar.efts_page(sess, probe_form, day, 0)["total"] > 0:
            return str(day)
        day -= dt.timedelta(days=1)
    return None


def load_cache() -> dict:
    d = io.read_json(CACHE) or {}
    # 🔑 An empty entry is a FAILED lookup that an earlier version cached as
    # if it were an answer, making that CIK unresolvable forever. Dropped on
    # load so it is looked up again.
    return {int(k): v for k, v in d.items() if v}


def save_cache(cache: dict) -> None:
    io.write_json(CACHE, {str(k): v for k, v in cache.items() if v})


def finnhub_insider(sess, ticker: str, key: str, lookback_days: int) -> dict:
    """-> {"ok": bool, "rows": [normalised], "reason": str}

    One retry on 429. A failure is reported as a failure, never as "this
    company has no insider transactions".
    """
    to_d = dt.datetime.now(dt.timezone.utc).date()
    params = {"symbol": ticker, "token": key,
              "from": (to_d - dt.timedelta(days=lookback_days)).isoformat(),
              "to": to_d.isoformat()}
    for attempt in (1, 2):
        try:
            r = sess.get(f4.FINNHUB_URL, params=params, timeout=20)
        except Exception as e:                       # noqa: BLE001
            return {"ok": False, "rows": [], "reason": type(e).__name__}
        if r.status_code == 429 and attempt == 1:
            time.sleep(20)
            continue
        if r.status_code != 200:
            return {"ok": False, "rows": [],
                    "reason": f"finnhub answered {r.status_code}"}
        try:
            data = (r.json() or {}).get("data") or []
        except ValueError:
            return {"ok": False, "rows": [], "reason": "finnhub: not JSON"}
        time.sleep(1.1)                              # free tier: 60 / minute
        return {"ok": True, "rows": f4.normalise(data, ticker), "reason": "ok"}
    return {"ok": False, "rows": [], "reason": "finnhub rate limited"}


def collect_filings(sess, day: str, univ: set, cache: dict, now: str) -> tuple:
    """-> (records, totals). One EDGAR census per form for `day`."""
    records, totals = [], {}
    for form in FORMS:
        res = edgar.efts_day(sess, form, day, MAX_PAGES)
        filings = edgar.dedupe_filings(res["hits"])
        n_in = 0
        for acc, hit in filings.items():
            src = hit.get("_source") or {}
            ciks = src.get("ciks") or []
            if form == "4":
                # Form 4 carries no ticker. Ask data.sec.gov which CIK is the
                # issuer: an operating company has tickers, a person does not.
                for c in ciks:
                    try:
                        ci = int(c)
                    except (TypeError, ValueError):
                        continue
                    if ci not in cache:
                        got = edgar.submissions(sess, ci)
                        if got:                  # never cache a failure
                            cache[ci] = got
                cik, tk = edgar.pick_issuer(ciks, cache)
            else:
                tks = edgar.tickers_from_display_names(src.get("display_names"))
                # Shortest first: the common share class, not the unit or
                # the warrant, when a filing lists several.
                tk = next((t for t in sorted(tks, key=lambda x: (len(x), x))
                           if t in univ), None)
                cik = int(ciks[0]) if ciks else None
            if tk and tk in univ:
                records.append(observation(form, hit, tk, cik, now))
                n_in += 1
        totals[form] = {"documents": res["total_documents"],
                        "filings": len(filings), "in_universe": n_in,
                        "truncated": res["truncated"],
                        "error": res.get("error")}
        if res.get("error"):
            print(f"::warning::observer: {form} census incomplete — "
                  f"{res['error']}")
        print(f"[observer] {form:<6} {len(filings):>5} filings market-wide, "
              f"{n_in:>4} in universe"
              f"{'  [TRUNCATED — a sample]' if res['truncated'] else ''}")
    return records, totals


def direction_pass(sess, day: str, now: str) -> dict:
    """Fill Form 4 directions for the last few day files, and derive events.

    🔑 Every fill and every derived event is stamped with THIS run's time,
    and CHIEF OF STAFF and ACTUARY read "what resolved in this run" across
    the recheck window -- not just the newest file. Finnhub lags EDGAR by
    about two days, so the newest file is almost entirely undirected when
    the brief is written. The first version of the brief read only that
    file, and from 11-16 to 11-24 it showed no purchase and no cluster buy
    on any night, while 185 purchases and 29 clusters resolved into older
    files nobody displayed.
    """
    all_files = io.day_files(OUT_DIR, ".jsonl")
    files = all_files[-F4_RECHECK_FILES:]
    loaded = {d: [upgrade(r) for r in io.read_jsonl(p)] for d, p in files}

    # Every cluster already reported, in ANY day file, so an extension of
    # one is not reported again.
    reported = set()
    for d, p in all_files:
        for r in (loaded.get(d) or io.read_jsonl(p)):
            if r.get("kind") == "derived_event":
                reported.add(cluster_key(r))

    need = []                                   # newest day first
    for d in sorted(loaded, reverse=True):
        for r in loaded[d]:
            if is_form4(r) and not r.get("direction") \
                    and r["ticker"] not in need:
                need.append(r["ticker"])
    # Then purchases whose first-purchase flag was never evaluated.
    for d in sorted(loaded, reverse=True):
        for r in loaded[d]:
            if (is_form4(r) and r.get("direction") in ("purchase", "mixed")
                    and "first_purchase" not in r
                    and r["ticker"] not in need):
                need.append(r["ticker"])

    out = {"key": bool(FINNHUB_KEY), "tickers_needed": len(need),
           "tickers_checked": 0, "filled": 0, "failed": 0, "capped": False,
           "clusters": 0, "first_purchases": 0}

    def pending() -> int:
        return sum(1 for recs in loaded.values() for r in recs
                   if is_form4(r) and not r.get("direction"))

    if not FINNHUB_KEY:
        out["note"] = (f"{NO_BODY}, and FINNHUB_API_KEY is not configured, "
                       f"so no second source was available")
        for d, recs in loaded.items():
            for r in recs:
                if is_form4(r) and not r.get("direction"):
                    r["direction_source"] = f"unavailable: {out['note']}"
            io.write_jsonl(os.path.join(OUT_DIR, f"{d}.jsonl"), recs)
        out["pending"] = pending()
        return out

    todo = need[:F4_MAX_TICKERS]
    out["capped"] = len(need) > len(todo)
    rows_by, unavailable = {}, {}
    for i, tk in enumerate(todo, 1):
        got = finnhub_insider(sess, tk, FINNHUB_KEY, F4_LOOKBACK_DAYS)
        if got["ok"]:
            rows_by[tk] = got["rows"]
        else:
            unavailable[tk] = got["reason"]
            out["failed"] += 1
        if i % 25 == 0:
            print(f"[observer] finnhub {i}/{len(todo)}")
    out["tickers_checked"] = len(todo)

    out["duplicate_clusters_suppressed"] = 0
    for d, recs in loaded.items():
        out["filled"] += enrich_direction(recs, rows_by, now, unavailable)
        have = {r.get("id") for r in recs}
        for tk, rows in rows_by.items():
            for ev in cluster_events(rows, d, now):
                if ev["id"] in have:
                    continue
                if cluster_key(ev) in reported:
                    out["duplicate_clusters_suppressed"] += 1
                    continue
                recs.append(ev)
                have.add(ev["id"])
                reported.add(cluster_key(ev))
                out["clusters"] += 1
            # 🔑 Evaluated on whichever run the direction ARRIVES, for any
            # day in the window. The first version required the filing day
            # to be the newest day, and the two-day Finnhub lag meant that
            # almost never happened: 20 of 205 purchases were ever checked.
            fp = first_purchases(rows, d)
            for r in recs:
                if (is_form4(r) and r.get("ticker") == tk
                        and r.get("direction") in ("purchase", "mixed")
                        and "first_purchase" not in r):
                    mine = [x for x in fp
                            if x["insider"] in (r.get("insiders") or [])]
                    r["first_purchase"] = mine or None
                    out["first_purchases"] += len(mine)
        io.write_jsonl(os.path.join(OUT_DIR, f"{d}.jsonl"), recs)

    # Any file that the NEXT run will not recheck is finalized now; that
    # includes files that fell out of the window before this rule existed.
    keep = {d for d, _ in all_files[-(F4_RECHECK_FILES - 1):]} \
        if F4_RECHECK_FILES > 1 else set()
    out["finalized"] = 0
    for d, p in all_files:
        if d in keep:
            continue
        recs = loaded.get(d)
        if recs is None:
            recs = [upgrade(r) for r in io.read_jsonl(p)]
        n = finalize_pending(recs, now, F4_RECHECK_FILES)
        if n or d in loaded:
            io.write_jsonl(os.path.join(OUT_DIR, f"{d}.jsonl"), recs)
        out["finalized"] += n

    out["pending"] = pending() - sum(
        1 for d in loaded if d not in keep for r in loaded[d]
        if is_form4(r) and r.get("direction_final"))
    out["note"] = (f"{NO_BODY}; direction comes from Finnhub by accession, "
                   f"which lags EDGAR by about two days — {out['pending']} "
                   f"still pending and rechecked on later runs"
                   + (f", {out['finalized']} marked final after "
                      f"{F4_RECHECK_FILES} rechecks" if out["finalized"] else ""))
    return out


def main() -> None:
    now = io.now_utc()
    w = io.watched_universe(UNIVERSE, PANEL, LEDGER)
    univ = w["tickers"]
    extra = (f" (+{w['thesis_added']} from open theses)"
             if w["thesis_added"] else "")
    print(f"[observer] watching {len(univ)} tickers — {w['source']}{extra}")
    if not univ:
        print("[observer] no universe — refusing to report an empty watch "
              "list as 'nothing happened'")
        sys.exit(2)

    sess = edgar.session(os.environ.get("SEC_USER_AGENT", ""))
    day = last_filing_day(sess)
    if day is None:
        print("[observer] no EDGAR filings found in the last 7 days")
        sys.exit(3)
    print(f"[observer] filing day {day}")

    cache = load_cache()
    new, totals = collect_filings(sess, day, univ, cache, now)
    save_cache(cache)

    path = os.path.join(OUT_DIR, f"{day}.jsonl")
    existing, bad = io.read_jsonl_counted(path)
    merged, n_added = merge_day(existing, new)
    io.write_jsonl(path, merged)
    print(f"[observer] {path}: {len(existing)} already recorded, "
          f"+{n_added} new"
          + (f", {bad} unreadable line(s) dropped" if bad else ""))

    direction = direction_pass(sess, day, now)
    records = io.read_jsonl(path)

    meta = {"generated_at": now, "filing_day": day, "watched": len(univ),
            "universe_source": w["source"], "thesis_added": w["thesis_added"],
            "totals": totals, "records": len(records),
            "added_this_run": n_added, "direction": direction,
            "forms": list(FORMS), "cik_cache": len(cache)}
    io.write_json(os.path.join(OUT_DIR, f"{day}.meta.json"), meta)
    text = digest(records, day, meta)
    io.write_text(os.path.join(OUT_DIR, f"{day}.md"), text)

    print(f"\n[observer] {len(records)} records for {day}")
    print(f"  direction: key={'yes' if direction['key'] else 'NO'} · checked "
          f"{direction['tickers_checked']} of {direction['tickers_needed']} "
          f"ticker(s) · filled {direction['filled']} · pending "
          f"{direction['pending']} · failed {direction['failed']}"
          f"{'  [CAPPED]' if direction['capped'] else ''}")
    print(f"  cluster events {direction['clusters']} (+"
          f"{direction.get('duplicate_clusters_suppressed', 0)} extensions of "
          f"clusters already reported, suppressed) · first purchases "
          f"{direction['first_purchases']} · finalized "
          f"{direction.get('finalized', 0)} · cik cache {len(cache)}")
    print("\n" + text[:1500])
    print("\n[observer] collected only. Nothing scored, nothing recommended, "
          "no order placed, K unchanged.")


if __name__ == "__main__":
    main()
