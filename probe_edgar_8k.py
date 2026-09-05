#!/usr/bin/env python3
"""Availability probe — can EDGAR supply earnings-ANNOUNCEMENT dates for the A4 universe?

The Finnhub probe (probe_earnings.py, run 33938076855) did not clear: four
quarters of history, fiscal period-end instead of announcement date, calendar
reaching back a month. A PEAD specification needs, for every announcement in
a three-year window, the exact bar the market first saw the number. That is
the filing timestamp of the 8-K that carried it -- Item 2.02, "Results of
Operations and Financial Condition" -- and EDGAR serves those for free, back
to 2004, with acceptance timestamps.

This probe answers, for the 907-name universe:

  1. CIK coverage    -- how many tickers map to an SEC registrant at all
  2. 8-K Item 2.02   -- how many announcements in the 1,095-day window, per
                        name, per year; how many are MATURED for a 63-bar
                        horizon; and the acceptance-hour distribution, which
                        decides whether "first bar after" is same-day or
                        next-day
  3. XBRL depth      -- on a 15-name sample, whether companyfacts carries
                        enough quarterly diluted EPS to build a seasonal
                        random-walk SUE (the estimate-free surprise measure)

Full sweep, not a sample, for 1 and 2: 907 submissions files at <=10 req/s
is about three minutes, and the event COUNT is what the spec design needs.

Availability only. Computes no return, writes nothing under data/, spends
no K.

SEC fair-access policy: a descriptive User-Agent is required. The default
identifies the repository; SEC_USER_AGENT overrides it.
"""
from __future__ import annotations

import collections
import os
import sys
import time

import pandas as pd
import requests

# `or`, not a default arg: the workflow passes SEC_USER_AGENT="" when the secret
# is unset, and an empty env var does NOT fall through to a default (the same
# trap as `inputs.x || default`). The SEC rejects an empty User-Agent.
UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research (https://github.com/Southpaw3234/Quant-Terminal)"
UNIVERSE = os.environ.get("QT_U_FILE", "data/universe/v27_universe.csv")
WINDOW_START, WINDOW_END = "2023-09-01", "2026-09-05"
MATURE_BY = "2026-06-01"            # ~63 bars before the data end
N_FACTS_SAMPLE = int(os.environ.get("QT_PROBE_FACTS_N", "15"))
SLEEP = 0.12                          # SEC asks for <= 10 req/s
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBS_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def _get(sess, url):
    """-> (status, json|None). status in {'ok','404','err'}."""
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=30)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1)); continue
        if r.status_code == 200:
            return "ok", r.json()
        if r.status_code == 404:
            return "404", None
        last = r.status_code
        time.sleep(1.5 * (attempt + 1))
    return f"err:{locals().get('last', '')}", None


def _universe() -> list:
    df = pd.read_csv(UNIVERSE)
    return sorted(df["ticker"].astype(str).str.upper().unique().tolist())


def _cik_map(sess) -> dict:
    st, body = _get(sess, TICKERS_URL)
    if st != "ok":
        print(f"[probe] company_tickers.json unavailable ({st})"); sys.exit(2)
    m = {}
    for rec in body.values():
        m[str(rec["ticker"]).upper()] = int(rec["cik_str"])
    return m


def _lookup(tk: str, m: dict):
    for cand in (tk, tk.replace(".", "-"), tk.replace("-", "."), tk.replace("-", ""), tk.replace(".", "")):
        if cand in m:
            return m[cand]
    return None


