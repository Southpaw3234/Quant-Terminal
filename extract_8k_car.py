#!/usr/bin/env python3
"""Event extractor for specification #3 — 8-K Item 2.02 announcement-window CAR.

Builds `data/events/events_8k_car_1095d.csv`, the declared input of
`pead_8k_car_v3` (see data/registry/specifications.json and
docs/V27_SPEC3_PROPOSAL.md).

WHAT THIS COMPUTES, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------
It computes the SELECTION: the two-session abnormal return around each
earnings announcement, and whether that clears the declared +5.0% bar.

It does NOT compute the OUTCOME: the 63-bar forward return after entry. That
is event_study.py's job, it is what spends one of K=5, and keeping the two in
separate programs is what keeps the distinction real rather than merely
stated. The same separation held for extract_form4.py before spec #1.

🔑 **THE THRESHOLD WAS FIXED BEFORE THIS FILE EXISTED.** +5.0% is in the
registry, declared 2026-09-05, before the CAR distribution had ever been
computed. `validate_extract_8k_car.py` asserts the constant here still
matches the registry, so tuning it toward a nicer N would fail the suite
rather than pass silently.

THE ANNOUNCEMENT SESSION, WHICH IS THE WHOLE DIFFICULTY
--------------------------------------------------------
`acceptanceDateTime` is served in UTC. Reading its raw hour as ET puts a
21:01Z after-close release at "9pm" and calls a 12:00Z pre-market release
"intraday" -- an error the first probe run actually made. So:

    A = the ET calendar date of acceptance, when that date is a trading
        session and acceptance precedes its 16:00 ET close
      = otherwise, the first trading session strictly after that date

DST is not cosmetic here. 20:30Z is 15:30 ET in January (intraday, so A is
that same session) and 16:30 EDT in July (after the close, so A is the next
session). Same clock reading, different answer, which is why the conversion
is delegated to tz_convert rather than an offset.

⚠️ **A IS DERIVED FROM THE ACCEPTANCE TIMESTAMP, NOT FROM EDGAR'S
`filingDate`.** EDGAR rolls the official filing date to the next business day
for late submissions, so `filingDate` would put an after-close Monday release
on Tuesday and then the next-session rule would push entry to Wednesday --
one session of drift, silently, on exactly the 66.5% of events that arrive
after the close. The spec's phrase "the filing day D ... acceptance precedes
its 16:00 ET close" is inherently about the acceptance instant.

CAR is close-to-close, compounded across the two sessions:

    CAR = close_stock(A+1)/close_stock(A-1) - close_spy(A+1)/close_spy(A-1)

A-1 is the last close BEFORE the news under all three arrival patterns
(pre-market, intraday, after-close), which is what makes one formula correct
for all of them.

Prices are back-adjusted (`auto_adjust=True`). Returns are invariant to
back-adjustment, and an unadjusted split would otherwise manufacture a fake
50% "surprise" out of a 2-for-1.
"""
from __future__ import annotations

import collections
import os
import sys
import time

import numpy as np
import pandas as pd

# ── declared parameters. Changing any of these is a NEW specification. ──
WINDOW_START = "2023-09-01"
WINDOW_END = "2026-09-05"
CAR_THRESHOLD = 0.05          # registry: "CAR[A, A+1] >= +5.0%"
SURPRISE_SESSIONS = 2         # [A, A+1]
BENCHMARK = "SPY"
EVENT_TYPE = "8k_item202_car_ge5"

UNIVERSE = os.environ.get("QT_8K_UNIVERSE", "data/universe/v27_universe.csv")
OUT_CSV = os.environ.get("QT_8K_OUT", "data/events/events_8k_car_1095d.csv")
LIMIT = int(os.environ.get("QT_8K_LIMIT", "0"))       # 0 = whole universe
UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Quant-Terminal research"
SLEEP = 0.12
ET = "America/New_York"
CLOSE_HOUR, CLOSE_MIN = 16, 0

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBS_URL = "https://data.sec.gov/submissions/{name}"


# ═══════════════════════════════════════════════════ pure logic (no network)

def announcement_session(acceptance, sessions) -> "pd.Timestamp | None":
    """The first session that can react to news accepted at `acceptance`.

    `sessions` is a tz-naive DatetimeIndex of trading dates (the benchmark's
    own calendar -- the operationally correct one, since it is exactly the
    days on which a price exists).
    """
    if acceptance is None or (isinstance(acceptance, float) and np.isnan(acceptance)):
        return None
    t = pd.Timestamp(acceptance)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t_et = t.tz_convert(ET)
    d = pd.Timestamp(t_et.date())                       # ET calendar date, naive
    before_close = (t_et.hour, t_et.minute) < (CLOSE_HOUR, CLOSE_MIN)
    i = sessions.searchsorted(d, side="left")
    if before_close and i < len(sessions) and sessions[i] == d:
        return sessions[i]                              # pre-market or intraday
    j = sessions.searchsorted(d, side="right")           # strictly after d
    return sessions[j] if j < len(sessions) else None


