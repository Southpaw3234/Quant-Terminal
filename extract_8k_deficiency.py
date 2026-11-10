#!/usr/bin/env python3
"""Event extractor for specification #4 — 8-K Item 3.01 listing deficiency.

SELECTION ONLY. This file computes WHICH events exist and WHEN they may be
entered. It computes no forward return, consults no Referee, and spends no K.

─────────────────────────────────────────────────────────────────────────────
WHY ITEM 3.01, AND WHY IT IS NOT ANOTHER SPEC #3

Specifications #1 to #3 all tested mechanisms that persist because someone is
UNINFORMED -- insider buying and earnings surprises are information, and
information is arbitraged once published. All three found nothing, and spec #3
found a large effect pointing the wrong way.

Item 3.01 is a notice that a company has fallen below a continued-listing rule
(price, market capitalisation, public float, or similar). Funds with listing
mandates must then sell REGARDLESS OF VIEW. The mechanism persists because the
holder is CONSTRAINED, not because the holder is uninformed: the rule creating
the constraint does not get arbitraged away, because the seller is not trying
to beat the market.

🔑 THE SELECTION RULE CONDITIONS ON A FILING, NOT ON A PRICE.

Spec #3 selected on "CAR[A, A+1] >= +5.0%" and died at t = -4.04 because
buying after a large positive move buys mean reversion -- the declaration had
named that downward bias in advance, and it was larger than the effect it was
a caveat about. There is NO price condition anywhere in this extractor. That
failure mode cannot recur here, and saying so now is cheap; saying it after a
pass would be a rationalisation.

─────────────────────────────────────────────────────────────────────────────
WHAT THIS DELIBERATELY DOES NOT DO

* No return is computed. The event file is an input to the measurement, and
  the measurement is a SEPARATE run that the Referee gates.
* No threshold is tuned. There is nothing to tune: an Item 3.01 filing either
  exists in the window or does not.
* No universe is invented. Membership is read point-in-time from the frozen
  V29 panel, as of the month-end ON OR BEFORE the announcement session, so a
  company that only later became eligible is not selected with hindsight.

─────────────────────────────────────────────────────────────────────────────
ENTRY, AND WHY IT IS A+1 RATHER THAN SPEC #3'S A+2

A is the first session that can react to the filing. Spec #3 used sessions
[A, A+1] to BUILD its selection, so it had to enter at A+2 to stay strictly
clear of its own selection window. This specification's selection uses no
price at all, so A+1 -- the first session strictly after the news is public --
is both correct and the earliest honestly tradeable bar.

event_ts is therefore written as the close of A. The measurement enters at
`searchsorted(event_ts, side="right")`, which lands on A+1.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

# ── declared parameters. Changing any of these is a NEW specification. ──
WINDOW_START = "2015-09-30"      # the frozen V29 panel's first month-end
WINDOW_END = "2025-08-31"        # the frozen V29 panel's last month-end
BENCHMARK = "IWM"                # Russell 2000: the hedge this spec can borrow
EVENT_TYPE = "8k_item301_listing_deficiency"
ITEM = "3.01"

PANEL = os.environ.get("QT_DEF_PANEL", "data/universe/v29_universe_pit.csv")
OUT_CSV = os.environ.get("QT_DEF_OUT", "data/events/events_8k_item301.csv")
LIMIT = int(os.environ.get("QT_DEF_LIMIT", "0"))      # 0 = whole universe

CLOSE_HOUR, CLOSE_MIN = 16, 0
UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
SLEEP = 0.12
ET = "America/New_York"

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBS_URL = "https://data.sec.gov/submissions/{name}"


# ═══════════════════════════════════════════════ pure: sessions and entry

def announcement_session(acceptance, sessions) -> "pd.Timestamp | None":
    """The first session that can react to news accepted at `acceptance`.

    Identical in behaviour to spec #3's, deliberately: the rule for when the
    market can first act on a filing does not depend on which item it is.
    `sessions` is the benchmark's own calendar -- exactly the days a price
    exists, which is the operationally correct definition.
    """
    if acceptance is None or (isinstance(acceptance, float) and np.isnan(acceptance)):
        return None
    t = pd.Timestamp(acceptance)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t_et = t.tz_convert(ET)
    d = pd.Timestamp(t_et.date())
    before_close = (t_et.hour, t_et.minute) < (CLOSE_HOUR, CLOSE_MIN)
    i = sessions.searchsorted(d, side="left")
    if before_close and i < len(sessions) and sessions[i] == d:
        return sessions[i]
    j = sessions.searchsorted(d, side="right")
    return sessions[j] if j < len(sessions) else None


def event_ts_for(a_day, sessions) -> "pd.Timestamp | None":
    """event_ts = the close of A, so entry lands on A+1.

    The measurement enters at searchsorted(event_ts, side='right'). Writing A
    here yields A+1 -- strictly after the session on which the news became
    actionable, and with no price of A used in the selection.

    Returns None when A is not a session, or when A is the last session in the
    history: an event with no session after it is not a measurement, and
    silently shortening the window would change the specification for exactly
    those events.
    """
    if a_day is None:
        return None
    i = sessions.searchsorted(a_day, side="left")
    if i >= len(sessions) or sessions[i] != a_day:
        return None
    return sessions[i] if i + 1 < len(sessions) else None


def pit_eligible(panel_by_month: dict, month_ends, ticker: str, a_day) -> bool:
    """Was `ticker` in the universe at the last month-end ON OR BEFORE A?

    This is the look-ahead guard for membership. A company that becomes
    eligible AFTER its deficiency notice must not be selected on the strength
    of eligibility it did not yet have.
    """
    if a_day is None:
        return False
    i = month_ends.searchsorted(pd.Timestamp(a_day), side="right") - 1
    if i < 0:
        return False
    return ticker in panel_by_month.get(month_ends[i], frozenset())


def qualifies(items_field: str) -> bool:
    """An Item 3.01 filing qualifies. There is no second condition.

    Written as a function with its own test so that the ABSENCE of a price
    threshold is an asserted property of this specification rather than an
    accident of how the loop happens to be written today.
    """
    return ITEM in str(items_field or "")


# ═══════════════════════════════════════════════════════════ assembling rows

def build_rows(filings, sessions, panel_by_month, month_ends):
    """Filings -> event rows. Returns (rows, counters).

    One event per ticker per announcement session: two 3.01 filings that
    become actionable on the same session are ONE announcement, not two
    observations of one.
    """
    rows, seen = [], set()
    c = {"filings": 0, "no_session": 0, "no_next_session": 0,
         "not_pit_eligible": 0, "duplicate_session": 0, "kept": 0}
    for f in filings:
        c["filings"] += 1
        tk = str(f["ticker"]).upper()
        a_day = announcement_session(f.get("acceptance"), sessions)
        if a_day is None:
            c["no_session"] += 1
            continue
        ets = event_ts_for(a_day, sessions)
        if ets is None:
            c["no_next_session"] += 1
            continue
        if not pit_eligible(panel_by_month, month_ends, tk, a_day):
            c["not_pit_eligible"] += 1
            continue
        key = (tk, a_day)
        if key in seen:
            c["duplicate_session"] += 1
            continue
        seen.add(key)
        c["kept"] += 1
        rows.append({
            "event_id": f"{tk}_{a_day.date()}_{EVENT_TYPE}",
            "ticker": tk,
            "event_ts": ets.strftime("%Y-%m-%d"),
            "event_type": EVENT_TYPE,
            "filing_date": f.get("filing_date", ""),
            "acceptance_utc": f.get("acceptance", ""),
            "day_A": a_day.strftime("%Y-%m-%d"),
        })
    rows.sort(key=lambda r: (r["event_ts"], r["ticker"]))
    return rows, c


# ═══════════════════════════════════════════════════════════════════ EDGAR

def _session():
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", UA), ("Accept-Encoding", "gzip, deflate")]
    return op


def _get(op, url, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        with op.open(url, timeout=30) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return "ok", json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return f"http{e.code}", {}
    except Exception as e:                      # noqa: BLE001 - named below
        return f"err:{type(e).__name__}", {}


def resolve_cik(op, tk: str):
    """ticker -> CIK, resolved on ANY 8-K rather than on an Item 3.01 one.

    ⚠️ Spec #3 resolved the CIK with an item-specific query. That was harmless
    there, but here it would conflate two different things: a company with no
    Item 3.01 filing and a company whose CIK could not be resolved at all. The
    first is a real zero; the second is a gap in coverage. The availability
    rehearsal has to be able to tell them apart, so resolution is generic and
    the two counters are reported separately.

    www.sec.gov (where company_tickers.json lives) answers 403 from CI and
    from the operator's machine alike, which is why this goes through
    full-text search.
    """
    st, body = _get(op, FTS_URL, params={
        "q": '"8-K"', "forms": "8-K", "entityName": tk,
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


def fetch_deficiency_filings(op, tk: str, cik: int):
    """Item 3.01 8-Ks in the window, from the submissions API `items` field.

    The submissions API paginates older filings into `files`; a ten-year
    window reaches well past the `recent` block, so every extra chunk whose
    range can overlap the window is fetched. Dropping them would silently
    truncate the sample to roughly the last thousand filings per company.
    """
    st, body = _get(op, SUBS_URL.format(name=f"CIK{cik:010d}.json"))
    time.sleep(SLEEP)
    if st != "ok":
        return None
    chunks = [(body.get("filings", {}) or {}).get("recent", {}) or {}]
    for extra in (body.get("filings", {}) or {}).get("files", []) or []:
        if str(extra.get("filingTo", "")) >= WINDOW_START:
            st2, b2 = _get(op, SUBS_URL.format(name=extra["name"]))
            time.sleep(SLEEP)
            if st2 == "ok":
                chunks.append(b2)
    out = []
    for ch in chunks:
        forms = ch.get("form", []) or []
        dates = ch.get("filingDate", []) or []
        items = ch.get("items", []) or []
        acc = ch.get("acceptanceDateTime", []) or []
        for i in range(len(forms)):
            if forms[i] != "8-K":
                continue
            if not qualifies(items[i] if i < len(items) else ""):
                continue
            d = str(dates[i] if i < len(dates) else "")
            if not (WINDOW_START <= d <= WINDOW_END):
                continue
            out.append({"ticker": tk, "filing_date": d,
                        "acceptance": str(acc[i]) if i < len(acc) else ""})
    return out


def fetch_benchmark_sessions():
    """The benchmark's own trading calendar, as a tz-naive DatetimeIndex."""
    import yfinance as yf
    raw = yf.download(BENCHMARK, start="2015-06-01", end=None, auto_adjust=True,
                      progress=False)
    if raw is None or len(raw) == 0:
        return None
    idx = pd.DatetimeIndex(raw.index).tz_localize(None).normalize()
    return idx.sort_values()