def _eight_k_202(sess, cik: int):
    """All 8-K filings carrying Item 2.02 inside the window, incl. older chunks."""
    st, body = _get(sess, SUBS_URL.format(name=f"CIK{cik:010d}.json"))
    time.sleep(SLEEP)
    if st != "ok":
        return st, []
    out = []
    chunks = [body.get("filings", {}).get("recent", {})]
    for extra in body.get("filings", {}).get("files", []):
        # only fetch older chunks that overlap the window
        if str(extra.get("filingTo", "")) >= WINDOW_START:
            st2, b2 = _get(sess, SUBS_URL.format(name=extra["name"]))
            time.sleep(SLEEP)
            if st2 == "ok":
                chunks.append(b2)
    for ch in chunks:
        forms = ch.get("form", []); dates = ch.get("filingDate", [])
        items = ch.get("items", []); acc = ch.get("acceptanceDateTime", [])
        rep = ch.get("reportDate", [])
        for i in range(len(forms)):
            if forms[i] != "8-K":
                continue
            if "2.02" not in str(items[i] if i < len(items) else ""):
                continue
            d = str(dates[i])
            if not (WINDOW_START <= d <= WINDOW_END):
                continue
            out.append({
                "filingDate": d,
                "acceptance": str(acc[i]) if i < len(acc) else "",
                "reportDate": str(rep[i]) if i < len(rep) else "",
                "items": str(items[i]),
            })
    # one event per filing date per name (a same-day 8-K + 8-K pair is one announcement)
    seen = set(); dedup = []
    for r in sorted(out, key=lambda r: r["filingDate"]):
        if r["filingDate"] in seen:
            continue
        seen.add(r["filingDate"]); dedup.append(r)
    return "ok", dedup


def _eps_depth(sess, cik: int):
    st, body = _get(sess, FACTS_URL.format(cik=cik))
    time.sleep(SLEEP)
    if st != "ok":
        return st, 0, 0, ""
    facts = body.get("facts", {}).get("us-gaap", {})
    node = facts.get("EarningsPerShareDiluted") or facts.get("EarningsPerShareBasic") or {}
    rows = node.get("units", {}).get("USD/shares", [])
    q_ends = set(); fy_ends = set()
    for r in rows:
        end = str(r.get("end", ""))
        if not (WINDOW_START[:4] <= end[:4] <= WINDOW_END[:4]):
            continue
        if r.get("form") == "10-Q" and r.get("fp") in ("Q1", "Q2", "Q3"):
            q_ends.add(end)
        elif r.get("form") == "10-K" and r.get("fp") == "FY":
            fy_ends.add(end)
    tag = "EarningsPerShareDiluted" if "EarningsPerShareDiluted" in facts else ("EarningsPerShareBasic" if "EarningsPerShareBasic" in facts else "NONE")
    return "ok", len(q_ends), len(fy_ends), tag


