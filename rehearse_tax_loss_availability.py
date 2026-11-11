#!/usr/bin/env python3
"""COUNT-ONLY availability rehearsal for the tax-loss book. Spec #4.

NO RETURN IS COMPUTED. Not the book's, not the benchmark's, not one name's.
This answers the two questions that cannot be settled from a desk, and that
spec #4's previous occupant failed on:

    1. Is there a measurable book here -- enough names, in enough cycles,
       across enough live sessions?
    2. Can the price layer SEE a name that stops trading?

🔑 QUESTION 2 IS WHY THIS FILE EXISTS IN THIS FORM.

The listing-deficiency specification was abandoned because its rehearsal
reported 0 delistings out of 2,507 events and 838 of 838 tickers with full
price history. Complete coverage of a distressed sample is not a clean
sample -- it is a sample with the dead removed. That was only visible
because the rehearsal was made to report its own coverage, and the same
reporting is built in here from the start rather than added after a
suspicious zero.

This book selects the WORST-PERFORMING quintile of a microcap universe.
Some of those names die. If none appear to, the price layer is lying.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from qt import tickers as qtk

# The repo's prose uses section marks and box-drawing freely, and this file
# prints it. A Windows console defaults to cp1252 and raises on the first
# such character -- which would make a suite that PASSED look like a crash.
# The stream is fixed rather than the prose.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BOOK = os.environ.get("QT_TL_BOOK", "data/events/tax_loss_book.csv")
BENCHMARK = "IWM"
MIN_BOOK = 5
PRICE_START = "2014-06-01"


def fetch_closes(tickers):
    """-> (series_by_ticker, missing). MISSING is returned, never discarded:
    a ticker the price layer never delivered and a ticker that stopped
    trading are the same absence once both are merely not in the dict."""
    import yfinance as yf
    need = sorted(set(tickers) | {BENCHMARK})
    out = {}
    for k in range(0, len(need), 100):
        chunk = need[k:k + 100]
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
        print(f"  prices {min(k + 100, len(need))}/{len(need)}")
    return out, [t for t in need if t not in out]


def main() -> None:
    if not os.path.exists(BOOK):
        print(f"[rehearse] no schedule at {BOOK}")
        sys.exit(1)

    bk = qtk.read_csv(BOOK)
    bk["ticker"] = qtk.normalise(bk["ticker"])
    for c in ("entry_ts", "exit_ts"):
        bk[c] = pd.to_datetime(bk[c]).dt.normalize()
    cycles = sorted(bk["cycle"].unique().tolist())
    print(f"[rehearse] {BOOK}: {len(bk)} positions, "
          f"{bk.ticker.nunique()} distinct tickers, {len(cycles)} cycles")
    print(f"[rehearse] min book {MIN_BOOK}, benchmark {BENCHMARK}")
    print("[rehearse] COUNT ONLY — no return computed, Referee not "
          "consulted, K untouched.\n")

    closes, missing = fetch_closes(bk.ticker.unique().tolist())
    if BENCHMARK not in closes:
        print(f"[rehearse] no {BENCHMARK} history — cannot build a calendar")
        sys.exit(2)
    sessions = closes[BENCHMARK].index
    last = sessions[-1]

    # ── per cycle ───────────────────────────────────────────────────────────
    print("\n[rehearse] per cycle")
    print(f"  {'cycle':<7}{'entry':<12}{'exit':<12}{'names':>6}"
          f"{'priced':>8}{'dead':>6}{'sessions':>10}  status")
    live = np.zeros(len(sessions), dtype=int)
    tot_dead = tot_priced = 0
    rows = []
    for cy in cycles:
        g = bk[bk.cycle == cy]
        e, x = g.entry_ts.iloc[0], g.exit_ts.iloc[0]
        i = sessions.searchsorted(e, side="left")
        j = sessions.searchsorted(x, side="right") - 1
        n_sess = max(0, j - i + 1)
        priced = dead = 0
        sell_on = sessions[j] if 0 <= j < len(sessions) else None
        for t in g.ticker:
            s = closes.get(t)
            if s is None or not len(s):
                continue
            priced += 1
            # Stopped trading before the book was due to be sold.
            if sell_on is not None and s.index[-1] < sell_on:
                dead += 1
        if n_sess > 0:
            live[i:j + 1] += priced
        tot_dead += dead
        tot_priced += priced
        status = "ok" if priced >= MIN_BOOK and n_sess > 0 else "THIN"
        rows.append((cy, priced, dead, n_sess, status))
        print(f"  {cy:<7}{str(e.date()):<12}{str(x.date()):<12}{len(g):>6}"
              f"{priced:>8}{dead:>6}{n_sess:>10}  {status}")

    # ── can this price layer see a death? ───────────────────────────────────
    in_book = set(bk.ticker.unique())
    missing_bk = sorted(in_book - set(closes))
    ends = {t: s.index[-1] for t, s in closes.items()
            if t != BENCHMARK and t in in_book}
    stopped = sorted(t for t, e in ends.items() if e < last)

    print("\n[rehearse] price coverage — can this layer SEE a delisting?")
    print(f"  tickers in schedule     {len(in_book)}")
    print(f"  with usable history     {len(ends)}")
    print(f"  no history delivered    {len(missing_bk)}")
    print(f"  series ending before {last.date()}  {len(stopped)}")
    if ends:
        print(f"  distinct series end dates {len({e for e in ends.values()})}")

    ok_cycles = [r for r in rows if r[4] == "ok"]
    days_min = int((live >= MIN_BOOK).sum())
    nz = live[live > 0]
    print("\n[rehearse] the book")
    print(f"  cycles with >= {MIN_BOOK} names  {len(ok_cycles)} of {len(cycles)}")
    print(f"  sessions with a book       {int((live > 0).sum())}")
    print(f"  sessions with >= {MIN_BOOK} names  {days_min}")
    if len(nz):
        print(f"  book size min/median/max   {int(nz.min())} / "
              f"{int(np.median(nz))} / {int(nz.max())}")

    # ── the refusal ─────────────────────────────────────────────────────────
    # These numbers go into `availability_rehearsal` in the declaration and a
    # prior gets signed against them. An unfalsifiable figure must not reach
    # the record. A7: a green run is not a delivered artifact.
    if ends and not stopped:
        print("\n[rehearse] REFUSING TO REPORT THIS BOOK")
        print(f"::error::not one of {len(ends)} tickers has a series ending "
              f"before {last.date()}, so the delisting test could not have "
              f"fired. This book is the WORST quintile of a microcap "
              f"universe; if none of them died, the price layer cannot see "
              f"the ones that did, and {tot_dead} is an artefact.")
        print("  This is exactly how specification #4's previous occupant "
              "was found to be a sample of survivors. Fix the price layer "
              "and re-run before anything here is quoted in a declaration.")
        print("\n[rehearse] no return computed. K unchanged. Nothing declared.")
        sys.exit(5)

    print("\n[rehearse] reading")
    if not ok_cycles:
        print("  NO CYCLE CARRIES A BOOK. This specification is not "
              "measurable on this universe. No slot has been spent finding "
              "that out.")
    else:
        pct = 100.0 * tot_dead / tot_priced if tot_priced else 0.0
        print(f"  {len(ok_cycles)} of {len(cycles)} cycles carry a book, "
              f"across {days_min} sessions holding at least {MIN_BOOK} names.")
        print(f"  {tot_dead} of {tot_priced} positions ({pct:.1f}%) stopped "
              f"trading inside their window. The book is the worst quintile "
              f"of a microcap universe, so a non-trivial share is expected; "
              f"it is the tail the declaration must price, not a defect.")
        if len(ok_cycles) < 10:
            print(f"  ⚠️  §⑥ assumes TEN cycles and criterion 5 is '>= 7 of "
                  f"10 positive'. Only {len(ok_cycles)} are measurable, so "
                  f"that criterion must be restated before declaring.")

    print("\n[rehearse] no return computed. K unchanged. Nothing declared.")


if __name__ == "__main__":
    main()