# ══════════════════════════════════════════════════════════════════════ main

def load_panel(path: str):
    """The frozen PIT panel -> {month_end: frozenset(tickers)} and the index."""
    df = pd.read_csv(path, usecols=["date", "ticker"])
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["ticker"] = df["ticker"].astype(str).str.upper()
    by_month = {d: frozenset(g) for d, g in df.groupby("date")["ticker"]}
    month_ends = pd.DatetimeIndex(sorted(by_month)).sort_values()
    return by_month, month_ends, sorted(df["ticker"].unique())


def main() -> None:
    if not os.path.exists(PANEL):
        print(f"[3.01] no panel at {PANEL}")
        sys.exit(1)
    by_month, month_ends, tickers = load_panel(PANEL)
    if LIMIT:
        tickers = tickers[:LIMIT]
    print(f"[3.01] panel {PANEL}: {len(by_month)} month-ends "
          f"{month_ends[0].date()}..{month_ends[-1].date()}, "
          f"{len(tickers)} distinct tickers")
    print(f"[3.01] window {WINDOW_START}..{WINDOW_END}, item {ITEM}, "
          f"benchmark {BENCHMARK}")
    print("[3.01] SELECTION ONLY — no return computed, no K consumed.\n")

    sessions = fetch_benchmark_sessions()
    if sessions is None or len(sessions) == 0:
        print(f"[3.01] no {BENCHMARK} history — cannot map filings to sessions")
        sys.exit(2)
    print(f"[3.01] {len(sessions)} sessions "
          f"{sessions[0].date()}..{sessions[-1].date()}")

    op = _session()
    filings, n_nocik, n_nosubs, n_withfilings = [], 0, 0, 0
    for i, tk in enumerate(tickers, 1):
        cik = resolve_cik(op, tk)
        if cik is None:
            n_nocik += 1
        else:
            got = fetch_deficiency_filings(op, tk, cik)
            if got is None:
                n_nosubs += 1
            elif got:
                n_withfilings += 1
                filings.extend(got)
        if i % 100 == 0 or i == len(tickers):
            print(f"  {i}/{len(tickers)} tickers · {len(filings)} filings · "
                  f"{n_nocik} unresolved · {n_nosubs} submissions-failed")

    rows, c = build_rows(filings, sessions, by_month, month_ends)

    print("\n[3.01] funnel")
    print(f"  tickers in panel          {len(tickers)}")
    print(f"  CIK unresolved            {n_nocik}")
    print(f"  submissions fetch failed  {n_nosubs}")
    print(f"  tickers WITH a 3.01       {n_withfilings}")
    print(f"  3.01 filings in window    {c['filings']}")
    print(f"  no reactable session      {c['no_session']}")
    print(f"  no session after A        {c['no_next_session']}")
    print(f"  not PIT-eligible at A     {c['not_pit_eligible']}")
    print(f"  same-session duplicates   {c['duplicate_session']}")
    print(f"  EVENTS KEPT               {c['kept']}")

    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    cols = ["event_id", "ticker", "event_ts", "event_type",
            "filing_date", "acceptance_utc", "day_A"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"\n[3.01] wrote {OUT_CSV}: {len(rows)} rows")
    if rows:
        print(f"[3.01] window of kept events "
              f"{rows[0]['event_ts']}..{rows[-1]['event_ts']}, "
              f"{len({r['ticker'] for r in rows})} distinct tickers")
    print("[3.01] no return computed, Referee not consulted, K untouched.")


if __name__ == "__main__":
    main()
