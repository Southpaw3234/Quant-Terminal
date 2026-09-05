#!/usr/bin/env python3
"""Availability probe — can EDGAR supply earnings-ANNOUNCEMENT dates for the A4 universe?

The Finnhub probe (probe_earnings.py, run 33938076855) did not clear: four
quarters of history, fiscal period-end instead of announcement date, calendar
reaching back a month. A PEAD specification needs, for every announcement in
a three-year window, the exact bar the market first saw the number. That is
the filing timestamp of the 8-K that carried it -- Item 2.02, "Results of
Operations and Financial Condition" -- and EDGAR serves those for free with
acceptance timestamps.

TWO HOST FACTS, MEASURED 2026-09-05 (runs 33938339262 / 33938394855 and a
local curl matrix), because both cost a failed run to learn:

  * www.sec.gov 403s EVERYTHING from this network -- static files, Archives,
    company_tickers.json -- with "Request Rate Threshold Exceeded". It failed
    from the GitHub runner AND from the operator's machine, so it is not a
    datacenter block and not something a retry fixes. The canonical
    ticker->CIK map lives there and is therefore UNREACHABLE.
  * data.sec.gov and efts.sec.gov both serve 200 -- but ONLY to a plain
    descriptive User-Agent. A UA containing a URL is 403'd by the same WAF.
    That is why the default below has no link in it.

So the ticker->CIK map is rebuilt from EDGAR full-text search, which returns
`ciks`, `display_names` (carrying the ticker), `items` and `file_date` per
hit, and accepts a date range. Announcement detail then comes from the
submissions API, whose structured `items` field is authoritative and whose
`acceptanceDateTime` decides same-day vs next-bar entry.

SAMPLES rather than sweeps: ~2 requests per name at roughly a second each
makes 907 names a 20-minute job, and availability is a question about
coverage and per-name density, which a 150-name random sample answers to
within a few points. The full sweep belongs in the extractor, which runs
once and commits its output.

Availability only. Computes no return, writes nothing under data/, spends
no K.
"""
from __future__ import annotations

import collections
import os
import random
import sys
import time

import pandas as pd
import requests

# No URL in the default: the SEC's WAF 403s a User-Agent containing one, on
# data.sec.gov as well as www. Measured, not guessed. SEC_USER_AGENT overrides.
UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
UNIVERSE = os.environ.get("QT_U_FILE", "data/universe/v27_universe.csv")
WINDOW_START, WINDOW_END = "2023-09-01", "2026-09-05"
MATURE_BY = "2026-06-01"              # ~63 trading bars before the data end
N_SAMPLE = int(os.environ.get("QT_PROBE_N", "150"))
N_FACTS_SAMPLE = int(os.environ.get("QT_PROBE_FACTS_N", "15"))
SEED = 20260905
SLEEP = 0.12                          # SEC fair access asks for <= 10 req/s
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBS_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def _get(sess, url, params=None):
    """-> (status, json|None). status is 'ok', '404', or 'err:<code>'."""
    last = ""
    for attempt in range(3):
        try:
            r = sess.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            last = type(exc).__name__
            time.sleep(1.5 * (attempt + 1)); continue
        if r.status_code == 200:
            try:
                return "ok", r.json()
            except ValueError:
                return "err:notjson", None
        if r.status_code == 404:
            return "404", None
        last = r.status_code
        time.sleep(1.5 * (attempt + 1))
    return f"err:{last}", None


def _universe() -> list:
    df = pd.read_csv(UNIVERSE)
    return sorted(df["ticker"].astype(str).str.upper().unique().tolist())


