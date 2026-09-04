# Specification #2 — proposal (UNDECLARED)

**Drafted:** 2026-09-04
**Status:** 🟢 **DECLARED 2026-09-04** as `form4_cluster_buy_v2` in `data/registry/specifications.json`. **Not read.** Declaring cost nothing;
reading spends one of the four remaining specifications under
`docs/V27_PREREGISTRATION.md`.

---

## ① What failed, precisely

Spec #1 (`form4_cluster_buy_v1`, read 2026-09-02) failed on **two** bars, and
they are qualitatively different:

| bar | result | kind of failure |
|---|---|---|
| N ≥ 40 | 160 ✅ | — |
| mean ≥ +0.50% | **+1.7647%** ✅ | — |
| **t ≥ 2.0** | **+1.16** ❌ | **power** |
| **stability ≥ 0.50** | **−0.52** ❌ | **not power** |

🔑 **The effect size was never the problem.** +1.76% is three and a half times
the bar. What failed is that `sd = 19.22%` swamps it — the standard error is
1.52%, so even a healthy mean sits barely one standard error from zero, and
**49% of events were positive**, a coin flip with a right skew.

## ② The arithmetic, computed from the frozen ledger

```
observed:  N=160  mean=+1.7647%  sd=19.22%  t=1.16
N required for t = 2.0, holding mean and sd:   474
t if N tripled, holding mean and sd:           2.01
```

**474 observations.** The current window is 365 days and produced 205 events
(160 after the independence and settlement filters). Roughly three years of
history would produce ~480.

## ③ The proposal

**Change exactly one thing: the lookback window, 365 days → 1,095 days.**

Everything else held identical — same 907-name universe, same
`≥2 insiders / 5 days / $100,000` event definition, same 21-bar horizon, same
SPY control, same entry rule.

Why this and not something else:

* It attacks **the binding constraint**, which the arithmetic above identifies
  unambiguously as sample size.
* It does **not change the hypothesis**, so a pass or a fail is attributable.
  Changing the event definition *and* the window would leave neither testable.
* It is the only single-parameter change with the power to move t from 1.16 to
  the bar at all.

## ④ ⚠️ Three reasons to be uncomfortable with it

**This section exists because a proposal that only argues for itself is a sales
pitch.**

**1. It only fixes ONE of the two failures.** Stability was −0.52 — the effect
did not decay, it **reversed sign** (first third +1.01%, last third −0.52%).
That is not a small-sample artifact in any obvious way, and **more data cannot
fix an effect that reverses; it can only measure the reversal more precisely.**
If the reversal is real, spec #2 fails on stability at N≈480 exactly as spec #1
failed at N=160, and a specification will have been spent confirming it.

**2. It lands exactly on the bar, which is not a margin.** t = 2.01 assumes the
mean and sd both hold at three times the sample. They will not hold exactly. A
design whose success case is a coin flip at the threshold is a design that will
produce an ambiguous result more often than a clear one.

**3. Survivorship gets WORSE, not better.** The universe is today's listed
names. Over 365 days a modest number of companies delisted; over 1,095 days
substantially more did, and **every one of them is invisible.** Insiders buy on
the way down too. Extending the window therefore *increases* the upward bias in
the direction that would flatter a pass. `build_universe.py` cannot fix this on
Finnhub's free tier.

## ⑤ Honest prior

Spec #1 came in with a large effect size and no significance. The single most
likely explanation remains that **+1.76% is a right-skewed noise draw** rather
than an edge, and that tripling the sample pulls the mean toward zero rather
than pulling t toward 2.0.

Stating a number before the read, as `V27_PREREGISTRATION.md` §⑥ requires:

```
P(spec #2 clears all four E1 bars) = ______%     signed: ________  date: ______
```

*Anchor, not a recommendation:* the project-level prior was set at 15%. Spec #1
has since failed, and its failure included a mode more data does not address.
A number **below** 15% is the defensible direction.

## ⑥ What must be checked first — free, and does not touch K

1. **Does Finnhub return three years of insider history on this key?** The
   2026-09-01 probe only requested 365 days. If the tier caps at one or two
   years, this proposal is not executable as written and must be revised
   *before* declaration, not after seeing a truncated result.
2. **How many events does 1,095 days actually yield?** Counting is free. If it
   is materially under ~480, the power argument weakens and the proposal should
   be reconsidered rather than run.

⚠️ Both are **availability** questions. Neither computes a return, and neither
spends budget. They must be answered before declaration.

## ⑦ The alternative worth weighing

**Do not spend spec #2 at all.**

Three of four v25 criteria failed, S4 is pending, and spec #1 failed with a mode
that more data does not repair. Four specifications is not many, and the
strongest argument for holding one back is that a *better* hypothesis may arrive
later — and cannot be tested if the budget is gone.

`V27_PREREGISTRATION.md` §⑤ already names the failure mode this guards against:
*"Testing more than K specifications and reporting the best."* Spending them
quickly, on variations, is how that happens without anyone deciding to.

---

## ⑧ Declaration record — 2026-09-04

Declared as **`form4_cluster_buy_v2`** after both §⑥ checks cleared:

| check | result | run |
|---|---|---|
| Finnhub returns 1,095d? | **yes** — 561 events, 278 tickers; 2024/2025 at ~36k filings each | `33927199851` |
| dedup at 63 bars? | **127 settle vs bar 40** on the v1 set; ~390 expected on the 561-event set | `33927201211` |

**Inputs are frozen by file:** events `data/events/events_form4_1095d.csv`, universe
`data/universe/v27_universe.csv`. **Output goes to `data/events/event_study_v2.csv`
— never `event_study.csv`, which is v1's frozen ledger.** The E1 job refuses that
combination with exit 4 before a price downloads.

**Budget: K remains 1/5.** A declaration spends nothing.

🔴 **STILL REQUIRED BEFORE THE READ — the §⑤ prior.** Blank until the operator
fills it:

```
P(spec #2 clears all four E1 bars) = ______%     signed: ________  date: ______
```

The Referee will authorise the read without it; the *documents* will not. That
gap is deliberate — the number has to be yours, written before any result exists
to argue against.