def main() -> None:
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    tickers = _universe()
    print(f"[probe] {len(tickers)} tickers from {UNIVERSE}; window {WINDOW_START}..{WINDOW_END}; UA={UA!r}\n")

    # ── 1. CIK coverage ────────────────────────────────────────────────
    print("=" * 72); print("1. ticker -> CIK  (company_tickers.json)"); print("=" * 72)
    m = _cik_map(sess)
    ciks = {tk: _lookup(tk, m) for tk in tickers}
    mapped = {tk: c for tk, c in ciks.items() if c}
    unmapped = sorted(tk for tk, c in ciks.items() if not c)
    print(f"  mapped {len(mapped)}/{len(tickers)}  ({100*len(mapped)/len(tickers):.1f}%)")
    print(f"  unmapped ({len(unmapped)}): {' '.join(unmapped[:40])}{' ...' if len(unmapped) > 40 else ''}\n")

    # ── 2. 8-K Item 2.02 sweep ─────────────────────────────────────────
    print("=" * 72); print("2. 8-K Item 2.02 filings (earnings announcements), FULL SWEEP"); print("=" * 72)
    t0 = time.time()
    status = collections.Counter(); events = []; per_name = {}
    for k, (tk, cik) in enumerate(sorted(mapped.items())):
        st, evs = _eight_k_202(sess, cik)
        status[st] += 1
        per_name[tk] = len(evs)
        for e in evs:
            e["ticker"] = tk; events.append(e)
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(mapped)} names, {len(events)} events so far, {time.time()-t0:.0f}s")
    print(f"\n  submissions fetched: {dict(status)}  in {time.time()-t0:.0f}s")
    if events:
        print(f"  record: {events[0]}")
    n_with = sum(1 for v in per_name.values() if v > 0)
    counts = sorted(per_name.values())
    print(f"\n  names with >=1 announcement: {n_with}/{len(mapped)}  ({100*n_with/max(1,len(mapped)):.1f}%)")
    print(f"  TOTAL 8-K 2.02 announcements in window: {len(events)}")
    if counts:
        med = counts[len(counts)//2]
        print(f"  per name over ~3.0y: min={counts[0]} median={med} max={counts[-1]}   "
              f"(quarterly reporting = ~12)  >=10: {sum(1 for v in counts if v >= 10)}  ==0: {sum(1 for v in counts if v == 0)}")
    by_year = collections.Counter(e["filingDate"][:4] for e in events)
    print(f"  by year: {dict(sorted(by_year.items()))}")
    mature = [e for e in events if e["filingDate"] <= MATURE_BY]
    print(f"  MATURED for a 63-bar horizon (filed <= {MATURE_BY}): {len(mature)}")
    hours = collections.Counter()
    for e in events:
        a = e["acceptance"]
        if "T" in a:
            hours[a.split("T")[1][:2]] += 1
    print(f"  acceptance hour histogram (timestamp as served; decides same-day vs next-bar entry):")
    for h in sorted(hours):
        print(f"    {h}h  {hours[h]:>5}  {'#' * min(60, hours[h] // max(1, len(events) // 300))}")
    print(f"  (no timestamp: {len(events) - sum(hours.values())})")
    # reportDate sanity: 2.02 8-Ks report the period end; the gap tells the lag
    lags = []
    for e in events:
        try:
            lags.append((pd.Timestamp(e["filingDate"]) - pd.Timestamp(e["reportDate"])).days)
        except Exception:
            pass
    if lags:
        lags.sort()
        print(f"  filing minus reportDate (days): median {lags[len(lags)//2]}  p10 {lags[len(lags)//10]}  p90 {lags[9*len(lags)//10]}")

    # ── 3. XBRL EPS depth on a sample ──────────────────────────────────
    print("\n" + "=" * 72); print(f"3. companyfacts quarterly diluted EPS depth ({N_FACTS_SAMPLE}-name sample)"); print("=" * 72)
    import random
    rnd = random.Random(20260905)
    sample = sorted(rnd.sample(sorted(mapped), min(N_FACTS_SAMPLE, len(mapped))))
    ok_q = []
    for tk in sample:
        st, nq, nfy, tag = _eps_depth(sess, mapped[tk])
        print(f"  {tk:<6} {st:<4} 10-Q quarters={nq:<3} 10-K years={nfy:<2} {tag}")
        if st == "ok":
            ok_q.append(nq)
    if ok_q:
        print(f"\n  facts ok {len(ok_q)}/{len(sample)}; 10-Q quarters in window: median {sorted(ok_q)[len(ok_q)//2]}, "
              f"names with >=8: {sum(1 for q in ok_q if q >= 8)}  (Q4 must be derived FY - Q1 - Q2 - Q3)")

    # ── verdict ────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    cov = n_with / max(1, len(tickers))
    if cov >= 0.75 and len(mature) >= 400:
        print(f"✅ CLEARS on availability: {n_with}/{len(tickers)} names announce via 8-K 2.02, "
              f"{len(mature)} matured announcements. Enough to define a top-decile event set well above N>=40.")
    else:
        print(f"🔴 DOES NOT CLEAR: coverage {cov:.0%}, matured {len(mature)}.")
    print("[probe] done. Availability only -- no return computed, nothing written, K untouched.")


if __name__ == "__main__":
    main()