def _resolve_cik(sess, tk: str):
    """ticker -> (cik, fts_hits) via full-text search over its 8-K 2.02 filings.

    display_names look like 'Fossil Group, Inc.  (FOSL)  (CIK 0000883569)',
    so the ticker is confirmed on the hit itself rather than assumed from the
    query -- entityName is a fuzzy match and will happily return a different
    registrant.
    """
    st, body = _get(sess, FTS_URL, params={
        "q": '"Item 2.02"', "forms": "8-K", "entityName": tk,
        "startdt": WINDOW_START, "enddt": WINDOW_END,
    })
    time.sleep(SLEEP)
    if st != "ok":
        return None, 0, st
    hits = (body.get("hits", {}) or {}).get("hits", []) or []
    total = ((body.get("hits", {}) or {}).get("total", {}) or {}).get("value", 0)
    for h in hits:
        src = h.get("_source", {}) or {}
        names = " ".join(src.get("display_names", []) or [])
        if f"({tk})" in names or f"({tk}," in names or f", {tk})" in names:
            ciks = src.get("ciks") or []
            if ciks:
                return int(ciks[0]), total, "ok"
    return None, total, "no-ticker-match"


def _eight_k_202(sess, cik: int):
    """Item 2.02 8-Ks inside the window, from the authoritative submissions doc."""
    st, body = _get(sess, SUBS_URL.format(name=f"CIK{cik:010d}.json"))
    time.sleep(SLEEP)
    if st != "ok":
        return st, []
    chunks = [(body.get("filings", {}) or {}).get("recent", {}) or {}]
    for extra in (body.get("filings", {}) or {}).get("files", []) or []:
        if str(extra.get("filingTo", "")) >= WINDOW_START:   # only overlapping chunks
            st2, b2 = _get(sess, SUBS_URL.format(name=extra["name"]))
            time.sleep(SLEEP)
            if st2 == "ok":
                chunks.append(b2)
    out = []
    for ch in chunks:
        forms = ch.get("form", []) or []
        dates = ch.get("filingDate", []) or []
        items = ch.get("items", []) or []
        acc = ch.get("acceptanceDateTime", []) or []
        rep = ch.get("reportDate", []) or []
        for i in range(len(forms)):
            if forms[i] != "8-K":
                continue
            if "2.02" not in str(items[i] if i < len(items) else ""):
                continue
            d = str(dates[i] if i < len(dates) else "")
            if not (WINDOW_START <= d <= WINDOW_END):
                continue
            out.append({
                "filingDate": d,
                "acceptance": str(acc[i]) if i < len(acc) else "",
                "reportDate": str(rep[i]) if i < len(rep) else "",
                "items": str(items[i]),
            })
    # one announcement per date: an 8-K plus its same-day amendment is one event
    seen, dedup = set(), []
    for r in sorted(out, key=lambda r: r["filingDate"]):
        if r["filingDate"] in seen:
            continue
        seen.add(r["filingDate"]); dedup.append(r)
    return "ok", dedup


def _eps_depth(sess, cik: int):
    """Quarterly diluted EPS depth — the estimate-free SUE needs 8+ quarters."""
    st, body = _get(sess, FACTS_URL.format(cik=cik))
    time.sleep(SLEEP)
    if st != "ok":
        return st, 0, 0, ""
    facts = (body.get("facts", {}) or {}).get("us-gaap", {}) or {}
    tag = ("EarningsPerShareDiluted" if "EarningsPerShareDiluted" in facts
           else "EarningsPerShareBasic" if "EarningsPerShareBasic" in facts else "NONE")
    node = facts.get(tag, {}) if tag != "NONE" else {}
    rows = (node.get("units", {}) or {}).get("USD/shares", []) or []
    q_ends, fy_ends = set(), set()
    for r in rows:
        end = str(r.get("end", ""))
        if not (WINDOW_START[:4] <= end[:4] <= WINDOW_END[:4]):
            continue
        if r.get("form") == "10-Q" and r.get("fp") in ("Q1", "Q2", "Q3"):
            q_ends.add(end)
        elif r.get("form") == "10-K" and r.get("fp") == "FY":
            fy_ends.add(end)
    return "ok", len(q_ends), len(fy_ends), tag


