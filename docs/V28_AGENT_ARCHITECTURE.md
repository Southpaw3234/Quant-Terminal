# v28 — Autonomous Agent Team

**Written:** 2026-09-02
**Status:** 🟡 **ARCHITECTURE.** No code, no crons, no workflows.
**Decision:** keep what v25 proved worth keeping, replace the rest, and operate it
as a team of agents with separated responsibilities.

> ⚠️ **Recorded override.** `NEXT_ARCHITECTURE.md` §⑤ says "no autonomous trading",
> and §⑤ named rebound-building as the live risk. `V27_BUILD_PLAN.md` already
> overruled that once. This document does so again, knowingly. The reasoning in
> §⑤ is not withdrawn — the C-layer guards and the referee in §④ exist to answer
> it, and if they are ever removed the objection returns in full force.

---

## ⓪ The one thing this architecture cannot do

**It cannot create an edge.** Four pre-registered reads have failed:

| read | result |
|---|---|
| S1 Frame 3 | ann. SR −3.94 |
| S2 Frame 2 | t −1.54, retired |
| S3 label | +0.0254 (folds 1–6) → −0.0053 (folds 9–12) |
| **E1 spec 1** | **mean +1.76%, t 1.16, stability −0.52** |

None failed for want of orchestration. E1 failed because **sd = 19.22% swamped a
1.76% mean**, and 49% of events were positive — a coin flip with a right skew.
An agent team does not shrink a standard deviation.

🔑 **So the split that defines this architecture: agents own everything EXCEPT
the edge.** Data, measurement, risk, execution, reconciliation, ops, adjudication
— all agent work, all genuinely improved by separation of concerns. The edge
stays behind E1–E4 exactly as it is today. **An agent that proposes a strategy has
no more authority than a human proposing one, and is subject to the same K.**

Building the team is worth doing on its own terms: it replaces a 404KB script and
a 516KB notebook with something testable. It is not a plan to make the null go
away.

---

## ① What carries over from v25 — unchanged

The genuine output of the retired project, none of it model code:

