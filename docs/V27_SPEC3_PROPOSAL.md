# Specification #3 — DECLARED 2026-09-05

**Status:** 🟡 **DECLARED, UNREAD.** `pead_8k_car_v3` in `data/registry/specifications.json`.
**K is 2/5.** Declaring spends nothing — `qt.referee.k_used()` counts reads. Three slots remain.

**Two things are still owed before a read may be dispatched:**

1. the operator's **signed per-spec prior** (`result.prior` currently reads `NOT YET RECORDED`), and
2. the **extractor**, which does not exist yet.

The registry is the machine's ledger. This document is the human contract.

---

## ① What failed, precisely, twice

Specifications #1 and #2 tested the same event class — insider cluster buying — at two horizons
and two sample sizes. Both were NOT MET, and the interesting part is not that they failed but
*how*:

| | spec #1 (21 bars, n=160) | spec #2 (63 bars, n=406) |
|---|---|---|
| mean | +1.76% | +1.19% |
| **median** | **−0.17%** | **−4.49%** |
| % positive | 49% | 40% |
| mean without top decile | −2.80% | **−8.05%** |

The typical event lost money. The positive mean was a right tail: forty events supplying seven
times the entire positive mass. Tripling the sample **cut** the mean by 70%, which falsified the
linear-accrual assumption underneath every power projection made for it.

**The lesson that constrains this proposal:** a hypothesis that amounts to a bet on *which small
company recovers* produces that shape. Officers-only, a larger dollar threshold, first-buy-in-N-years
— these are variations on a distribution that has now failed twice, not new hypotheses. A third
specification must have a mechanism whose **median** is positive.

## ② The proposal

**Post-earnings-announcement drift.** Small-cap prices underreact to earnings news, so a large
positive announcement-window abnormal return is followed by continued positive drift.

| parameter | value |
|---|---|
| universe | 907 names, unchanged and frozen |
| event | SEC EDGAR 8-K **Item 2.02**, 2023-09-01 .. 2026-09-05 |
| announcement day **A** | filing day if accepted before its 16:00 ET close, else the next trading day |
| surprise | **CAR[A, A+1]** — compounded stock return minus compounded SPY, over two sessions |
| selection | **CAR ≥ +5.0%**, absolute, long only |
| entry | first bar **strictly after the close of A+1** — the open of A+2 |
| horizon | 63 bars |
| control | SPY, market-adjusted |

Three choices deserve their reasons on the record.

**Why an abnormal return and not an earnings surprise against consensus.** There is no free
analyst consensus covering a 907-name small-cap universe. Finnhub caps at four quarters and
carries no announcement date (probe run `33938076855`). Chan–Jegadeesh–Lakonishok (1996) used the
price reaction itself as the surprise proxy for exactly this reason, and it is the measure whose
inputs we can actually obtain.

**Why an absolute threshold and not a decile.** A cross-sectional decile ranks each event against
peers that include *later* events. A trailing-window percentile avoids that but burns a year of the
sample warming up. An absolute bar is point-in-time by construction. Its risk is miscalibration,
and the insurance is arithmetic: roughly 7,292 matured announcements mean that even a pessimistic
5% qualification rate yields ~365 events before deduplication and ~180 after — over four times the
N ≥ 40 floor.

**Why entry is A+2.** The selection uses sessions A and A+1. Entering at A+1's close would trade
on the second day of the selection window itself. Setting `event_ts` to the close of A+1 makes the
engine's existing look-ahead guard produce the A+2 open with no new code path.

## ③ ⚠️ Four reasons to be uncomfortable with it

**1. Short-term reversal is a headwind built into the selection.** Buying after a ≥ +5% two-day
move means buying into a move that may partly mean-revert. This biases the result **downward**. It
is not a flaw to be engineered away — it is the honest cost of using price as the surprise proxy.

**2. PEAD is the most-published anomaly in the literature.** Fifty years of publication is fifty
years of arbitrage. Whatever survives in 2026 is what was too small, too illiquid, or too costly
to trade away — which is either why it might still work in this universe, or why it will not.

**3. Survivorship is unchanged and still unfixable free.** Today's listed universe only; three
years of small-cap delistings absent; biasing upward. `probe_delisted.py` found 0 of 3 bankruptcies
serve price history. **Any pass is a ceiling with an unknown floor.**

**4. The universe screen is not point-in-time for event selection.** Names were screened on average
dollar volume as of 2026-09, so a 2023 announcement is selected using a liquidity fact from 2026.
Unchanged from specs #1 and #2, and it biases upward.

## ④ Honest prior

**Not yet recorded, and it is the operator's to give.** `result.prior` reads `NOT YET RECORDED`,
and `validate_qt_phase1.py::spec3-prior-still-owed` is a tripwire asserting exactly that. It must
be flipped to assert a signed, dated prior before a read is dispatched — the sequence spec #2
followed, where the prior was recorded sixteen seconds before dispatch.

For calibration when that number is chosen: the project prior is 15%. Spec #2's was 10%. Two of
two declared reads have failed, as have four of four pre-registered v25 criteria. Reason 2 above
argues against anything higher than the project prior.

## ⑤ What must happen before a read — free, and does not touch K

1. **Build the extractor.** `data/events/events_8k_car_1095d.csv` does not exist. Building it
   computes the *selection* (CAR over [A, A+1]) but never the *outcome* (the 63-bar forward return
   after entry) — exactly as `extract_form4.py` computed insider clustering before spec #1 was
   read. **The threshold is already fixed, so the extractor cannot be tuned toward a target N.**
2. **Count settled events** at 63 bars with the engine's `COUNT_ONLY` mode. No returns, no budget.
3. **Record the prior.**

Only then is a read dispatchable, and dispatching it spends the third of five slots.

## ⑥ What a read cannot be talked out of

Frozen at declaration: threshold, window, horizon, direction, universe, control, and the entry
rule. Changing any of them afterwards is a **new** specification consuming another slot —
`qt.referee.declare` refuses to silently redefine a declared spec, and
`validate_qt_phase1.py::spec3-frozen-once-declared` proves the refusal.

E1 is unchanged: N ≥ 40, mean ≥ +0.50%, t ≥ 2.0, stability (final third ≥ 50% of first third),
**and** White's Reality Check / Hansen's SPA at p < α/K_declared = 0.02 with unread slots charged.

**Anti-deferral applies from the moment the read lands.** A bug found afterwards does not reopen it.

## ⑦ Declaration record — 2026-09-05

Declared after both availability probes returned, and before any return on its inputs was computed.

| check | result |
|---|---|
| announcement dates exist | ✅ 115/150 names, median 12 per name over ~3y, ~7,292 matured |
| entry bar unambiguous | ✅ 93% of announcements land outside the session |
| analyst consensus available | ❌ — which is why the surprise measure is an abnormal return |
| quarterly EPS depth | ✅ median 11 quarters — supports the SUE alternative **not** chosen here |
| extractor built | ❌ not yet |
| prior recorded | ❌ not yet |

Probes: `33938076855` (Finnhub, did not clear), `33939274269` (EDGAR, cleared).
