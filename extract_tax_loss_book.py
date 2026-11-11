#!/usr/bin/env python3
"""Build the tax-loss reversal book schedule — specification #4, SELECTION ONLY.

NO RETURN IS COMPUTED HERE. Not the book's, not the benchmark's, not a
single name's. This file decides WHO is in the book and WHEN, and stops.
The measurement is a separate run that the Referee gates and that spends one
of K=5.

See docs/V27_SPEC4_TAX_LOSS_REVERSAL.md. Every parameter below is fixed
there; changing one is a new specification and a different slot.

─────────────────────────────────────────────────────────────────────────────
🔑 THE ONE PROPERTY THIS FILE MUST HAVE

Selection must be unable to see anything after the formation date.

That is not achieved by being careful. It is achieved by `select_cycle`
taking only three arguments -- the eligible set, each name's year-to-date
return as of formation, and how many to keep -- so there is no exit price,
no forward return and no outcome anywhere in its scope to leak from.
validate_extract_tax_loss.py asserts that signature with `inspect`, so
widening it turns the suite red.

The calendar does the rest: entry and exit dates are derived from 15
December and 31 January against the session index, never from anything
about a name.

─────────────────────────────────────────────────────────────────────────────
WHAT A ROW MEANS

One row per (cycle, ticker): this name was in the universe on the formation
date, sat in the bottom quintile of calendar-year-to-date return, and would
be bought at the formation close and sold at the exit close.

The output is a BOOK SCHEDULE, not an event list. Specifications #1-#3 were
piles of independent events; this is ten annual books, and the measurement
is calendar-time. §⑥ of the specification is explicit that ten cycles is the
effective sample size, not three hundred days.
"""

from __future__ import annotations

import hashlib
import os
import sys

import pandas as pd

from qt import panel as qtp
from qt import tickers as qtk

# The repo's prose uses section marks and box-drawing freely, and this file
# prints it. A Windows console defaults to cp1252 and raises on the first
# such character -- which would make a suite that PASSED look like a crash.
# The stream is fixed rather than the prose.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time.
# Selection must not be able to spend a read or evaluate a criterion, and
# the absence of the import is what makes that true rather than intent.
# validate_extract_tax_loss.py parses this file's imports and fails on one.

PANEL = os.environ.get("QT_TL_PANEL", "data/universe/v29_universe_pit.csv")
OUT_CSV = os.environ.get("QT_TL_OUT", "data/events/tax_loss_book.csv")
LIMIT = int(os.environ.get("QT_TL_LIMIT", "0"))
CHUNK = int(os.environ.get("QT_TL_CHUNK", "100"))

# ── the declared parameters (docs/V27_SPEC4_TAX_LOSS_REVERSAL.md §⑤) ──
BENCHMARK = "IWM"
ENTRY_MONTH, ENTRY_DAY = 12, 15        # first session on or after 15 Dec
EXIT_MONTH, EXIT_DAY = 1, 31           # first session on or after 31 Jan
QUANTILE = 0.20                        # bottom quintile by YTD return
MIN_BOOK = 5                           # below this the cycle is flat
FIRST_CYCLE, LAST_CYCLE = 2015, 2024   # Dec 2015 -> Jan 2016 .. Dec 2024 -> Jan 2025
PRICE_START = "2014-06-01"


# ── the calendar ─────────────────────────────────────────────────────────────

def first_session_on_or_after(sessions, when) -> "pd.Timestamp | None":
    """The first trading session at or after a calendar date.

    15 December and 31 January are calendar anchors, not sessions. Which
    session they land on depends on weekends and holidays, so the mapping
    goes through the benchmark's own index rather than through arithmetic.
    """
    ts = pd.Timestamp(when)
    i = sessions.searchsorted(ts, side="left")
    return None if i >= len(sessions) else sessions[i]


