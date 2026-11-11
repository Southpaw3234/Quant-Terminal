#!/usr/bin/env python3
"""COUNT-ONLY availability rehearsal for specification #4 (Item 3.01).

NO RETURN IS COMPUTED HERE. Not the strategy's, not the benchmark's, not a
single event's. This file answers one question, and it is the only question
about this specification that cannot be settled from a desk:

    Does this event class produce enough events, spread across enough
    trading days, to be measurable at all?

That matters more here than it did for specs #1-#3 because the measurement is
a CALENDAR-TIME book rather than a pile of event outcomes. The binding
constraint is not just how many events exist; it is how many DAYS carry a
book of at least MIN_BOOK names. A thousand events that all arrive in two
clusters is a handful of independent observations wearing a large N.

🔑 WHY THIS RUNS BEFORE THE DECLARATION, NOT AFTER
If N comes back thin, nothing has been spent. The slot is still there, and
the fallback event class is still available. Learning this AFTER declaring
would mean a specification that misses on its own sample-size floor, which is
a slot spent discovering that the data was never there.

The numbers printed here go into the declaration as `availability_rehearsal`,
exactly as spec #3's did, so what was known before the read is on the record
rather than reconstructed afterwards.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from qt import tickers as qtk

EVENTS = os.environ.get("QT_DEF_EVENTS", "data/events/events_8k_item301.csv")
BENCHMARK = "IWM"
HORIZON_BARS = 63          # one quarter, unchanged from the v27 programme
MIN_BOOK = 5               # below this the day is FLAT, not dropped
PRICE_START = "2015-06-01"


def fetch_prices(tickers):
    """Back-adjusted daily closes. Used to COUNT, never to compute a return.

    Returns (series_by_ticker, missing). The MISSING list is returned rather
    than discarded because a ticker the price layer never delivered and a
    ticker that stopped trading are indistinguishable once both are merely
    absent from the dict -- and telling those two apart is the whole job of
    this rehearsal.
    """
    import yfinance as yf
    need = sorted(set(tickers) | {BENCHMARK})
    out = {}
    for k in range(0, len(need), 100):
        chunk = need[k:k + 100]
        raw = yf.download(chunk, start=PRICE_START, end=None, auto_adjust=True,
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
        print(f"  prices {min(k + 100, len(need))}/{len(need)}")
    return out, [t for t in need if t not in out]


def main() -> None:
    if not os.path.exists(EVENTS):
        print(f"[rehearse] no event file at {EVENTS}")
        sys.exit(1)
    # Ticker-safe: a plain read_csv turns the real ticker "NA" into a missing
    # value, and this event class selects disproportionately for exactly that
    # kind of name. See qt/tickers.py.
    ev = qtk.read_csv(EVENTS)
    ev["ticker"] = qtk.normalise(ev["ticker"])
    ev["event_ts"] = pd.to_datetime(ev["event_ts"]).dt.normalize()
    print(f"[rehearse] {EVENTS}: {len(ev)} events, "
          f"{ev.ticker.nunique()} distinct tickers, "
          f"{ev.event_ts.min().date()}..{ev.event_ts.max().date()}")
    print(f"[rehearse] horizon {HORIZON_BARS} bars, min book {MIN_BOOK}, "
          f"benchmark {BENCHMARK}")
    print("[rehearse] COUNT ONLY — no return computed, Referee not consulted, "
          "K untouched.\n")

    prices, missing = fetch_prices(ev.ticker.unique().tolist())
    if BENCHMARK not in prices:
        print(f"[rehearse] no {BENCHMARK} history — cannot build a calendar")
        sys.exit(2)
    sessions = prices[BENCHMARK].index
    last = sessions[-1]

    c = {"no_price": 0, "no_entry": 0, "unmatured": 0, "delisted": 0, "ok": 0}
    spans = []            # (entry_idx, exit_idx) for each countable event
    for r in ev.itertuples():
        s = prices.get(r.ticker)
        if s is None or len(s) == 0:
            c["no_price"] += 1
            continue
        i = sessions.searchsorted(r.event_ts, side="right")   # entry = A+1
        if i >= len(sessions):
            c["no_entry"] += 1
            continue
        j = i + HORIZON_BARS - 1
        if j >= len(sessions):
            # The horizon runs past the end of the price history. Withheld
            # rather than truncated: a shorter window for the newest events
            # would quietly change the specification for exactly those.
            c["unmatured"] += 1
            continue
        # Does the name still trade at the end of its window? A name whose
        # series stops early is a DELISTING, counted and kept rather than
        # dropped -- dropping them is how spec #2 removed disproportionately
        # bad outcomes without meaning to.
        if s.index[-1] < sessions[j]:
            c["delisted"] += 1
        else:
            c["ok"] += 1
        spans.append((i, j))

    # ── how many DAYS carry a book, which is what calendar-time measures ──
    live = np.zeros(len(sessions), dtype=int)
    for i, j in spans:
        live[i:j + 1] += 1
    settled = c["ok"] + c["delisted"]
    days_any = int((live > 0).sum())
    days_min = int((live >= MIN_BOOK).sum())
    nz = live[live > 0]

    print("\n[rehearse] event funnel")
    print(f"  events in file        {len(ev)}")
    print(f"  no price history      {c['no_price']}")
    print(f"  no entry session      {c['no_entry']}")
    print(f"  unmatured at window end {c['unmatured']}")
    print(f"  delisted inside window  {c['delisted']}")
    print(f"  fully priced            {c['ok']}")
    print(f"  SETTLED (countable)     {settled}")

    # ── CAN THIS PRICE LAYER SEE A DELISTING AT ALL? ──────────────────────
    # The funnel above decides "delisted" by asking whether a name's own
    # series ends before its window does. That test is only meaningful if the
    # series actually END at different dates. A batch download that aligns
    # every ticker onto one common index, or silently pads the tail, makes
    # `delisted` structurally UNREACHABLE -- and the funnel then prints a
    # clean sample whether or not the sample is clean.
    #
    # 🔑 A ZERO THAT CANNOT BE NON-ZERO IS NOT A MEASUREMENT.
    # So the capability is measured here, separately, and the rehearsal
    # refuses to report a funnel it cannot substantiate.
    in_file = set(ev.ticker.unique())
    missing_ev = sorted(in_file - set(prices))
    ends = {t: s.index[-1] for t, s in prices.items()
            if t != BENCHMARK and t in in_file}
    stopped = sorted(t for t, e in ends.items() if e < last)

    print("\n[rehearse] price coverage — can this layer SEE a delisting?")
    print(f"  tickers in event file   {len(in_file)}")
    print(f"  with usable history     {len(ends)}")
    print(f"  no history delivered    {len(missing_ev)}")
    print(f"  series ending before {last.date()}  {len(stopped)}")
    if ends:
        uniq = len({e for e in ends.values()})
        print(f"  distinct series end dates {uniq}")

    print("\n[rehearse] the book, which is what a calendar-time read measures")
    print(f"  sessions in history     {len(sessions)} "
          f"{sessions[0].date()}..{last.date()}")
    print(f"  days with ANY position  {days_any}")
    print(f"  days with >= {MIN_BOOK} names   {days_min}")
    if len(nz):
        print(f"  book size min/median/max {int(nz.min())} / "
              f"{int(np.median(nz))} / {int(nz.max())}")
        print(f"  mean book size          {nz.mean():.1f}")

    # The refusal. Not a warning: these numbers go into `availability_rehearsal`
    # in the declaration, and a prior gets signed against them. Reporting a
    # funnel whose delisting cell could not have been non-zero would put an
    # unfalsifiable figure on the record, which is the A7 defect exactly --
    # a green run is not a delivered artifact.
    if ends and not stopped:
        print("\n[rehearse] REFUSING TO REPORT THIS FUNNEL")
        print(f"::error::not one of {len(ends)} tickers has a series ending "
              f"before {last.date()}, so the delisting test could not have "
              f"fired. The {c['delisted']} delistings above is an artefact of "
              f"the price layer, not a property of the sample.")
        print("  Fix the price layer (per-ticker history, not a batch aligned "
              "on one index) and re-run before anything here is quoted in a "
              "declaration.")
        print("\n[rehearse] no return computed. K unchanged. Nothing declared.")
        sys.exit(5)

    print("\n[rehearse] reading")
    if settled == 0:
        print("  NOTHING SETTLES. This event class is not measurable on this "
              "universe and window. No slot has been spent finding that out.")
    else:
        print(f"  {settled} settled events across {days_min} days carrying at "
              f"least {MIN_BOOK} names.")
        print(f"  Delistings are {c['delisted']} of {settled} settled "
              f"({100.0 * c['delisted'] / settled:.1f}%) — this event class "
              f"selects companies in breach of a listing rule, so a HIGH share "
              f"is expected and is the tail the declaration must price, not a "
              f"defect in the sample.")
    print("\n[rehearse] no return computed. K unchanged. Nothing declared.")


if __name__ == "__main__":
    main()
