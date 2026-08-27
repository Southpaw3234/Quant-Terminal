# What Comes After — design sketch, NOT a commitment

**Written:** 2026-08-27
**Status:** 🟡 **SKETCH ONLY.** Nothing here is scheduled, built, or decided. No code, no crons, no workflows.
**Precondition:** do not act on this before **S4 lands (~2026-09-24)** and the closing ledger is written. See `HANDOFF.md` § 🛑 STOPPING RULE and § 🏁 THE ALPHA SEARCH IS OVER.

> ⚠️ **This document is not permission to restart.** The anti-deferral rule still binds:
> only a FULL Stage-1 gate pass (+0.03 **and** t ≥ 2.0) that ALSO clears WRC/SPA
> reopens the current architecture. This sketch is about what a *different* project
> could look like — after the current one is properly closed, not instead of closing it.

---

## ① The finding that reframes everything: the inputs were never the problem

The 2026-08-09 stopping decision named three remaining options, and called **"change the inputs"** the only lever with a high ceiling — listing short interest, borrow cost, revision breadth, options skew, and explicitly *not* more OHLCV transforms.

**That barrier is already crossed. `quant_runner.py` has all of it wired today:**

| feed | endpoint | line | currently consumed as |
|---|---|---|---|
| SEC EDGAR full-text (8-K) | `efts.sec.gov/LATEST/search-index` | `1980` | a scalar feature |
| SEC submissions (Form 4) | `data.sec.gov/submissions/CIK*.json` | `6984` | a scalar feature |
| SEC filing bodies | `sec.gov/Archives/edgar/data/...` | `1998`, `7000` | a scalar feature |
| **Short interest** | `quiverquant.com/beta/live/shortinterest` | `7079` | a scalar feature |
| **Options** | `quiverquant.com/beta/live/options` | `7103` | a scalar feature |
| Congress trading | `quiverquant.com/beta/live/congresstrading` | `6804` | a scalar feature |
| Insider trading | `quiverquant.com/beta/live/insidertrading` | `6877` | a scalar feature |
| Insider transactions | `finnhub.io/api/v1/stock/insider-transactions` | `6915` | a scalar feature |
| Company news | `finnhub.io/api/v1/company-news` | `2303` | a scalar feature |
| Macro | `api.stlouisfed.org/fred/series` | — | a scalar feature |
| Factor returns | Ken French data library | — | a scalar feature |
| Patents | `api.patentsview.org/patents` | — | a scalar feature |

🔑 **Every one of these is event-level, structured, human-readable data. Every one is flattened into a float, averaged across 279 names, and fed to a 5-day classifier that reports walk-forward AUC 0.4862 — below coin flip.**

A Form 4 reading *"the CFO bought $2M on the open market, first purchase in six years"* becomes `0.31`. An 8-K becomes `0.07`. **That is where the information went.**

**Conclusion: the output layer was wrong, not the input layer.** That matters, because it means the next thing does not need new data contracts, new API keys, or new spend. It needs a different consumer of feeds that already work.

---

## ② The strategic split: trading and investing are not one problem

The retired project treated "make money in markets" as a single engineering problem. It is not, and conflating them is what pointed months of good engineering at the least winnable cell in the market.

| | trading | investing |
|---|---|---|
| horizon | days | years |
| competition | brutal — **now measured, not assumed** | far thinner for a patient individual |
| edge source | speed, data, execution | temperament, time horizon, concentration |
| is it a model problem? | yes | **mostly not** |

⚠️ **Scope the null correctly.** What was proven is narrow and specific: *279 US large-caps, price-derived features, 5-day horizon, daily rebalance, has no edge available to this operator.* That says nothing about longer horizons, smaller names, event-conditioned setups, or discretionary investing. **Do not over-generalise it into "markets are unbeatable" — and do not under-generalise it into "the model just needs more features" either.**

---

## ③ The proposal: an instrument panel, not a predictor

**Every autonomous predictor fights the efficient-market prior head-on. That is the fight just lost — correctly, and at real cost.** A system that makes the operator faster and better-informed does not fight that prior at all. Nobody has arbitraged away *reading the 10-K carefully*.

Five modules. Each repoints existing, working code. **None of them predicts a price.**

### 3.1 Filing watcher
`data.sec.gov/submissions` already pulls Form 4s (`quant_runner.py:6984`). Stop scoring them — **show** them. Surface: cluster buys, unusual size relative to existing holdings, first open-market purchase in N years, sales during a buyback. Link straight to the filing. Post to the existing Discord webhook.

### 3.2 Filing diff
8-K and 10-K bodies are already fetched (`1998`, `7000`). Diff risk factors and MD&A quarter over quarter. **Removed language is frequently more informative than added language.** Pure text work; no model, no scoring, no backtest.

