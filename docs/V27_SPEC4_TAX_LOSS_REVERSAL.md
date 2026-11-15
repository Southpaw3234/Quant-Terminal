# Specification #4 — tax-loss selling reversal

**Status:** ⛔ **BLOCKED BEFORE DECLARATION, 2026-11-15. Nothing is declared.
K is 3/5 and untouched.** The availability rehearsal ran and showed the
universe contains no delistings. See §⓿. Whether to repair the universe or
retire this specification is the operator's decision; it has not been made.
**§⓿ carries a correction dated 2026-11-15: the defect is narrower than first
written.**

This occupies the slot vacated by `docs/V27_SPEC4_LISTING_DEFICIENCY.md`,
which was abandoned on 2026-11-11 before declaration when its availability
rehearsal showed the sample contained survivors rather than the deficiency
population. No slot was spent there and none is spent here. Declaring is
what costs K; everything in this document is free.

---

## ⓿ The availability rehearsal, 2026-11-15: nothing died, and nothing is missing

Run `43671860047`, count-only, over the book built by run `42977153884`. No
return was computed, the Referee was not consulted, K is unchanged.

| | |
|---|---|
| cycles carrying a book | **10 of 10** |
| positions | **2,453** across 1,359 distinct tickers |
| tickers with a usable price history | **1,359 of 1,359** |
| tickers with no history delivered | **0** |
| positions that stopped trading inside their holding window | **0 of 2,453** |

🔑 **That last row is not a result. It is a measurement of the universe.**

This book is the worst-performing quintile of a microcap universe, formed
every December for ten years. Some of those companies went to zero. A book
of 2,453 such positions in which not one stopped trading while held, and in
which not one ticker is missing a price history, was not drawn from the
companies that existed at the time. It was drawn from the ones that still
trade.

The consequence is not a small bias. The hypothesis in §② is that December's
losers REBOUND in January. The losers that did not rebound — the ones that
delisted — are exactly the ones this universe cannot contain. Measuring the
rebound on what is left measures it on the survivors of the thing being
tested, and it would flatter the result in the one direction this
specification hopes for.

**This is the same defect that retired this slot's previous occupant**
(`docs/V27_SPEC4_LISTING_DEFICIENCY.md` §⓿: 838 of 838 priced, 0 of 2,507
delisted). It is a property of `data/universe/v29_universe_pit.csv` and the
price layer under it, not of either specification, and it will be found
again by anything that conditions on distress.

### The rehearsal reported this as `status ok`

That is recorded here because it is the more useful half of the finding.
The rehearsal had a survivorship guard. It asked whether ANY series ends
before the last session; three of 1,359 do, so it passed, printed `ok`
beside every cycle, and described the zero as *"the tail the declaration
must price, not a defect"* — a sentence written on the assumption the
number would not be zero.

The guard was testing whether the price layer can see some series end
somewhere. The question that matters is whether a position that died while
it was HELD shows up at all, as a death or as a missing ticker.
`rehearse_tax_loss_availability.survivorship_refusal` now asks that, and
this exact result — 2,453 priced, 0 dead, 0 missing — is a refusal with
exit 5. `validate_extract_tax_loss.py` pins it.

### ⚠️ Correction, 2026-11-15: the roster WAS already joined

The paragraphs above say the book "was not drawn from the companies that
existed at the time", and an earlier version of this section said the
delisted roster was "not yet joined to the panel". **That was wrong, and it
overstated the defect.** `build_universe_v29.py` has admitted roster names
since the panel was built. Joining the roster to this book
(`rehearse_tax_loss_availability.roster_exposure`) gives:

| | |
|---|---|
| tickers held | 1,359 |
| of those, in the delisted roster | **335** (152 continued over the counter, 143 still listed, 40 identity mismatch) |
| positions leaving their exchange INSIDE a holding window | 5 |
| positions leaving within a year after exit | 53 |

So the universe does contain delistings. The ones it contains continued
over the counter, their price series never ends, and that is the path a
holder really had — which is why "stopped trading" reads zero.