def car_two_session(stock: pd.Series, bench: pd.Series, a_day) -> "float | None":
    """Market-adjusted compounded return over sessions [A, A+1].

    Needs A-1, A and A+1 present in BOTH series. Returns None otherwise --
    an event at the very start or end of the price history is not a
    measurement, and silently substituting a shorter window would quietly
    change the specification for those events.
    """
    if a_day is None:
        return None
    for s in (stock, bench):
        if a_day not in s.index:
            return None
    si = stock.index.get_loc(a_day)
    bi = bench.index.get_loc(a_day)
    if si < 1 or si + SURPRISE_SESSIONS - 1 >= len(stock):
        return None
    if bi < 1 or bi + SURPRISE_SESSIONS - 1 >= len(bench):
        return None
    s_pre, s_post = float(stock.iloc[si - 1]), float(stock.iloc[si + SURPRISE_SESSIONS - 1])
    b_pre, b_post = float(bench.iloc[bi - 1]), float(bench.iloc[bi + SURPRISE_SESSIONS - 1])
    if not all(np.isfinite(v) for v in (s_pre, s_post, b_pre, b_post)) or s_pre <= 0 or b_pre <= 0:
        return None
    return (s_post / s_pre - 1.0) - (b_post / b_pre - 1.0)


def entry_ts(a_day, sessions) -> "pd.Timestamp | None":
    """event_ts = the close of A+1.

    event_study.py enters at `searchsorted(event_ts, side="right")`, so an
    event_ts landing exactly on A+1 yields entry at A+2 -- strictly after the
    two sessions the selection used. The engine's existing look-ahead guard
    does the work; no new code path, and nothing to get wrong twice.
    """
    if a_day is None:
        return None
    i = sessions.searchsorted(a_day, side="left")
    if i >= len(sessions) or sessions[i] != a_day:
        return None
    return sessions[i + 1] if i + 1 < len(sessions) else None


def qualifies(car) -> bool:
    return car is not None and np.isfinite(car) and car >= CAR_THRESHOLD


def build_rows(announcements, prices, sessions):
    """announcements: iterable of dicts with ticker / acceptance / filing_date.

    Returns (rows, stats). One row per QUALIFYING event. Deduplicated on
    (ticker, A): two filings mapping to the same reacting session -- a Friday
    after-close release and a Saturday amendment, say -- are one announcement.
    """
    stats = collections.Counter()
    seen, cars, rows = set(), [], []
    for a in sorted(announcements, key=lambda r: (r["ticker"], str(r.get("acceptance") or ""))):
        tk = a["ticker"]
        stats["announcements"] += 1
        s = prices.get(tk)
        if s is None or len(s) == 0:
            stats["no_prices"] += 1; continue
        A = announcement_session(a.get("acceptance"), sessions)
        if A is None:
            stats["no_session"] += 1; continue
        if (tk, A) in seen:
            stats["dup_same_session"] += 1; continue
        seen.add((tk, A))
        car = car_two_session(s, prices[BENCHMARK], A)
        if car is None:
            stats["car_unavailable"] += 1; continue
        stats["priced"] += 1
        cars.append(car)
        if not qualifies(car):
            stats["below_threshold"] += 1; continue
        ts = entry_ts(A, sessions)
        if ts is None:
            stats["no_entry_bar"] += 1; continue
        rows.append({
            "event_id": f"{tk}_{A.date()}_car",
            "ticker": tk,
            "event_ts": ts.strftime("%Y-%m-%d"),
            "event_type": EVENT_TYPE,
            "car": round(car, 6),
            "day_A": A.strftime("%Y-%m-%d"),
            "acceptance_utc": str(a.get("acceptance") or ""),
            "filing_date": str(a.get("filing_date") or ""),
        })
        stats["qualified"] += 1
    return rows, stats, cars


# ═══════════════════════════════════════════════════════════ network shell

def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    return s


def _get(sess, url, params=None):
    import requests
    last = ""
    for attempt in range(3):
        try:
            r = sess.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            last = type(exc).__name__; time.sleep(1.5 * (attempt + 1)); continue
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


def resolve_cik(sess, tk: str):
    """ticker -> CIK via full-text search, confirming the ticker on the hit.

    www.sec.gov (where company_tickers.json lives) is 403 from CI and from
    the operator's machine alike -- measured, see probe_edgar_8k.py.
    """
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


