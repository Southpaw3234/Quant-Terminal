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
ROSTER = os.environ.get("QT_TL_ROSTER", "data/universe/delisted_roster_2015.csv")
CLASSIFIED = os.environ.get("QT_TL_CLASSIFIED",
                            "data/universe/departures_classified.csv")
# Below this many priced positions, zero deaths proves nothing either way.
MIN_POSITIONS_FOR_A_DEATH = 500


def survivorship_refusal(n_priced: int, n_dead: int, n_missing: int,
                         n_stopped: int) -> "str | None":
    """Why this book must not be reported, or None if it may be.

    Two ways a price layer can be blind to delistings, and the first version
    of this rehearsal only checked for one of them.

    1. NO SERIES ENDS EARLY. Every ticker trades through the last session,
       so the death test could not have fired for anyone.

    2. 🔑 NOTHING DIED AND NOTHING IS MISSING. The live run of 2026-11-15
       is what this branch exists for: 1,359 of 1,359 tickers priced, 0
       missing, and 0 of 2,453 positions stopped trading inside a holding
       window -- in the WORST-performing quintile of a microcap universe,
       across ten Decembers. Three series did end before the last session,
       so check 1 passed and the run printed `status ok` on every cycle.

       The question was never whether the layer can see some series end
       somewhere. It is whether a position that died while it was HELD
       shows up, either as a death or as a ticker with no history. When a
       large book shows neither, every name in it has a price history
       running to the present.

       🔑 What that does and does not mean, corrected after the roster was
       joined to this book on 2026-11-15. It does NOT mean the universe
       contains no delistings: 335 of the book's 1,359 tickers are in the
       delisted roster, and 38 positions left their exchange within a year
       of exit. Those continued over the counter, so their series never
       ends, and that is the price path a holder really had.

       What is absent is every departure the price source no longer
       carries at all -- 518 names in the ten-year roster. They cannot be
       screened, so they cannot be held, so they cannot die in this book.
       `roster_exposure` reports both halves on every run, because the
       first refusal message said "no delistings at all" and that was more
       than the evidence supported.
    """
    if n_priced and n_stopped == 0:
        return (f"not one of the priced tickers has a series ending before "
                f"the last session, so the delisting test could not have "
                f"fired for any of them")
    if n_priced >= MIN_POSITIONS_FOR_A_DEATH and n_dead == 0 \
            and n_missing == 0:
        return (f"0 of {n_priced} positions stopped trading inside a "
                f"holding window AND 0 tickers are missing a price history. "
                f"In the worst quintile of a microcap universe that is not a "
                f"measurement of the strategy: every name in it has a price "
                f"history running to the present. Departures that continued "
                f"over the counter are in it; the ones the price source no "
                f"longer carries are not, and those are the ones that could "
                f"have stopped — so what is left is a sample of survivors")
    return None


# 🔑 THE PRE-REGISTERED SURVIVORSHIP BOUND (spec §⑧ criterion 9).
# Declared here, before any read, so the git history fixes it in advance.
# Changing either number after the read is a new specification.
PHANTOM_FRACTION = 0.02     # of each cycle's book, rounded UP
PHANTOM_FLOOR = 3           # and never fewer than this per cycle
PHANTOM_RETURN = -1.0       # each phantom is a total loss


def phantom_count(n_names: int) -> int:
    """How many total-loss positions a cycle is charged for names the
    universe cannot contain. Zero for an empty cycle: there is no book to
    restate."""
    import math
    if n_names <= 0:
        return 0
    return max(PHANTOM_FLOOR, math.ceil(PHANTOM_FRACTION * n_names))


def phantom_restated(book_return: float, n_names: int) -> float:
    """The equal-weight book's holding-period return with the phantoms in.

    The real names keep their return; `phantom_count` extra names at
    PHANTOM_RETURN are averaged in. Pure arithmetic on a number the read
    produces -- nothing here measures anything.
    """
    k = phantom_count(n_names)
    if n_names <= 0:
        return book_return
    return (n_names * book_return + k * PHANTOM_RETURN) / (n_names + k)