def cycle_dates(sessions, year: int):
    """(formation, exit) sessions for the cycle formed in December `year`.

    Both come from the calendar alone. Nothing about any company, price or
    outcome can move either date.
    """
    f = first_session_on_or_after(sessions, f"{year}-{ENTRY_MONTH:02d}-{ENTRY_DAY:02d}")
    x = first_session_on_or_after(sessions, f"{year + 1}-{EXIT_MONTH:02d}-{EXIT_DAY:02d}")
    if f is None or x is None or x <= f:
        return None, None
    return f, x


def prior_year_end_session(sessions, year: int) -> "pd.Timestamp | None":
    """The last session of `year - 1`: the base for the year-to-date return.

    Year-to-date, not trailing twelve months, because the tax year IS the
    calendar year -- the loss a holder can realise is measured from one
    31 December to the next. A 12-month lookback would measure something the
    tax code does not care about.
    """
    cutoff = pd.Timestamp(f"{year}-01-01")
    i = sessions.searchsorted(cutoff, side="left") - 1
    return None if i < 0 else sessions[i]


# ── the selection ────────────────────────────────────────────────────────────

def select_cycle(eligible, ytd_by_ticker, quantile: float = QUANTILE) -> list:
    """The bottom-quantile names by year-to-date return. PURE.

    🔑 THE ARGUMENT LIST IS THE LOOK-AHEAD GUARD. There is no exit date, no
    exit price, no forward return and no outcome in scope, so none can leak
    into the choice however this function is later edited. The validator
    pins this signature.

    Ties at the quantile boundary are resolved by taking every name at or
    below the cutoff value rather than slicing to an exact count: a cut that
    depends on sort order would make the book depend on dictionary ordering,
    which is not a declared parameter.
    """
    rows = [(t, v) for t, v in ytd_by_ticker.items()
            if t in eligible and v is not None and v == v]   # v == v drops NaN
    if not rows:
        return []
    rows.sort(key=lambda r: r[1])
    k = max(1, int(round(len(rows) * quantile)))
    cutoff = rows[min(k, len(rows)) - 1][1]
    return sorted(t for t, v in rows if v <= cutoff)


def ytd_returns(closes: dict, base_ts, formation_ts, eligible) -> dict:
    """Year-to-date return per eligible name, as of the formation close.

    Both prices are at or before the formation date, so this is computable on
    the day and contains no look-ahead. A name without a price at EITHER end
    is dropped rather than defaulted -- a missing base price silently treated
    as zero would rank the name as unchanged and quietly keep it out of the
    bottom quintile.
    """
    out = {}
    for t in eligible:
        s = closes.get(t)
        if s is None or not len(s):
            continue
        a = s.loc[:base_ts]
        b = s.loc[:formation_ts]
        if not len(a) or not len(b):
            continue
        p0, p1 = float(a.iloc[-1]), float(b.iloc[-1])
        if p0 > 0:
            out[t] = p1 / p0 - 1.0
    return out


# ── prices ───────────────────────────────────────────────────────────────────

def fetch_closes(tickers) -> dict:
    """Back-adjusted daily closes. Used to RANK, never to score."""
    import yfinance as yf
    need = sorted(set(tickers) | {BENCHMARK})
    out = {}
    for k in range(0, len(need), CHUNK):
        chunk = need[k:k + CHUNK]
        raw = yf.download(chunk, start=PRICE_START, end=None, auto_adjust=True,
                          progress=False, threads=True, group_by="column")
        if raw is None or len(raw) == 0:
            continue
        close = (raw["Close"] if isinstance(raw.columns, pd.MultiIndex)
                 else raw[["Close"]])
        if isinstance(close, pd.Series):
            close = close.to_frame(chunk[0])
        for c in close.columns:
            s = pd.to_numeric(close[c], errors="coerce").dropna()
            if len(s):
                s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
                out[str(c)] = s
        print(f"  prices {min(k + CHUNK, len(need))}/{len(need)}")
    return out


# ── main ─────────────────────────────────────────────────────────────────────