def fetch_announcements(sess, tk: str, cik: int):
    """Item 2.02 8-Ks in the window, from the submissions API's `items` field."""
    st, body = _get(sess, SUBS_URL.format(name=f"CIK{cik:010d}.json"))
    time.sleep(SLEEP)
    if st != "ok":
        return []
    chunks = [(body.get("filings", {}) or {}).get("recent", {}) or {}]
    for extra in (body.get("filings", {}) or {}).get("files", []) or []:
        if str(extra.get("filingTo", "")) >= WINDOW_START:
            st2, b2 = _get(sess, SUBS_URL.format(name=extra["name"]))
            time.sleep(SLEEP)
            if st2 == "ok":
                chunks.append(b2)
    out, seen = [], set()
    for ch in chunks:
        forms = ch.get("form", []) or []
        dates = ch.get("filingDate", []) or []
        items = ch.get("items", []) or []
        acc = ch.get("acceptanceDateTime", []) or []
        for i in range(len(forms)):
            if forms[i] != "8-K":
                continue
            if "2.02" not in str(items[i] if i < len(items) else ""):
                continue
            d = str(dates[i] if i < len(dates) else "")
            if not (WINDOW_START <= d <= WINDOW_END) or d in seen:
                continue
            seen.add(d)
            out.append({"ticker": tk, "filing_date": d,
                        "acceptance": str(acc[i]) if i < len(acc) else ""})
    return out


def fetch_prices(tickers: list) -> dict:
    """Back-adjusted daily closes. Returns invariant to back-adjustment."""
    import yfinance as yf
    need = sorted(set(tickers) | {BENCHMARK})
    out = {}
    for k in range(0, len(need), 100):
        chunk = need[k:k + 100]
        raw = yf.download(chunk, start="2023-08-01", end=None, auto_adjust=True,
                          progress=False, threads=True, group_by="column")
        if raw is None or len(raw) == 0:
            continue
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
        if isinstance(close, pd.Series):
            close = close.to_frame(chunk[0])
        for c in close.columns:
            s = pd.to_numeric(close[c], errors="coerce").dropna()
            if len(s):
                s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
                out[str(c)] = s
        print(f"  prices {min(k+100, len(need))}/{len(need)}")
    return out


def main() -> None:
    if not os.path.exists(UNIVERSE):
        print(f"[8k-car] no universe at {UNIVERSE}"); sys.exit(1)
    tickers = sorted(pd.read_csv(UNIVERSE)["ticker"].astype(str).str.upper().unique())
    if LIMIT:
        tickers = tickers[:LIMIT]
    print(f"[8k-car] universe {len(tickers)}; window {WINDOW_START}..{WINDOW_END}; "
          f"threshold CAR >= {CAR_THRESHOLD:+.1%}; UA={UA!r}\n")

    sess = _session()
    t0 = time.time()
    anns, resolved = [], 0
    for k, tk in enumerate(tickers):
        cik = resolve_cik(sess, tk)
        if cik is None:
            continue
        resolved += 1
        anns.extend(fetch_announcements(sess, tk, cik))
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(tickers)} names, {resolved} resolved, "
                  f"{len(anns)} announcements, {time.time()-t0:.0f}s")
    print(f"\n[8k-car] {resolved}/{len(tickers)} resolved; {len(anns)} announcements "
          f"in {time.time()-t0:.0f}s")
    if not anns:
        print("[8k-car] no announcements — refusing to write an empty event file"); sys.exit(1)

    with_events = sorted({a["ticker"] for a in anns})
    print(f"[8k-car] downloading prices for {len(with_events)} names + {BENCHMARK}")
    prices = fetch_prices(with_events)
    if BENCHMARK not in prices:
        print(f"[8k-car] no {BENCHMARK} history — cannot market-adjust"); sys.exit(1)
    sessions = prices[BENCHMARK].index

    rows, stats, cars = build_rows(anns, prices, sessions)
    print("\n" + "=" * 68)
    for key in ("announcements", "no_prices", "no_session", "dup_same_session",
                "car_unavailable", "priced", "below_threshold", "no_entry_bar",
                "qualified"):
        print(f"  {key:<20} {stats[key]}")
    if cars:
        q = np.percentile(cars, [10, 25, 50, 75, 90, 95])
        print(f"\n  CAR[A,A+1] distribution over {len(cars)} priced announcements:")
        print(f"    p10 {q[0]:+.2%}  p25 {q[1]:+.2%}  median {q[2]:+.2%}  "
              f"p75 {q[3]:+.2%}  p90 {q[4]:+.2%}  p95 {q[5]:+.2%}")
        print(f"    mean {np.mean(cars):+.2%}   >= +5.0%: {sum(1 for c in cars if c >= CAR_THRESHOLD)} "
              f"({100*sum(1 for c in cars if c >= CAR_THRESHOLD)/len(cars):.1f}%)")
    if not rows:
        print("[8k-car] nothing cleared the threshold — refusing to write"); sys.exit(1)

    df = pd.DataFrame(rows).sort_values(["event_ts", "ticker"]).reset_index(drop=True)
    by_year = collections.Counter(r["event_ts"][:4] for r in rows)
    print(f"\n  qualifying events {len(df)} across {df['ticker'].nunique()} tickers; "
          f"by year {dict(sorted(by_year.items()))}")
    print(f"  event_ts spans {df['event_ts'].min()} .. {df['event_ts'].max()}")
    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"\n[8k-car] wrote {OUT_CSV} ({len(df)} rows)")
    print("[8k-car] SELECTION only — no forward return computed, K untouched.")


if __name__ == "__main__":
    main()
