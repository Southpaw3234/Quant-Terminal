# Specification #4 — SKETCH (not declared, K untouched)

**Status:** ⚪ **SKETCH.** Nothing here is in `data/registry/specifications.json`. K is 2/5.
This document exists so the design is settled *before* anyone decides whether it is worth a slot.

Two separable ideas. The first is a **measurement** change and applies to any event class. The
second is an **event class** with a better prior. They compose, and the measurement change is the
one carrying most of the leverage.

---

## ① The problem this fixes

Specifications #1–#3 all measure the same way: take N independent events, average the forward
return against SPY, test the mean. That design has three leaks, and none of them is about the
hypothesis.

**It throws away data to buy independence.** Spec #3 deleted 286 of 1,688 events because their
63-bar windows overlapped. The overlap is real and counting them naively would inflate t by
roughly √2 — but deletion is not the only remedy, and it is the most expensive one.

**It leaves the market factor in the variance.** Subtracting SPY's return removes the market from
the *mean*. It does not remove it from the *dispersion*: on any given day every small cap moves
partly with the market, and that shared movement is still in the spread of outcomes.

**It measures something you would not trade.** Nobody holds one event at a time. The tradeable
object is a book that turns over as events arrive and age out.

At spec #3's numbers, dispersion is the binding constraint. t = mean·√N / sd. With N = 1,402 and
sd ≈ 0.38, t ≥ 2.0 needs a mean near **+2%** per quarter. A genuine but modest **+1%** edge fails,
and fails in a way that looks exactly like no edge at all.

## ② The estimator

For each trading day *t*:

```
LONG_t   = every name whose qualifying event fell in (t-63, t]
SHORT_t  = every name whose opposite-signed event fell in the same window
r_t      = mean(LONG_t daily returns) - mean(SHORT_t daily returns)
```

Equal-weighted within each leg. The output is a **daily return series**, not a pile of event
outcomes. Test the mean of `r_t` with a Newey–West standard error (lag 10) — the books overlap
day to day, so the series is autocorrelated and an ordinary standard error would overstate t.

This is Fama's calendar-time portfolio, and it is the standard answer in this literature to
exactly the overlap problem spec #3 solved by deletion.

**What it buys:**

- **Every event is used.** The 286 deleted from spec #3 come back. Overlap is handled by the
  estimator instead of by the sample.
- **The market factor leaves the variance**, not just the mean. The short leg is a hedge, not a
  subtraction after the fact.
- **The statistic is the tradeable series.** Sharpe, drawdown, and the existing WRC/SPA machinery
  all consume a daily return series directly. `qt.wrc` already works on exactly this shape.

**Pre-registered mechanics** (all must be fixed before any read):

| | |
|---|---|
| minimum book | 5 names per leg; below that the day is flat, not dropped |
| weighting | equal, within each leg |
| holding window | 63 trading days from entry |
| entry | first bar strictly after the event, unchanged |
| standard error | Newey–West, lag 10 |

## ③ ⚠️ The objection that has to be answered first

**The short leg may not be borrowable.** In a universe screened to 250k–5M average dollar volume,
a large share of names are hard to borrow, expensive to borrow, or unavailable. A long-short
result that cannot be implemented is a measurement, not an opportunity.

So the two are separated on purpose, and which is which is pre-registered:

- **PRIMARY, and the criterion:** the long-short series. It tests whether the *mechanism* is real,
  with the variance reduction that makes a modest effect visible.
- **SECONDARY, reported but not a pass/fail bar:** the long leg alone, net of costs. This is the
  tradeable claim.

A mechanism that lives entirely in the short leg is a genuine finding and **not** a trade. Saying
so now costs nothing; discovering it after a pass would be the kind of thing that quietly becomes
a rationalisation.

**Costs must be in the criterion this time.** Specs #1–#3 were silent on them, and in this ADV
band the spread is what kills small-cap anomalies. Pre-register a round-trip cost assumption and
require the secondary long-only claim to survive it. A gross edge that a 25 bp round trip erases
is not a result.

## ④ E1, translated to a return series

The existing criteria are defined on event outcomes and do not carry over unchanged.

| current (event-based) | spec #4 equivalent |
|---|---|
| N ≥ 40 independent events | ≥ 250 trading days with a live book |
| mean ≥ +0.50% per event | mean daily return > 0 |
| t ≥ 2.0 | t ≥ 2.0 on the daily mean, **Newey–West lag 10** |
| stability: final third ≥ 50% of first third | unchanged, applied to the cumulative series |
| WRC/SPA p < α/K_declared | unchanged |
| — | **new:** secondary long-only claim survives the pre-registered cost assumption |

## ⑤ The event class is a slot, and it should come from the constraint family

The measurement change above works with any event. It would work with spec #3's own events, and
that is worth saying plainly: **re-measuring spec #3's data this way would be a new specification
and would consume a slot.** The read that has been set up stands on its own terms.

For a *new* event, prefer a mechanism that persists because someone is **constrained**, not
because someone is **uninformed**. Information gets arbitraged once it is published; a constraint
persists as long as the rule creating it persists.

Candidates, ranked by prior × data availability. Availability is being probed separately
(`probe_constraint_events.py`); the ranking below is prior only.

**1. Listing-deficiency notice — 8-K Item 3.01.** A company falls below a continued-listing rule
(price, market cap, float). Funds with listing mandates must sell regardless of view, and the
selling is concentrated and forced. Free on EDGAR, structured, and the same machinery spec #3
already uses reads it. Highest prior of the three because the constraint is a written rule and the
holder has no discretion.

**2. Tax-loss selling reversal.** Names down heavily through November face concentrated selling
from taxable holders who must realise losses by 31 December, then that pressure lifts. **Needs no
new data at all** — it is prices and a calendar. The constraint is the tax code, which does not
get arbitraged away because the sellers are not trying to beat the market. Heavily studied, which
is the mark against it.

**3. Follow-on offering — 424B5.** Forced supply at a negotiated discount, plus the stabilisation
that follows. Free on EDGAR and abundant. Lower prior because it is partly an information event:
issuers choose when to sell stock, and that choice is a signal.

## ⑥ What would have to be true before this becomes a declaration

1. The event class is chosen and its availability has cleared.
2. The cost assumption is a number, not a principle.
3. Borrow availability on the short leg has been checked, not assumed.
4. The prior is signed.

None of that costs a slot. All of it must precede one.