def build(closes: dict, by_month, month_ends) -> tuple:
    """All ten cycles -> (rows, per-cycle report). No return is computed."""
    sessions = closes[BENCHMARK].index
    rows, report = [], []
    for year in range(FIRST_CYCLE, LAST_CYCLE + 1):
        f, x = cycle_dates(sessions, year)
        base = prior_year_end_session(sessions, year)
        if f is None or x is None or base is None:
            report.append({"cycle": year, "status": "no_calendar",
                           "eligible": 0, "selected": 0})
            continue

        elig = qtp.eligible_at(by_month, month_ends, f)
        if LIMIT:
            elig = frozenset(sorted(elig)[:LIMIT])
        ytd = ytd_returns(closes, base, f, elig)
        picked = select_cycle(elig, ytd)

        # A thin cycle is REPORTED and kept visible, never silently dropped:
        # "the book was too small in 2021" is a finding about the mechanism,
        # and deleting the cycle would hide it.
        status = "ok" if len(picked) >= MIN_BOOK else "below_min_book"
        report.append({"cycle": year, "status": status,
                       "formation": str(f.date()), "exit": str(x.date()),
                       "base": str(base.date()), "eligible": len(elig),
                       "priced": len(ytd), "selected": len(picked),
                       "worst_ytd": round(min(ytd.values()), 4) if ytd else None,
                       "cutoff_ytd": round(max(ytd[t] for t in picked), 4)
                       if picked else None})
        for t in picked:
            rows.append({"cycle": year, "entry_ts": str(f.date()),
                         "exit_ts": str(x.date()), "ticker": t,
                         "ytd_return": round(ytd[t], 6),
                         "base_ts": str(base.date())})
    return rows, report


def main() -> None:
    if not os.path.exists(PANEL):
        print(f"[tax-loss] no panel at {PANEL}")
        sys.exit(1)
    print("[tax-loss] SELECTION ONLY — no return computed, Referee not "
          "consulted, K untouched.")
    print(f"[tax-loss] bottom {QUANTILE:.0%} by YTD return, enter "
          f"{ENTRY_DAY:02d}-{ENTRY_MONTH:02d}, exit {EXIT_DAY:02d}-"
          f"{EXIT_MONTH:02d}, cycles {FIRST_CYCLE}..{LAST_CYCLE}\n")

    by_month, month_ends, tickers = qtp.load_pit_panel(PANEL)
    print(f"[tax-loss] panel {len(tickers)} tickers, "
          f"{month_ends[0].date()}..{month_ends[-1].date()}")

    closes = fetch_closes(tickers)
    if BENCHMARK not in closes:
        print(f"[tax-loss] no {BENCHMARK} history — cannot build a calendar")
        sys.exit(2)

    rows, report = build(closes, by_month, month_ends)

    print("\n[tax-loss] per cycle")
    print(f"  {'cycle':<6}{'formation':<12}{'exit':<12}{'elig':>7}"
          f"{'priced':>8}{'book':>7}  {'cutoff':>8}  status")
    for r in report:
        print(f"  {r['cycle']:<6}{r.get('formation',''):<12}"
              f"{r.get('exit',''):<12}{r['eligible']:>7}"
              f"{r.get('priced',0):>8}{r['selected']:>7}  "
              f"{str(r.get('cutoff_ytd','')):>8}  {r['status']}")

    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df = qtk.frame(rows)
    df.to_csv(OUT_CSV, index=False, lineterminator="\n")

    b = open(OUT_CSV, "rb").read().replace(b"\r\n", b"\n")
    ok = [r for r in report if r["status"] == "ok"]
    print(f"\n[tax-loss] wrote {OUT_CSV}")
    print(f"  rows      {len(rows)}")
    print(f"  cycles ok {len(ok)} of {LAST_CYCLE - FIRST_CYCLE + 1}")
    print(f"  tickers   {df['ticker'].nunique() if len(df) else 0}")
    print(f"  sha256    {hashlib.sha256(b).hexdigest()}")
    print("\n[tax-loss] no return computed. K unchanged. Nothing declared.")


if __name__ == "__main__":
    main()