**What is actually absent** is every departure the price source no longer
carries at all: 518 rows of `delisted_roster_2015.csv` with status
`no-data`. They cannot be screened point-in-time, because eligibility is
defined on price and volume and there is none. No free source prices them;
the second one tried (Stooq) refuses scripted access outright.

### Why each unpriceable name left

`classify_departures.py` reads each company's own filing history on
data.sec.gov and applies stated, mechanical rules. All 518 resolved; no
lookup failed. `data/universe/departures_classified.csv` holds the rows.

| reads as | all 518 | inside the panel window (385) |
|---|---|---|
| **adverse** — bankruptcy, delisted for cause, deregistered | 155 | 127 |
| **benign** — acquired, SPAC redeemed, fund merged | 212 | 127 |
| **not a departure** — still listed, exchange transfer, kept reporting | 131 | 117 |
| unknown | 20 | 14 |

Two things follow, and neither was known before this was counted.

1. **"No-data" is not "died".** A quarter of these rows are not departures
   at all: a Form 25 removes one security or one venue, and the company
   carried on. Of the real departures inside the window, the adverse and
   the benign are **even — 127 each**. Missing bankruptcies flatter a
   result; missing acquisitions understate one. The bias this universe
   carries has two signs, not one.

2. **For THIS book the gap is small in count.** Across the ten holding
   windows, 52 unpriceable departures were filed. Nineteen are not
   departures and three are unknown, leaving **13 adverse and 17 benign** —
   against 2,453 positions. Even if every one of the 13 had been eligible
   and in the worst quintile, that is about half a percent of the book.

⚠️ **Two limits on that second point, both real.** The roster is thin
before 2023 — 6 to 26 unpriceable rows a year from 2015 to 2022 against
113 and 165 in 2024 and 2025 — because EDGAR drops the ticker from an old
filer's name and the roster is found by ticker; so the early cycles are
under-counted by an unknown amount. And the six-week holding window is
short: a name that fails in March is not in "departures during a window",
though it was a loser in December.

### What it would take

Either of two things, and the choice is the operator's:

* **Price the gap.** A survivorship-free dataset with delisted names and
  their final prices. Not free. This is the only route to a number with no
  caveat.
* **Pre-register the bound.** Declare, before the read, how the result is
  restated if N phantom positions at a total loss are added per cycle, and
  what N is taken to be. `qt.measurement.survivorship_sensitivity` already
  exists for this. The read then carries its own worst case instead of an
  unquantified warning.

Until one of those is chosen **nothing in §⑤–§⑧ should be declared**, and
the open question about the reality-check bar (0.0167 against 0.02) still
does not need answering.

No slot was spent learning any of this. That is what the rehearsal is for.

---

## ① What the record already rules out

Five reads, no finding. They are not five independent failures — they share
a shape, and the shape is what this specification is chosen against.

| read | result | how it died |
|---|---|---|
| `form4_cluster_buy_v1` | +1.76%, t 1.16 | nothing there |
| `form4_cluster_buy_v2` | +1.19%, t 0.62 | median **−4.49%**, 40% positive |
| `pead_8k_car_v3` | −3.81%, t −4.04 | wrong sign, decisively |
| `value_ebit_ev_v1` | — | stability: first third ≤ 0 |
| `value_ebit_ev_v2` | — | stability: first third ≤ 0 |

Two lessons are load-bearing here, and both come from the record rather than
from theory.

**🔑 THE MEAN WAS CAPTURED BY A TAIL.** Spec #2 cleared its mean bar at
+1.19% while the *typical* event lost 4.49%. Forty events out of 406 held
seven times the entire positive mass; strip them and the other 366 average
−8.05%. A criterion set that tests only the mean will hand a pass to a
losing strategy with a lottery ticket attached. §⑦ fixes that with a median
bar, and it is the single most important difference between this
specification and the three that preceded it.

**🔑 THREE OF FIVE DIED ON STABILITY.** An effect present in one stretch of
the sample and absent in another. In each case stability was an *accident*
of the sample being long enough to split. Here recurrence is the hypothesis
itself (§⑥), so the same test becomes a direct question rather than an
afterthought.

