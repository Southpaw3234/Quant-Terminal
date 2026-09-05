#!/usr/bin/env python3
"""Availability probe — constraint events for specification #4.

docs/V27_SPEC4_SKETCH.md §⑤ argues for a mechanism that persists because
someone is CONSTRAINED rather than because someone is UNINFORMED. Information
gets arbitraged once published; a constraint persists as long as the rule
creating it persists.

This asks EDGAR whether the three candidates are countable on the A4 universe,
and at what density. Prior is argued in the sketch; this measures only
availability.

  8-K Item 3.01   listing-deficiency notice. A company falls below a continued
                  listing rule and funds with listing mandates must sell
                  regardless of view. The constraint is a written exchange rule
                  and the holder has no discretion -- the strongest prior of
                  the three.
  424B5           follow-on offering: forced supply at a negotiated discount.
                  Abundant, but partly an INFORMATION event, since issuers
                  choose when to sell stock.
  SC TO-I         issuer tender offer: forced demand, the mirror image.
  25 / 25-NSE     delisting itself (also enumerated by probe_ceiling.py, from
                  the other direction -- there by exchange filing, here by
                  whether names in OUR universe left).

Tax-loss selling, the second-ranked candidate in the sketch, is deliberately
absent: it needs prices and a calendar and no filing at all, so there is
nothing to probe.

Samples rather than sweeps -- 2 requests per name makes 907 a 20-minute job,
and density is what availability asks.

Availability only. Computes no return, writes nothing under data/, spends no K.
"""
from __future__ import annotations

import collections
import os
import random
import sys
import time

import pandas as pd

UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
UNIVERSE = os.environ.get("QT_U_FILE", "data/universe/v27_universe.csv")
WINDOW_START, WINDOW_END = "2023-09-01", "2026-09-05"
N_SAMPLE = int(os.environ.get("QT_PROBE_N", "120"))
SEED = 20260905
SLEEP = 0.12
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBS_URL = "https://data.sec.gov/submissions/{name}"

# form -> 8-K item to require (None = the form itself is the event)
TARGETS = {
    "8-K:3.01": ("8-K", "3.01"),
    "424B5": ("424B5", None),
    "SC TO-I": ("SC TO-I", None),
    "25-NSE": ("25-NSE", None),
}


def _get(sess, url, params=None):
    import requests
    for attempt in range(3):
        try:
            r = sess.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(1.2 * (attempt + 1)); continue
        if r.status_code == 200:
            try:
                return "ok", r.json()
            except ValueError:
                return "err", None
        if r.status_code == 404:
            return "404", None
        time.sleep(1.2 * (attempt + 1))
    return "err", None


def resolve_cik(sess, tk: str):
    st, body = _get(sess, FTS_URL, params={
        "q": '"Item 2.02"', "forms": "8-K", "entityName": tk,
        "startdt": WINDOW_START, "enddt": WINDOW_END})
    time.sleep(SLEEP)
    if st != "ok":
        return None
    for h in ((body.get("hits", {}) or {}).get("hits", []) or []):
        src = h.get("_source", {}) or {}
        names = " ".join(src.get("display_names", []) or [])
        if f"({tk})" in names or f"({tk}," in names or f", {tk})" in names:
            ciks = src.get("ciks") or []
            if ciks:
                return int(ciks[0])
    return None


def count_events(sess, cik: int) -> collections.Counter:
    st, body = _get(sess, SUBS_URL.format(name=f"CIK{cik:010d}.json"))
    time.sleep(SLEEP)
    got = collections.Counter()
    if st != "ok":
        return got
    chunks = [(body.get("filings", {}) or {}).get("recent", {}) or {}]
    for extra in (body.get("filings", {}) or {}).get("files", []) or []:
        if str(extra.get("filingTo", "")) >= WINDOW_START:
            st2, b2 = _get(sess, SUBS_URL.format(name=extra["name"]))
            time.sleep(SLEEP)
            if st2 == "ok":
                chunks.append(b2)
    for ch in chunks:
        forms = ch.get("form", []) or []
        dates = ch.get("filingDate", []) or []
        items = ch.get("items", []) or []
        for i in range(len(forms)):
            d = str(dates[i] if i < len(dates) else "")
            if not (WINDOW_START <= d <= WINDOW_END):
                continue
            f = str(forms[i])
            it = str(items[i] if i < len(items) else "")
            for label, (want_form, want_item) in TARGETS.items():
                if f != want_form:
                    continue
                if want_item and want_item not in it:
                    continue
                got[label] += 1
    return got


def main() -> None:
    import requests
    if not os.path.exists(UNIVERSE):
        print(f"[constraints] no universe at {UNIVERSE}"); sys.exit(1)
    tickers = sorted(pd.read_csv(UNIVERSE)["ticker"].astype(str).str.upper().unique())
    sample = sorted(random.Random(SEED).sample(tickers, min(N_SAMPLE, len(tickers))))
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    print(f"[constraints] universe {len(tickers)}; sampling {len(sample)}; "
          f"window {WINDOW_START}..{WINDOW_END}\n")

    t0 = time.time()
    totals = collections.Counter()
    names_with = collections.Counter()
    resolved = 0
    for k, tk in enumerate(sample):
        cik = resolve_cik(sess, tk)
        if cik is None:
            continue
        resolved += 1
        got = count_events(sess, cik)
        for label, n in got.items():
            totals[label] += n
            if n:
                names_with[label] += 1
        if (k + 1) % 40 == 0:
            print(f"  ...{k+1}/{len(sample)} names, {resolved} resolved, {time.time()-t0:.0f}s")

    scale = len(tickers) / max(1, resolved)
    print(f"\n  resolved {resolved}/{len(sample)} in {time.time()-t0:.0f}s\n")
    print("=" * 78)
    print(f"  {'event':<14} {'filings':<9} {'names':<8} {'% names':<9} {'projected@907':<14}")
    print("=" * 78)
    for label in TARGETS:
        pct = 100 * names_with[label] / max(1, resolved)
        print(f"  {label:<14} {totals[label]:<9} {names_with[label]:<8} {pct:<9.1f} "
              f"{int(totals[label]*scale):<14,}")

    print("\n  READING THESE NUMBERS")
    d = int(totals['8-K:3.01'] * scale)
    print(f"  * 8-K Item 3.01 projects to ~{d:,} universe-wide. The N>=40 floor needs 40 that")
    print(f"    SETTLE at 63 bars after dedup, so anything above roughly 200 is workable.")
    if names_with["8-K:3.01"] == 0:
        print("    🔴 NONE FOUND — the strongest-prior candidate is not available on this universe.")
    elif d >= 200:
        print("    ✅ Workable at this density.")
    else:
        print("    🟡 THIN. Would need a wider universe or a longer window, and both are")
        print("       specification changes that must be declared, not tuned afterwards.")
    print(f"  * 424B5 projects to ~{int(totals['424B5']*scale):,} — abundant, but the sketch ranks")
    print(f"    it BELOW 3.01 on prior, not on availability. Availability was never its problem.")
    print(f"  * SC TO-I at ~{int(totals['SC TO-I']*scale):,} is the thin one; issuer tenders are rare")
    print(f"    in this size band.")
    print(f"  * 25-NSE at ~{int(totals['25-NSE']*scale):,} counts names in OUR universe that LEFT —")
    print(f"    expected to be near zero BY CONSTRUCTION, since the universe is today's listings.")
    print(f"    A non-zero count here would mean the screen is staler than believed.")
    print("\n[constraints] done. Availability only — no return computed, nothing written, K untouched.")


if __name__ == "__main__":
    main()