* **Fail-closed guard stack** — gross cap, sector cap, oversell guard, kill switch (`dc04017`, `a0dd471`)
* **Append-only frozen ledgers** — `_freeze_first_write()` (`203d95c`), ledger guard (PR #25)
* **Settled-row withholding** — will not score an unsettled bar; structural, not clock-based
* **WRC / SPA** multiple-testing gates (`66312ae`)
* **Ops self-heal** — marker-gated retrain, four dup-retrain fixes, live-tip checkout (`86947a6`), watchdogs, Discord
* **`preflight` + `validate` suites** — 13 checks and five suites
* **Twelve wired data feeds** — SEC, Quiver, Finnhub, FRED, Ken French, PatentsView
* **`HANDOFF.md` ledger discipline** — dated, numbered, retractions recorded not edited away
* **Pre-registration discipline** — the single most valuable thing either project produced

And from v27, already built and tested:

* `event_study.py` — look-ahead, fake independence and unsettled exits made unrepresentable
* `extract_form4.py` — event extraction with the code-P distinction
* `build_universe.py` — point-in-time ADV, inverted screen, MIC filter

## ② What v25 leaves behind

| left behind | why |
|---|---|
| the feature set / 5-day classifier | walk-forward AUC ≈ 0.50 |
| Frame 2 intraday | t −1.54, retired under S2 |
| Frame 3 stat-arb | ann. SR −3.94 |
| legacy `confidence` scoring | 0.50-clamp artifact, 97.5% ties |
| 279 mega-cap universe | measured and closed |
| **`quant_runner.py` (404KB)** | one file, prepatch strings, `SECTOR_MAP` inside a string literal, unparseable by `ast` |
| **`trading_model_v25.1.ipynb` (516KB)** | notebook-as-production; cells rewritten at runtime by `[src rewrite]` patches |

🔑 **The monolith is the real technical debt.** Thirteen dated model changes and
not one was "the model needs better features" — every incident was measurement
integrity, and most were only possible because a 900KB two-file system had no
seams to test at. v28's structure is the fix for that, independent of alpha.

---

## ③ The team

Seven roles. Each is a module with an explicit contract, its own tests, and no
write access outside its lane. "Agent" here means a bounded role with a
declared interface — not an LLM given a broker key.

### 3.1 Data
Owns the twelve feeds, the universe, extraction. Writes `data/raw/**` and
`data/universe/**`. **Never** sees returns. Quality checks are its own: schema
drift, staleness, coverage, and the funnel that must sum to its input.

### 3.2 Measurement
Owns `event_study.py`, WRC/SPA, every ledger. Writes `data/evidence/**`,
append-only and frozen. **The only role permitted to compute a return.**
Enforces settle, freeze, independence and point-in-time membership.

### 3.3 Research
Proposes specifications. **Must write the pre-registration before any code**, and
may not read `data/evidence/**` outputs for a specification until it is declared.
Its proposals are inputs to the Referee, not decisions.

### 3.4 Referee — the important one
**Owns the pre-registration and can VETO.** Counts K, refuses a read that would
exceed it, refuses a read whose specification was not declared first, enforces
the anti-deferral clause and the terminal date. Writes nothing except verdicts.

🔑 **Separating the proposer from the judge is the one place an agent team buys
something a single operator cannot easily give themselves.** The discipline that
killed S1, S2, S3 and E1 was manual and held four times — but it was the same
person holding both roles, and that only stays true while the person is
disinterested. Make it structural.

### 3.5 Risk
Position sizing, per-name and portfolio drawdown, beta hedging, ADV caps, the
staged capital ramp. **Fails closed.** Has veto over Execution and does not need
anyone's agreement to use it.

### 3.6 Execution
Order lifecycle: partial fills, rejects, PDT, T+1 settlement, wash sales.
**Broker is source of truth, never the ledger.** Reconciles every run and halts
on an unexplained break.

### 3.7 Ops
Watchdogs, self-heal, alerting, the sunset gates. Owns the rule that an alarm
must never sit behind a permanently-red gate — learned from HON standing red for
21 days.

### Contract between roles

```
Data ──▶ Measurement ──▶ Referee ──▶ Risk ──▶ Execution
                            ▲
                       Research (proposes only)
                                              Ops watches all
```

Three invariants, each mechanically enforced rather than documented:

1. **Only Measurement computes returns.**
2. **Only Referee authorises a read**, and it counts K.
3. **Risk and Ops can halt anything. Nothing can halt them.**

---

## ④ Build order

Phase numbering is deliberate: nothing that touches money exists until an edge
has cleared its gates, because that is the sequencing v25 got wrong.

| phase | contents | gate to leave it |
|---|---|---|
| **0** | Repo restructure — package layout, kill the monolith, port the guard stack and ledgers into modules with tests | `preflight` + all validate suites green on the new structure |
| **1** | Data + Measurement + Referee | Referee can veto a real proposal; Measurement reproduces the E1 spec-1 read **exactly** from frozen inputs |
| **2** | Research proposes spec #2 under the existing pre-registration | E1–E4 on ≥1 specification |
| **3** | Risk + Execution, paper only | E3 clean: ≥30 days, zero unexplained reconciliation breaks |
| **4** | Live, staged ramp 5→15→40→100% of $10,000 | the ramp's own abort conditions |

⚠️ **Phase 1's exit test is not decoration.** If the rebuilt Measurement cannot
reproduce `+1.7647%, t 1.16, stability −0.52` from the frozen event ledger, the
rebuild has changed something nobody intended, and that is exactly the class of
silent drift v25 kept discovering months late.

## ⑤ What has NOT changed

* **K = 5, and one is spent.** v28 is a rebuild of the machinery, **not a reset
  of the budget.** Four specifications remain. A new architecture is not a new
  hypothesis.
* **The anti-deferral clause binds.** E1 spec 1 stands.
* **Terminal date 2027-03-31.**
* **Survivorship in the A4 universe is unfixed** and biases any result upward.
* **Nothing touches the live pipeline before 2026-09-29.** S4 reads obs #30 on
  9/28.

🛑 **If v28 is used to justify re-reading a spent specification, it has become
the rebound-building `NEXT_ARCHITECTURE.md` §⑤ warned about.** The tell is
simple and worth writing down while it is still cheap to admit: *rebuilding the
machinery is legitimate; resetting the counter is not.*

## ⑥ Suggested first step

**Phase 0, and only Phase 0.** Restructure the repo and port the guard stack into
tested modules. It is the largest genuine improvement available, it needs no
edge, it cannot lose money, and it makes every later phase cheaper.

It also answers a question worth answering honestly: if the rebuild stalls at
Phase 0, that says something true — and it is much cheaper to learn there than
at Phase 4.