Spec #2's own tail analysis wrote the prescription this specification is
answering:

> A third specification must have a mechanism where the MEDIAN outcome is
> positive — slow information diffusion or a mechanical flow, not a
> fundamental bet on survival.

Post-earnings drift was tried as spec #3 and failed hard. What remains from
that sentence is **mechanical flow**.

---

## ② The hypothesis

A taxable holder sitting on a loss has a reason to sell that has nothing to
do with what they think the stock is worth: realising the loss before
31 December reduces that year's tax bill. The deadline is statutory, the
holder has no discretion about *when*, and the selling is therefore
concentrated into the last weeks of the calendar year and falls hardest on
the names already down most.

In January that pressure is simply absent. Not reversed by buyers with a
view — absent. The same supply that had to clear by 31 December does not
exist on 2 January.

**The claim:** within a universe too small for arbitrage capital to work in,
the names most exposed to year-end loss realisation are priced below the
benchmark in late December and recover relative to it through January.

Why this belongs to the **constrained, not uninformed** family, which is
what §⑤ of the abandoned sketch argued for:

- **The constraint is the tax code.** It does not get competed away, because
  the sellers are not trying to beat the market. They are trying to book a
  loss before a date. An information edge disappears once published; a
  deadline does not.
- **It recurs on a schedule.** Every December, for as long as the tax year
  ends on 31 December.
- **It is strongest exactly where arbitrage capital cannot go.** The V29
  panel screens to $50k–$5M ADV. A fund that could absorb this supply cannot
  put meaningful money to work at that size — which is the entire premise of
  the V29 programme, applied here more cleanly than it applied to value.

### ⚠️ The objection that has to be stated, not buried

**This is the most heavily studied calendar anomaly in equities.** The
January effect has been in the literature since the 1970s and has been
widely reported as having decayed. That is a real mark against the prior and
it is why the signed prior in §⑨ should be low.

The counter-argument is specific rather than hopeful: published decay
studies are overwhelmingly run on universes that institutions can trade. The
V29 band is below that. This specification is not a claim that the January
effect is undiminished — it is a claim that it survives in the part of the
market where the arbitrage that killed it elsewhere cannot operate. If it
does not survive there either, that is a clean negative and worth knowing.

---

## ③ 🔑 The selection rule conditions on a calendar and a rank, never on an outcome

The entire selection is: *be in the universe on 30 November, and be in the
bottom quintile of calendar-year-to-date return.* Both are knowable on the
formation date. Nothing about what happens afterwards enters it.

**Year-to-date, not trailing twelve months.** This is a mechanism choice and
not a fitted one. The tax year is the calendar year, so the loss a holder can
realise is measured from 31 December to 31 December. A trailing-12-month
ranking would measure something the tax code does not care about.

**A rank, not an absolute threshold.** "Down more than 20%" sounds more
concrete but behaves worse: in 2021 almost nothing qualifies and in 2022
almost everything does, so book size swings with the market rather than with
the mechanism. A quintile cut adjusts automatically and keeps the book
roughly constant, which the minimum-book criterion needs.

**A quintile, not a decile.** The wider cut is the more conservative choice:
it dilutes the effect if one exists, so it biases the read *against* a pass.
A decile would concentrate the signal, and picking the narrower cut because
it concentrates the signal is how a parameter gets fitted before anyone has
looked at anything.

What the rule explicitly does **not** condition on: any return after the
formation date, any fundamental, any news, any filing, delisting status, or
whether the name still exists at exit.

---

## ④ The measurement: an annual book, flat for forty-six weeks

Equal-weighted long book, formed once a year, hedged with IWM. Measured in
calendar time with `qt/calendar_time.py`, the same machinery as spec #3.

```
  30 Nov          15 Dec                        31 Jan
    |               |                              |
    |-- rank YTD -->|======== book is live =======>|
  eligibility     enter                          exit
  fixed here      at close                       at close
```

