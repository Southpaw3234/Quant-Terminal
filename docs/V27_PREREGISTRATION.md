# v27 — Pre-Registration

**Drafted:** 2026-09-01
**Status:** 🔴 **UNSIGNED — NOT YET BINDING.** Becomes binding only when §⓪ is completed
by the operator. Until then this is a draft and no v27 code should be written.

> **Why this exists.** In v25, four criteria (S1–S4) were pre-registered and three failed.
> The thresholds were **not renegotiated**, and that is the single most valuable thing the
> project produced. This document applies the same instrument to v27 *before* any result
> exists to argue with. A threshold written after seeing data is not a threshold.

---

## ⓪ To be completed by the operator BEFORE any v27 code is written

**P1 — Stated prior.** Probability that v27 produces a strategy clearing E1–E4 and
surviving to a live capital ramp:

```
P(success) = ______%          signed: ____________   date: __________
```

*Anchor, not a recommendation:* the v25 attempt was recorded in advance at **≈10%
combined** and came in **consistent with that prior**. v27 targets a less-competed cell,
which argues for a higher number; it also has no working component yet, which argues for
a lower one. **Write your own number.** If it comes out low, that is information, not
pessimism — and it is much cheaper to learn here than in §⑤.

**P2 — Hypothesis budget.** Number of distinct event types / specifications that will be
tested before the WRC/SPA correction is applied:

```
K = ______           (declare it now; testing more than K invalidates E1)
```

**P3 — Intended full allocation.** The capital §⑤ ramps toward. Needed now so the ramp
percentages are meaningful and cannot be re-based later:

```
Full allocation = ____________
```

---

## ① Scope

**In scope.** Event-conditioned setups on a US equity universe deliberately inverted from
v25's — small, illiquid, uncovered, index-deleted — consumed as *events* rather than as
scalar features, at a horizon longer than 5 trading days.

**Out of scope, permanently.** The v25 cell: 279 US large-caps, price-derived features,
5-day horizon, daily rebalance. That is measured and closed. Re-testing it is not a v27
hypothesis and does not count against K.

---

## ② The four criteria

All four must pass. They are **conjunctive**, not a scorecard, and no criterion may be
substituted for another.

### E1 — In-sample event edge
Mean abnormal return vs. a matched control, over the declared holding horizon.

| requirement | bar |
|---|---|
| effect size | mean abnormal return **≥ +0.50%** per event |
| significance | **t ≥ 2.0** |
| sample | **N ≥ 40 independent events** (overlapping windows on the same name do not count as independent) |
| multiple testing | must clear **WRC and SPA at p < 0.10** against the declared K |
| **stability** | effect in the **final third** of the sample ≥ **50%** of the effect in the first third |

⚠️ **The stability clause is not optional.** v25's S3 produced a genuine positive read
(+0.0254 over folds 1–6) that **decayed to −0.0053 by folds 9–12**. An effect that decays
monotonically across time is a discovery about the past, not a strategy.

**Read date: 2026-12-15.**

### E2 — Out-of-sample
E1 re-run on a period **never used for specification, feature choice, or event definition**,
held out from the start and touched exactly once.

| requirement | bar |
|---|---|
| effect retention | **≥ 50%** of the E1 effect size |
| significance | **t ≥ 1.5** on the held-out sample alone |

**Read date: 2027-01-15.** Held-out period must be declared in writing before E1 is read.

### E3 — Forward paper
Live paper trading on Alpaca, full execution path, real timestamps, real slippage model.

| requirement | bar |
|---|---|
| duration | **N ≥ 30 trading days** |
| return | annualised **Sharpe ≥ +0.5** |
| fidelity | realized return **≥ 50%** of E2-predicted — no decay beyond half |
| operations | **zero** unexplained reconciliation breaks between broker and ledger |

**Read date: 2027-03-01.**

### E4 — Cost and capacity
The edge must survive execution at the intended size.

| requirement | bar |
|---|---|
| net of costs | E3 Sharpe computed **after** modelled slippage at intended size stays **≥ +0.5** |
| capacity | intended position size **≤ 5% of 20-day ADV** for every name traded |

**Read concurrently with E3.**

---

## ③ Restart budget

**One (1) window restart is available for the entire v27 project.**

v25's Frame 1 used both of its restarts and the final read had to stand whatever was
found afterward. A restart is: discarding accumulated observations and beginning the
count again, for any reason — bug, data fix, specification change.

```
Restarts used: 0 / 1
```

Spending it must be recorded here with a date and a reason, in the same commit as the
code change that caused it.

---

## ④ Anti-deferral clause

**A bug, data error, or improvement discovered after a criterion has been read does not
reset that criterion's clock and does not reopen it.**

This is the clause that did the most work in v25 and it is carried over verbatim in
force. It exists because the temptation to reopen is strongest precisely when the answer
is one you do not want. Specifically:

* A failed criterion stays failed.
* "We would have passed if not for X" is not a pass, whatever X is.
* Finding a real bug is grounds to fix it in the *next* project, not to re-run this one.
* **Terminal date: 2027-03-31.** If E1–E4 have not all passed by then, v27 closes on that
  date regardless of how close anything is, and regardless of work in flight.

---

## ⑤ Capital ramp — only if E1–E4 all pass

Expressed as a percentage of the §⓪ P3 allocation so it cannot be re-based after the fact.

| stage | size | hold | advance if | abort if |
|---|---|---|---|---|
| 1 | **5%** | 20 trading days | no abort trigger | drawdown > **5%**, or any reconciliation break |
| 2 | **15%** | 20 trading days | stage-1 Sharpe ≥ 0 | drawdown > **7%** |
| 3 | **40%** | 40 trading days | stages 1–2 combined Sharpe ≥ +0.3 | drawdown > **10%** |
| 4 | **100%** | — | stages 1–3 combined Sharpe ≥ +0.5 | drawdown > **12%** |

**An abort at any stage returns to paper, not to the previous stage.** The ramp is not
resumed on judgment; re-entry requires a fresh E3.

⚠️ **B5 (ramp controller) must enforce this in code.** A ramp that depends on the operator
choosing not to size up after a good week is not a control.

---

## ⑥ What would make this document worthless

Recorded now, while it is still cheap to admit:

* Reading E1 before the held-out period for E2 is declared.
* Testing more than K specifications and reporting the best.
* Treating E3's paper Sharpe as sufficient because E1 and E2 "were close."
* Extending the terminal date because progress is being made.
* Starting B and C before E1 returns a number, so that sunk cost argues for continuing.

**Every one of these is a thing a reasonable person does under mild pressure.** That is
why they are written down here rather than left to be noticed later.