def roster_exposure(positions: list, roster: list,
                    classified: list = None) -> dict:
    """Which exchange departures can this book see, and which can it not?

    `positions` = [(ticker, entry, exit)] as ISO dates. `roster` = rows of
    delisted_roster_2015.csv. `classified` = rows of
    departures_classified.csv (the unpriceable ones, with a category).

    Counts only. No price is read and nothing is measured.
    """
    by_ticker = {}
    for r in roster or []:
        tk = str(r.get("ticker") or "").strip().upper()
        d = str(r.get("form25_date") or "")[:10]
        if tk and len(d) == 10:
            by_ticker.setdefault(tk, []).append((d, r.get("status") or ""))

    held = {str(t).strip().upper() for t, _, _ in positions}
    in_roster = sorted(t for t in held if t in by_ticker)
    status = {}
    for t in in_roster:
        k = by_ticker[t][0][1] or "not recorded"
        status[k] = status.get(k, 0) + 1

    def plus_year(day: str) -> str:
        return f"{int(day[:4]) + 1}{day[4:]}"

    in_window = after = 0
    for tk, entry, exit_ in positions:
        e, x = str(entry)[:10], str(exit_)[:10]
        days = [d for d, _ in by_ticker.get(str(tk).strip().upper(), [])]
        if any(e <= d <= x for d in days):
            in_window += 1
        elif any(x < d <= plus_year(x) for d in days):
            after += 1

    # The other half: departures that happened DURING a holding window and
    # have no price history, so could not have been in the book at all.
    windows = sorted({(str(e)[:10], str(x)[:10]) for _, e, x in positions})
    unseen = {}
    for r in classified or []:
        d = str(r.get("form25_date") or "")[:10]
        if any(e <= d <= x for e, x in windows):
            c = r.get("category") or "unclassified"
            unseen[c] = unseen.get(c, 0) + 1
    return {"tickers_held": len(held), "tickers_in_roster": len(in_roster),
            "roster_status": dict(sorted(status.items())),
            "positions_departing_in_window": in_window,
            "positions_departing_within_a_year_after": after,
            "unpriceable_departures_in_a_window": sum(unseen.values()),
            "unpriceable_by_category": dict(sorted(unseen.items()))}


def _csv_rows(path: str) -> list:
    """Rows as dicts via the csv module, or [] if the file is not there.
    Not pandas: it turns the ticker NA into NaN."""
    import csv
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


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

    # ── the roster: which departures can this book see? ─────────────────────
    roster_rows, cls_rows = _csv_rows(ROSTER), _csv_rows(CLASSIFIED)
    print("\n[rehearse] the delisted roster — which departures can this "
          "book see?")
    if not roster_rows:
        # Absent is said, not skipped: with no roster this block cannot
        # distinguish "nothing departed" from "nobody looked".
        print(f"  NOT CHECKED — {ROSTER} is not on hand")
    else:
        ex = roster_exposure(
            [(t, str(e.date()), str(x.date()))
             for t, e, x in zip(bk.ticker, bk.entry_ts, bk.exit_ts)],
            roster_rows, cls_rows)
        print(f"  tickers held                         {ex['tickers_held']}")
        print(f"  of those, in the roster              "
              f"{ex['tickers_in_roster']}  {ex['roster_status']}")
        print(f"  positions departing INSIDE a window  "
              f"{ex['positions_departing_in_window']}")
        print(f"  positions departing within a year    "
              f"{ex['positions_departing_within_a_year_after']}")
        if cls_rows:
            print(f"  UNPRICEABLE departures in a window   "
                  f"{ex['unpriceable_departures_in_a_window']}  "
                  f"{ex['unpriceable_by_category']}")
            print("    ^ these left during a holding window and have no "
                  "price history, so they could not be in the book.")
        else:
            print(f"  unpriceable departures               NOT CHECKED — "
                  f"{CLASSIFIED} is not on hand")

    # ── the pre-registered bound, in counts ─────────────────────────────────
    print("\n[rehearse] the survivorship bound (spec criterion 9) — phantom "
          "total-loss positions charged per cycle")
    tot_ph = 0
    for cy, priced, _dead, _n, _st in rows:
        k = phantom_count(priced)
        tot_ph += k
        print(f"  {cy}   book {priced:>4}   phantoms {k:>2}   "
              f"drag on the book if all lose everything "
              f"{phantom_restated(0.0, priced):+.2%}")
    print(f"  total phantoms {tot_ph} against {tot_priced} real positions "
          f"({PHANTOM_FRACTION:.0%} rounded up, floor {PHANTOM_FLOOR})")

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
    why = survivorship_refusal(tot_priced, tot_dead, len(missing_bk),
                               len(stopped))
    if why:
        print("\n[rehearse] REFUSING TO REPORT THIS BOOK")
        print(f"::error::{why}. The figure {tot_dead} dead is an artefact of "
              f"the universe, not a property of the book.")
        print("  This is exactly how specification #4's previous occupant "
              "was found to be a sample of survivors. The roster block "
              "above says which departures this book can see and which it "
              "cannot. The unpriceable ones cannot be screened from any "
              "free source; data/universe/departures_classified.csv says "
              "why each left. Nothing here should be quoted in a "
              "declaration until that gap is priced or pre-registered as a "
              "bound.")
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