def main() -> None:
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    universe = _universe()
    sample = sorted(random.Random(SEED).sample(universe, min(N_SAMPLE, len(universe))))
    print(f"[probe] universe {len(universe)} from {UNIVERSE}; sampling {len(sample)}; "
          f"window {WINDOW_START}..{WINDOW_END}; UA={UA!r}\n")

    # ── 1. ticker -> CIK, via full-text search ─────────────────────────
    print("=" * 72); print("1. ticker -> CIK  (efts full-text search; www.sec.gov is 403 from here)"); print("=" * 72)
    t0 = time.time()
    ciks, fails = {}, collections.Counter()
    for k, tk in enumerate(sample):
        cik, _tot, st = _resolve_cik(sess, tk)
        if cik:
            ciks[tk] = cik
        else:
            fails[st] += 1
        if (k + 1) % 50 == 0:
            print(f"  ...{k+1}/{len(sample)} resolved={len(ciks)} {time.time()-t0:.0f}s")
    print(f"\n  resolved {len(ciks)}/{len(sample)}  ({100*len(ciks)/max(1,len(sample)):.1f}%)  in {time.time()-t0:.0f}s")
    print(f"  unresolved reasons: {dict(fails)}")
    print("  (an unresolved name has NO Item 2.02 8-K in the window, or is not an SEC registrant —\n"
          "   a fund, a foreign issuer filing 6-K, or a shell. Both are real exclusions, not gaps.)\n")

    # ── 2. 8-K Item 2.02 announcements ─────────────────────────────────
    print("=" * 72); print("2. 8-K Item 2.02 announcements (submissions API, structured items)"); print("=" * 72)
    t0 = time.time()
    status = collections.Counter(); events = []; per_name = {}
    for k, (tk, cik) in enumerate(sorted(ciks.items())):
        st, evs = _eight_k_202(sess, cik)
        status[st] += 1
        per_name[tk] = len(evs)
        for e in evs:
            e["ticker"] = tk; events.append(e)
        if (k + 1) % 50 == 0:
            print(f"  ...{k+1}/{len(ciks)} names, {len(events)} announcements, {time.time()-t0:.0f}s")
    print(f"\n  submissions fetched: {dict(status)} in {time.time()-t0:.0f}s")
    if events:
        print(f"  record: {events[0]}")
    counts = sorted(per_name.values())
    n_with = sum(1 for v in counts if v > 0)
    print(f"\n  names with >=1 announcement: {n_with}/{len(sample)} of the SAMPLE "
          f"({100*n_with/max(1,len(sample)):.1f}%)")
    print(f"  announcements in sample: {len(events)}")
    if counts:
        print(f"  per name over ~3.0y: min={counts[0]} median={counts[len(counts)//2]} max={counts[-1]}"
              f"   (quarterly reporting = ~12)   >=10: {sum(1 for v in counts if v >= 10)}"
              f"   ==0: {sum(1 for v in counts if v == 0)}")
    by_year = collections.Counter(e["filingDate"][:4] for e in events)
    print(f"  by year: {dict(sorted(by_year.items()))}")
    mature = [e for e in events if e["filingDate"] <= MATURE_BY]
    scale = len(universe) / max(1, len(sample))
    print(f"  MATURED for a 63-bar horizon (filed <= {MATURE_BY}): {len(mature)} in sample"
          f"  ->  ~{int(len(mature)*scale):,} projected across all {len(universe)} names")

    # acceptanceDateTime is served in UTC (the trailing Z). Reading the raw
    # hour as ET puts a 21:01Z after-close release at "9pm" and a 12:00Z
    # pre-market release "inside the session" -- backwards, and it would drive
    # the entry-bar rule the wrong way. Convert, then bucket against the
    # actual session. DST matters here: the same clock hour is a different
    # session position in January and July, so tz_convert does the work.
    hours = collections.Counter(); buckets = collections.Counter(); n_ts = 0
    for e in events:
        a = e["acceptance"]
        if "T" not in a:
            continue
        try:
            et = pd.Timestamp(a).tz_convert("America/New_York")
        except Exception:
            continue
        n_ts += 1
        hours[f"{et.hour:02d}"] += 1
        mins = et.hour * 60 + et.minute
        if mins < 9 * 60 + 30:
            buckets["pre-market (before 09:30 ET)"] += 1
        elif mins < 16 * 60:
            buckets["INSIDE the session (09:30-16:00 ET)"] += 1
        else:
            buckets["after the close (after 16:00 ET)"] += 1
    print("\n  acceptance hour, converted to ET (served as UTC; DST-aware):")
    unit = max(1, n_ts // 60)
    for h in sorted(hours):
        print(f"    {h}h ET {hours[h]:>5}  {'#' * (hours[h] // unit)}")
    print(f"  no timestamp: {len(events) - n_ts}")
    for b, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"    {v:>5}  {100*v/max(1,n_ts):>5.1f}%  {b}")
    inside = buckets["INSIDE the session (09:30-16:00 ET)"]
    if n_ts:
        print(f"\n  ENTRY-BAR CONSEQUENCE: {100*(n_ts-inside)/n_ts:.0f}% of announcements land OUTSIDE "
              f"the session,\n  so for almost every event the first tradeable bar is unambiguous — the next "
              f"open.\n  The {inside} ({100*inside/n_ts:.0f}%) that land intraday are the ones where a "
              f"same-day close would\n  capture part of the announcement move itself, and the study must skip "
              f"to the next bar.")

    lags = []
    for e in events:
        try:
            lags.append((pd.Timestamp(e["filingDate"]) - pd.Timestamp(e["reportDate"])).days)
        except Exception:
            pass
    if lags:
        lags.sort()
        print(f"  filing minus reportDate (days): p10 {lags[len(lags)//10]}  median {lags[len(lags)//2]}  "
              f"p90 {lags[9*len(lags)//10]}")

    # ── 3. XBRL EPS depth for an estimate-free SUE ─────────────────────
    print("\n" + "=" * 72); print(f"3. companyfacts quarterly EPS depth ({N_FACTS_SAMPLE}-name sub-sample)"); print("=" * 72)
    subs = sorted(random.Random(SEED).sample(sorted(ciks), min(N_FACTS_SAMPLE, len(ciks)))) if ciks else []
    ok_q = []
    for tk in subs:
        st, nq, nfy, tag = _eps_depth(sess, ciks[tk])
        print(f"  {tk:<6} {st:<9} 10-Q quarters={nq:<3} 10-K years={nfy:<2} {tag}")
        if st == "ok":
            ok_q.append(nq)
    if ok_q:
        ok_q.sort()
        print(f"\n  facts ok {len(ok_q)}/{len(subs)}; 10-Q quarters in window: median {ok_q[len(ok_q)//2]}; "
              f"names with >=8: {sum(1 for q in ok_q if q >= 8)}  (Q4 is derived FY - Q1 - Q2 - Q3)")

    # ── verdict ────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    cov = n_with / max(1, len(sample))
    proj = int(len(mature) * scale)
    if cov >= 0.70 and proj >= 1000:
        print(f"✅ CLEARS ON AVAILABILITY: {cov:.0%} of names announce via 8-K Item 2.02, "
              f"~{proj:,} matured announcements across the universe.")
        print("   Announcement DATE and TIME are exact and free. That is the half Finnhub could not supply.")
        print("   Still unresolved, and NOT an availability question: the surprise MEASURE. No free")
        print("   analyst consensus exists, so a spec must use announcement-window abnormal return")
        print("   (Chan-Jegadeesh-Lakonishok) or a seasonal-random-walk SUE from the EPS depth above.")
    else:
        print(f"🔴 DOES NOT CLEAR: coverage {cov:.0%}, ~{proj:,} matured announcements projected.")
    print("[probe] done. Availability only -- no return computed, nothing written, K untouched.")


if __name__ == "__main__":
    main()
