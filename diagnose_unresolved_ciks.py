#!/usr/bin/env python3
"""How many tickers cannot be resolved to a CIK, and WHICH ONES — spec #4.

DIAGNOSTIC ONLY. No event is produced, no return is computed, no Referee is
consulted, no K is spent. Nothing is written under data/.

─────────────────────────────────────────────────────────────────────────────
THE QUESTION

The 120-ticker smoke test could not resolve 31 tickers to a CIK — 26%. A
quarter of the universe never reaches the Item 3.01 filter at all, and a
count on its own does not say whether that matters.

🔑 IT MATTERS ENORMOUSLY IF THE UNRESOLVED ARE THE DELISTED.

Full-text search matches a ticker through a filing's `display_names`. A
company that delisted or changed its name may no longer be findable under the
ticker it traded as. Those companies are not a random slice of the universe --
they are DISPROPORTIONATELY THE ONES THAT WENT UNDER, which is precisely the
population a listing-deficiency specification is about.

If the names that actually failed are the names the extractor cannot see,
the sample is missing its worst outcomes and any measured effect is BIASED
UPWARD. The read would then be a CEILING rather than an estimate, and that
has to be written into the declaration BEFORE a prior is signed, not
discovered in the result.

─────────────────────────────────────────────────────────────────────────────
THE TEST

The panel is point-in-time monthly membership, so it records when a name
stopped being in the universe. Split the sample on that:

    LEFT EARLY     last panel month < the panel's final month
    STILL PRESENT  in the panel at the final month

and compare the unresolved rate across the two. If resolution failure is
independent of delisting, the two rates match and the gap is mere coverage
noise. If LEFT EARLY fails far more often, the bias is real, and this prints
its size.

The sample is RANDOM with a fixed seed, not the alphabetical head the smoke
test happened to use -- `tickers[:120]` is 'A' names, which is not a sample.
"""

from __future__ import annotations

import os
import random
import sys

import pandas as pd

import extract_8k_deficiency as ex
from qt import tickers as qtk

PANEL = os.environ.get("QT_DEF_PANEL", "data/universe/v29_universe_pit.csv")
SAMPLE = int(os.environ.get("QT_DIAG_SAMPLE", "400"))
SEED = int(os.environ.get("QT_DIAG_SEED", "20261110"))


def main() -> None:
    if not os.path.exists(PANEL):
        print(f"[diag] no panel at {PANEL}")
        sys.exit(1)

    by_month, month_ends, tickers = ex.load_panel(PANEL)
    final = month_ends[-1]

    last_seen = {}
    for m in month_ends:
        for t in by_month[m]:
            last_seen[t] = m
    left_early = {t for t, m in last_seen.items() if m < final}

    print(f"[diag] panel {PANEL}")
    print(f"  tickers                 {len(tickers)}")
    print(f"  final month             {final.date()}")
    print(f"  left the panel early    {len(left_early)} "
          f"({100.0 * len(left_early) / len(tickers):.1f}%)")
    print(f"  still present at the end{len(tickers) - len(left_early):>5}")
    print(f"[diag] sample {SAMPLE}, seed {SEED} — RANDOM, not the "
          f"alphabetical head the smoke test used\n")

    rng = random.Random(SEED)
    sample = rng.sample(tickers, min(SAMPLE, len(tickers)))

    op = ex._session()
    rows = []
    for i, tk in enumerate(sample, 1):
        cik = ex.resolve_cik(op, tk)
        rows.append({"ticker": tk, "resolved": cik is not None,
                     "left_early": tk in left_early,
                     "last_month": str(last_seen[tk].date())})
        if i % 50 == 0 or i == len(sample):
            got = sum(r["resolved"] for r in rows)
            print(f"  {i}/{len(sample)} · {got} resolved · {i - got} not")

    # Ticker-safe construction: pandas 3 turns the string "NA" into a missing
    # value during DataFrame construction too, which is how this very script
    # printed a BLANK ticker on its first run. See qt/tickers.py.
    df = qtk.frame(rows)
    n = len(df)
    unres = df.loc[~df.resolved]

    def rate(sub) -> float:
        return 100.0 * (~sub.resolved).mean() if len(sub) else float("nan")

    r_all = rate(df)
    r_left = rate(df[df.left_early])
    r_stay = rate(df[~df.left_early])

    print("\n[diag] unresolved rate")
    print(f"  overall                 {r_all:5.1f}%  (n={n})")
    print(f"  LEFT the panel early    {r_left:5.1f}%  "
          f"(n={int(df.left_early.sum())})")
    print(f"  STILL present at end    {r_stay:5.1f}%  "
          f"(n={int((~df.left_early).sum())})")

    print("\n[diag] reading")
    if not (r_left == r_left) or not (r_stay == r_stay):      # NaN guard
        print("  one of the two groups is empty in this sample — the "
              "comparison is not available. Raise QT_DIAG_SAMPLE.")
    elif r_stay <= 0.0001:
        print(f"  EVERY unresolved ticker in this sample had already left the "
              f"universe. Resolution failure is entirely a property of names "
              f"that stopped trading, so the extractor is blind to exactly "
              f"the population this specification is about.")
    else:
        ratio = r_left / r_stay
        print(f"  a ticker that LEFT the universe is {ratio:.1f}x as likely "
              f"to be unresolvable as one still in it.")
        if ratio >= 1.5:
            print("  🔑 THE BIAS IS REAL AND IT POINTS UPWARD. The names the "
                  "extractor cannot see are disproportionately the names that "
                  "went under. Any effect measured on what remains is a "
                  "CEILING, and the declaration must say so before a prior is "
                  "signed.")
        else:
            print("  resolution failure looks close to independent of leaving "
                  "the universe. The gap is coverage noise rather than "
                  "survivorship, and costs breadth rather than direction.")

    print(f"\n[diag] {len(unres)} unresolved tickers in this sample")
    for r in unres.sort_values(["left_early", "ticker"],
                               ascending=[False, True]).itertuples():
        print(f"  {r.ticker:<8} last in panel {r.last_month}"
              f"{'  LEFT EARLY' if r.left_early else ''}")

    print("\n[diag] no event produced, no return computed, K unchanged.")


if __name__ == "__main__":
    main()