**🔑 WHEN THE BOOK IS FLAT, THE BENCHMARK IS FLAT.** Outside the window the
strategy holds nothing, and the comparison must therefore be against holding
nothing — excess return is exactly zero on those days, and they are excluded
from the measured series entirely.

This matters more than it looks. If flat days were scored against a live
IWM, the read would be measuring *"six weeks of small caps versus twelve
months of IWM"*, which is mostly a statement about whether IWM went up. That
is a different hypothesis and not the one being declared. Only the ~30
sessions a year when the book is live are measured.

**Turnover is one round trip a year.** The book is bought in December and
sold in January, and nothing is traded in between. At the 50 bp round-trip
assumption that is 50 bp a year of cost, against strategies elsewhere in
this programme that rebalance monthly. This is a genuine structural
advantage of the design and it is why the cost assumption is unlikely to be
what kills it.

---

## ⑤ Pre-registered parameters

Every one of these is fixed before the read. Changing any is a **new
specification** and a different slot.

| | |
|---|---|
| universe | `data/universe/v29_universe_pit.csv` — frozen, point-in-time, ADV $50k–$5M, price ≥ $2 |
| | 152,006 name-months · 3,259 tickers · 2015-09-30 .. 2025-08-31 |
| membership | as of the month-end **on or before** the formation date (i.e. 30 November) |
| ranking metric | total return from the prior 31 December close to the formation close |
| selection | **bottom quintile** of that metric among eligible names |
| entry | first session **on or after 15 December**, at the close |
| exit | first session **on or after 31 January**, at the close |
| cycles | **10**: Dec 2015 → Jan 2016, through Dec 2024 → Jan 2025 |
| weighting | equal, within the book |
| minimum book | **5 names**; below that the cycle is flat and reported, not dropped |
| benchmark / hedge | **IWM**, held only while the book is live |
| flat days | excluded from the measured series; excess is zero by construction |
| round-trip cost | **50 bp**, charged once per cycle on full turnover |
| delisting assumption | **−30%** base case, with sensitivities (see §⑧ criterion 8) |
| standard error | Newey–West, **lag 10**, within cycle |
| reality check | WRC **and** SPA, `p < α/K_declared = 0.10/5 = 0.02`, unread slots charged |
| terminal date | 2027-03-31 (unchanged) |

The 15 December and 31 January anchors are round calendar dates either side
of the 31 December deadline. They are not tuned, and that claim is checkable
rather than merely asserted: the extractor is written, reviewed and merged
before the read is dispatched, so the git history fixes them in advance.

---

## ⑥ ⚠️ Ten Decembers, not three hundred days

This is the weakness of the specification and it belongs in the open, before
a prior is signed rather than in the discussion of a result.

The measured series will contain roughly 300 daily observations. **They are
not 300 independent observations.** They are ten annual episodes of about
thirty days each, and within an episode the daily returns of an
equal-weighted small-cap book are heavily correlated. The effective sample
size is much closer to **ten** than to three hundred.

Newey–West at lag 10 absorbs part of this. It does not absorb all of it, and
it is applied within cycles — the autocorrelation structure across a
ten-month gap between episodes is not meaningful and is not modelled.

Three consequences, all of which are accepted in advance:

1. **A daily t-statistic will overstate the evidence.** It is reported
   because the rest of the programme reports it and comparability matters,
   but it is not the criterion this specification should be judged on.
2. **The per-year sign test (criterion 5) is doing the real work.** With ten
   cycles, "positive in at least seven" is a statement about consistency
   that a daily t-statistic cannot make.
3. **A pass here is weaker evidence than a pass with the same numbers would
   have been for spec #3.** Spec #3 had 1,025 genuinely separate events.
   This has ten. §⑩ says what that limits.

---

## ⑦ The median criterion, and why the mean is not enough

Spec #2 is the reason this section exists.

```
mean                        +1.19%   ← cleared its bar
median                       -4.49%
positive events                 40%
top 10% (40 of 406 events)  +3,427% summed
mean without that top decile  -8.05%
```

A mean bar passed a strategy in which the typical position lost money and
the average was manufactured by forty lottery tickets. Nothing in that read's
criteria could see it, and the finding only surfaced because a tail analysis
was run afterwards out of suspicion.