### 3.3 Constraint screen
**Invert the universe.** The retired book was 279 mega-caps — maximally competed, and now measured as such. Screen instead for what large funds *structurally cannot* hold: too small, too illiquid, recently index-deleted, no analyst coverage, below mandate minimums. The mechanics already exist — `SECTOR_MAP` and the `_ADV_MIN_USD = 50_000_000` liquidity filter in the notebook's Cell 2. **Flip the sign on the filter rather than writing a new one.**

### 3.4 Base-rate lookup
Given an event type, what has historically happened next? Uses `predictions.csv` history and the Ken French factor data already pulled. **Anchors judgment to a distribution instead of to a narrative.** Report the distribution, never a point forecast.

### 3.5 Thesis ledger — the important one
**Pre-registration applied to your own judgment.** For every position: entry thesis, **falsifiers stated in advance**, review date, and an anti-deferral clause. Append-only, guarded the way `predictions.csv` is now (`Predictions ledger guard`, PR #25).

🔑 **This is where the durable edge actually is.** S1, S2 and S3 all failed and the thresholds were *not* renegotiated. That discipline — honoring a pre-registered stop when the data says stop — is rarer and worth more than any model, and **most people cannot do it with their own money.** Pointing it at investing decisions is the one move here the efficiency prior does not touch.

---

## ④ What carries over unchanged

The genuine output of the retired project was never the model. Read back the 2026-07 and 2026-08 ledgers: a stale-signal bug that made every live signal 5–10 sessions old, a scorer that silently died, drive-sync resurrecting frozen state, **four** distinct duplicate-retrain sources, an evidence-eating checkout race, a silently shrinking cross-section, a notebook nothing had ever parsed.

**Not one finding was "the model needs better features." Every single one was measurement integrity.**

Portable, and worth starting from rather than rebuilding:

* GitHub Actions cron scaffolding, with **sunset gates matched across workflows**
* Discord webhook + the "alarm must not sit behind a permanently-red gate" rule
* Fail-**closed** guard patterns (kill switch, gross cap, oversell guard)
* Append-only evidence ledgers with shrink detection
* `_freeze_first_write()` — written rows are immutable and the freeze is loud
* `preflight` (13 checks, incl. 12/13 which finally parse the notebook) and the `validate` suites
* The `HANDOFF.md` ledger discipline itself — dated, numbered, with retractions recorded rather than edited away

---

## ⑤ What NOT to do

* ❌ **No autonomous trading.** The panel informs; it never submits an order. This permanently retires the entire bug class that produced the HON short.
* ❌ **No AUC, rank-IC, or backtest on the panel.** It is not a predictor, and scoring it like one recreates the exact trap being exited.
* ❌ **No always-on 279-name book.** That cell is measured and closed.
* ❌ **No "v26" in October.** Rebound-building — starting again because stopping feels like losing — is the live risk. The anti-deferral rule anticipated it.
* ❌ **No new spend before a module earns it.** The feeds are already paid for and already working.

---

## ⑥ If a systematic attempt is ever made again

Both surviving options from the stopping rule remain open, but the rule attached to them binds:

> **State the prior before writing code.** The last attempt was recorded at ≈10% combined, in advance, and came in *consistent with that prior*. Do it again honestly — and if the number comes out low again, that is information, not pessimism.

⚠️ **Do not skip the base rate.** 279 US large-caps at a 5-day horizon is the most efficiently-priced, most-competed cell in global equities, and public OHLCV plus standard technicals is the toolkit best-capitalised funds saturated decades ago. **A positive result is the surprise requiring extraordinary evidence.** That is what WRC exists for; it currently reads p = 0.497.

---

## ⑦ Suggested first step, whenever that comes

**One module. The filing watcher (3.1).**

Smallest thing with standalone value, reuses a feed that already works, needs no model, cannot lose money, and produces something readable on day one.

**The test is honest and cheap: if you do not find yourself actually reading its Discord output after a few weeks, that tells you something true.** Learn it for the price of one module rather than the price of another architecture.

---

## ⑧ Open items this document does not resolve

* **The stale watchlist.** Nine tickers skip on fetch (`ANSS AVB EA EQR HOLX IPG K MMC SQ`); seven are chronically absent. Pruning or re-mapping them is a **dated MODEL change** and breaks every `n` comparison across that date — see the 8/26 ledger ⑪. Unresolved by design while a measurement window is open.
* **Why `AVB`/`EQR` intermittently miss the batch fetch.** PR #26 made the failure legible; it did not fix the fetch.
* **`HON` is uncovered.** Independent of everything above — the book should be flat regardless of what is decided about the search. It is a trade the user fires.
