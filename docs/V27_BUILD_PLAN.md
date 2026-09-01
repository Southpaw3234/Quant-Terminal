# v27 — Build Plan

**Written:** 2026-09-01
**Status:** 🟡 **PLAN.** No code, no crons, no workflows. Supersedes nothing.
**Decision on record:** full-auto execution, venue **Alpaca** (paper → live). Robinhood
remains a manual account and is deliberately **not** automated — it publishes no official
equities API, and automating it would require private endpoints in violation of its ToS.

> ⚠️ **This plan overrules `NEXT_ARCHITECTURE.md` §⑤ ("no autonomous trading").**
> That was a deliberate choice made 2026-09-01, not an oversight. The reasoning in §⑤
> still stands on its own terms and is not withdrawn — an executing bot re-opens the
> bug class that produced the HON short, and the guard stack in §C exists to answer it.

---

## ⓪ Hard sequencing constraint

**Nothing in this plan may touch the live v25 pipeline before 2026-09-29.**

S4 is mid-flight at N=12 and reads its 30th observation on ~2026-09-28. Changing the
model, universe, feature set or `predictions.csv` before then corrupts a measurement
with four dated criteria riding on it. Build on a separate branch; the crons retire
themselves 09-29 and the path clears without anyone having to remember.

---

## ① The gating item is the edge, and there isn't one yet

v25 measured **walk-forward AUC ≈ 0.50** and **WRC p ≈ 0.52**. A full-auto bot wrapped
around that does not lose randomly — it pays spread and fees on every rebalance, which
is a *reliable* negative return.

**Scope the null correctly.** What was proven is narrow: *279 US large-caps,
price-derived features, 5-day horizon, daily rebalance, no edge available to this
operator.* It says nothing about longer horizons, smaller names, or event-conditioned
setups. It also does not say "add more features" — that was tested and failed.

**The lead is `NEXT_ARCHITECTURE.md` §①:** twelve event-level feeds are already wired
and working, and every one is flattened into a float and averaged across 279 names.
A Form 4 reading *"CFO bought $2M, first open-market purchase in six years"* becomes
`0.31`. The information is destroyed at the **consumer**, not missing at the source.

🔑 **v27 changes the cell and the consumer. It does not add compute.**

---

## ② What carries over from v25 unchanged

None of it is model code. This is the real output of the retired project.

* Fail-closed guard stack — gross cap, sector cap, oversell guard, kill switch (`dc04017`, `a0dd471`)
* Append-only evidence ledgers — `_freeze_first_write()` (`203d95c`), ledger guard (PR #25)
* Settled-row withholding — `analyze_rank_ic.py` will not score an unsettled bar
* WRC / SPA multiple-testing gates (`66312ae`)
* Ops self-heal — marker-gated retrain, four dup-retrain fixes, live-tip checkout (`86947a6`), watchdogs, Discord
* `preflight` (13 checks) and the five `validate` suites
* Sunset gates matched across workflows, and the "no alarm behind a permanently-red gate" rule
* The twelve data feeds — SEC EDGAR / Form 4, Quiver short-interest & options, Finnhub, FRED, Ken French, PatentsView
* `HANDOFF.md` ledger discipline — dated, numbered, retractions recorded rather than edited away

## ③ What is dead and must not be ported

| component | measured |
|---|---|
| feature set / 5-day classifier | walk-forward AUC ≈ **0.50** |
| Frame 2 intraday | N=36, t **−1.54** — retired under S2 |
| Frame 3 stat-arb | N=44, ann. SR **−3.94** |
| legacy `confidence` scoring | 0.50-clamp artifact, 97.5% ties |
| 279 mega-caps @ 5-day horizon | measured and closed |

---

## ④ Components to build

### A. Edge layer — gates everything else

| # | component | blends with |
|---|---|---|
| A1 | **Pre-registration** — prior, thresholds, N, terminal date, written before any code | `docs/V27_PREREGISTRATION.md`; the S1–S4 discipline that just worked |
| A2 | **Event-study engine** — forward-return distribution vs. matched control | replaces the daily cross-sectional panel |
| A3 | **Event extractors** — Form 4 cluster buys, index deletion, short-interest / borrow squeeze | the twelve live feeds, consumed as events not scalars |
| A4 | **Inverted universe screen** — small, illiquid, uncovered, index-deleted | flip the sign on `_ADV_MIN_USD` in Cell 2 |

### B. Execution layer — new for live money

| # | component | why it is new |
|---|---|---|
| B1 | live/paper switch + credential separation | `paper-api.alpaca.markets` is hardcoded across 8 workflows |
| B2 | order lifecycle manager | partial fills, rejects, PDT rule, T+1 settlement, wash sales — all free to ignore in paper |
| B3 | position reconciliation | **broker becomes source of truth, not the ledger** |
| B4 | persistent kill switch | current one is run-scoped and non-latching by design — right for paper, wrong for real money |
| B5 | staged capital ramp controller | enforces §⑤ mechanically rather than trusting discipline |

### C. Risk layer — extends what exists

| # | component | current state |
|---|---|---|
| C1 | identified beta hedge | `beta_roll` unidentified — raw −1.34, residual +0.94, both fail \|β\|<0.2 |
| C2 | ADV-based sizer | exists as a *filter*; must become a *sizer* |
| C3 | data-staleness circuit breaker | the stale-bar bug cost 50 pred-days in paper; in live it trades week-old prices |
| C4 | drawdown limits, per-name and portfolio | gross cap exists; the drawdown brake is entries-only |

### D. Ops — hardening only

Live alerting that actually wakes a person, independent P&L reconciliation not sourced
from the broker, and a tax / audit trail.

---

## ⑤ Order of work

1. **Sign the pre-registration** (`docs/V27_PREREGISTRATION.md`) while no results exist to negotiate against. This is the step that makes this v27 rather than the rebound `NEXT_ARCHITECTURE.md` §⑤ predicted.
2. **Build A** and test it against the harness in §②.
3. **If E1–E4 pass** → build B and C, then ramp per the pre-registration.
4. **If they do not** → v27 closes, at the price of one module rather than one architecture.

⚠️ **B and C are roughly three weeks of well-understood engineering and are wasted
effort until step 2 returns a number.** Building them first is the most likely way this
plan fails, because it feels like progress and defers the only question that matters.

---

## ⑥ Known open items inherited

* **Stale watchlist** — nine tickers skip on fetch (`ANSS AVB EA EQR HOLX IPG K MMC SQ`), seven chronically absent. Free to fix from 09-29 once no measurement window is open.
* **`AVB`/`EQR` batch-fetch misses** — PR #26 made the failure legible; it did not fix the fetch.