So this specification pre-registers **two** median bars, at the two levels
where the failure can hide:

- **Per name** — the median holding-period excess return across every
  position taken across all ten cycles must be positive. This is the bar
  spec #2 would have failed.
- **Per cycle** — the median of the ten annual excess returns must be
  positive. This catches a strategy carried by one extraordinary year.

A mean bar is retained as well, but it is now the weaker of the two. The
intent is explicit: **if the mean and the median disagree, this
specification fails.** That is the lesson of spec #2 written into the
criteria in advance, where it can act, rather than into a post-mortem.

---

## ⑧ The criteria

All eight must clear. Any single miss is a **NOT MET** verdict.

| # | criterion | bar |
|---|---|---|
| 1 | measured sessions | **≥ 250** |
| 2 | book size | **≥ 5 names** on every live session of every counted cycle |
| 3 | annualised excess over IWM, net of cost | **> 0** |
| 4 | Newey–West t on daily excess | **≥ 2.0** |
| 5 | cycles with positive excess | **≥ 7 of 10** |
| 6 | median **per-name** holding-period excess | **> 0** |
| 7 | median **per-cycle** excess | **> 0** |
| 8 | WRC **and** SPA p, Bonferroni over K_declared | **< 0.02** |

Criterion 5 under the null alone is p ≈ 0.17 — it is a **shape**
requirement, not a statistical one, and it is not claimed as evidence by
itself. What it rules out is the failure mode this programme has seen three
times: an effect that lives in two years out of ten and averages well.

**Criterion 8 is evaluated under three delisting assumptions** — the −30%
base case, 0%, and −100% — and the verdict must be unchanged across all
three. A six-week window makes delisting rare, so this sensitivity should be
cheap; if it is not cheap, that is itself the finding and the verdict is NOT
MET.

Standing rule, restated because it applies the moment this is declared:
**a read is spent when taken. A bug found afterwards does not reopen it and
does not reset the clock.**

---

## ⑨ What has to happen before this is declared

None of this costs a slot. All of it must precede one.

1. **The extractor and its hermetic validator are written, reviewed and
   merged.** Selection must be provably blind to any post-formation
   outcome — the same `inspect`-based guard spec #4's validator used to
   prove its selection could not see a price.
2. **The availability rehearsal is run and recorded** — count only, no
   return. How many names qualify in each of the ten Decembers, how many
   live sessions each cycle carries, how many positions delist mid-window.
   This is the step that caught spec #4, and it is cheap now that the
   machinery exists.
3. **The event file is fingerprinted** and the sha256 recorded in the
   declaration, so the input cannot move between rehearsal and read.
4. **The prior is signed** by the operator, in a commit that precedes the
   read, with the git timestamp as the evidence.

On the prior: five of five reads have returned NOT MET, and this is the
most-studied anomaly of the three candidates. A signed prior above the
project's 15% baseline would need an argument this document does not make.

---

## ⑩ What a pass would and would not establish

**Would:** that within a universe institutions cannot trade at size, names
most exposed to year-end loss realisation were underpriced relative to IWM
in late December and recovered through January, consistently across at least
seven of ten years, after 50 bp of cost, and not as an artifact of searching
five specifications.

**Would not:**

- **That it will persist.** Ten cycles is ten cycles. A constraint-driven
  effect should persist while the constraint does, but the tax code is
  subject to change and the universe's composition is not fixed.
- **That it is tradeable at any size.** The book is several hundred names at
  $50k–$5M ADV. Capacity is a separate question this read does not answer,
  and the 50 bp assumption is a declared parameter rather than a measured
  fill.
- **That the January effect is undiminished generally.** The claim is
  specifically about the illiquid band. A pass here says nothing about
  anywhere else.
- **That the mechanism is tax-loss selling.** The read measures a calendar
  pattern in a ranked subset. Any other December-specific effect that
  correlates with being down on the year would produce the same number. The
  mechanism is the *reason for looking*, not a thing the read establishes.
