# Specification #4 — listing deficiency (8-K Item 3.01)

**Status:** ⚪ **DRAFT. Nothing is declared. K is 3/5 and untouched.**
Supersedes the event-class section of `docs/V27_SPEC4_SKETCH.md`, which was
written before the spec #3 read and before `qt/calendar_time.py` existed.

This document exists so the design is settled, argued with, and rehearsed
*before* anyone decides whether it is worth one of the two remaining slots.
Declaring is what spends K. Everything described here costs nothing.

---

## ① What the record already rules out

Five reads, no finding. They are not five independent failures; they share a
shape, and the shape is what this specification is chosen against.

| read | result | how it died |
|---|---|---|
| `form4_cluster_buy_v1` | +1.76%, t 1.16 | nothing there |
| `form4_cluster_buy_v2` | +1.19%, t 0.62 | nothing there; first third ≤ 0 |
| `pead_8k_car_v3` | **−3.81%, t −4.04** | the selection rule bought reversal |
| `value_ebit_ev_v1` | +2.71%, t 0.71, DD 50.9% vs 41.1% | real but tiny; first third ≤ 0 |
| `value_ebit_ev_v2` | +1.79%, t 0.53, DD 47.3% | worse; first third ≤ 0 |

**Every one of them tested a mechanism that persists because someone is
UNINFORMED.** Insider buying and earnings surprises are information. Value is
the most published factor in finance. Information is arbitraged once it is
public, and the record is consistent with exactly that.

**Not one constraint-based mechanism has been tested.** That is the single
largest untried region, and it is why this specification is the one proposed
rather than a sixth variation on a theme that has now failed five times.

---

## ② The hypothesis

> When a company receives a continued-listing deficiency notice, holders with
> listing mandates must sell **regardless of view**. That selling is forced,
> concentrated in time, and insensitive to price. Prices fall further than
> the information in the notice justifies, and recover as the forced supply
> clears.

The mechanism persists not because the information is secret — the notice is
a public filing — but because **the seller is not trying to beat the market.**
A fund whose mandate forbids holding non-compliant listings sells because the
mandate says so. No amount of arbitrage capital changes the mandate. The rule
creating the constraint is what would have to change, and rules change slowly.

That is a materially different claim from "the market has not noticed yet,"
which is what the five failed reads were all variants of.

### ⚠️ The honest case against it

- **It is not undiscovered.** Forced-selling effects are studied. What is less
  arbitraged is forced selling in names too small for the funds that would
  arbitrage it, which is the $50k–$5M ADV band this universe already screens
  to — the one structural edge a small account genuinely has.
- **The names are distressed.** This buys companies in breach of a listing
  rule. Some go to zero. §⑥ prices that explicitly rather than discovering it
  afterwards.
- **The notice is partly information.** A deficiency notice does tell you
  something bad. The claim is that the forced component overshoots, not that
  the news is meaningless. If the overshoot is small, this fails.

---

## ③ 🔑 The selection rule conditions on a filing, never on a price

This is the one design decision that matters most, and it comes directly from
spec #3's autopsy.

Spec #3 selected on `CAR[A, A+1] >= +5.0%` and returned **t = −4.04**: buying
after a large positive move bought mean reversion. Its own declaration had
named that bias in advance, and the bias turned out to be larger than the
effect it was a caveat about.

**There is no price condition anywhere in this specification.** An Item 3.01
filing either exists in the window or it does not. There is no threshold to
tune, nothing to drift toward a nicer N, and the reversal failure mode cannot
recur because nothing is selected on a return.

`validate_extract_8k_deficiency.py` asserts this structurally — it inspects
the extractor's source and fails if any CAR, surprise-window or return
threshold reappears, and it checks that `qualifies()` takes the filing's items
field *and nothing else*, so the selection function cannot see a price even in
principle.

---

## ④ The measurement: a long book hedged with IWM

Specs #1–#3 averaged independent event outcomes. This measures a **calendar-
time portfolio**, because that is the tradeable object: nobody holds one event
at a time.

```
for each trading day t:
    BOOK_t = every name whose Item 3.01 landed in (t-63, t]
    r_t    = mean(BOOK_t daily returns) - IWM daily return
```

Equal weighted. Fewer than **5** names and the day is **flat, not dropped** —
dropping thin days would silently select the periods when deficiency notices
cluster, which is the market stress this specification is supposed to be
measured through, not around.

### Why IWM, and not a short book of individual names

`docs/V27_SPEC4_SKETCH.md` proposed making a **long-short** series the
pass/fail criterion. For this event class that is the wrong bar, and the
reason is specific rather than general:

> **Deficiency names are the least borrowable segment of an already
> hard-to-borrow universe**, because they are precisely what everybody wants
> to short. Putting the criterion on a leg that is close to fictional at
> $50k–$5M ADV would mean spending an irreversible slot on a result that
> cannot be implemented.

Hedging with IWM gets the variance reduction the sketch wanted — the market
factor leaves the **dispersion**, not merely the mean — using an instrument
that is liquid, borrowable, and already the benchmark `qt/calendar_time.py`
measures against.

The long-short series against individual names is still **computed and
recorded**, as a diagnostic that says whether the mechanism is real. It is
never the criterion.

---

## ⑤ Pre-registered parameters

Every one of these is fixed before the read. Changing any is a **new
specification** and a different slot.

| | |
|---|---|
| universe | `data/universe/v29_universe_pit.csv` — frozen, point-in-time, ADV $50k–$5M, price ≥ $2 |
| | 152,006 name-months · 3,259 tickers · 2015-09-30 .. 2025-08-31 |
| membership | as of the month-end **on or before** the announcement session |
| event | 8-K **Item 3.01**, from the SEC submissions API `items` field |
| announcement session A | first session that can react to the acceptance timestamp (ET, close 16:00) |
| entry | **A+1** — first session strictly after the news was actionable |
| horizon | **63 trading days** |
| weighting | equal, within the book |
| minimum book | **5 names**; below that the day is flat |
| benchmark / hedge | **IWM** |
| round-trip cost | **50 bp**, charged on turnover |
| delisting assumption | **−30%** base case (see §⑥) |
| standard error | Newey–West, **lag 10** |
| reality check | WRC **and** SPA, `p < α/K_declared = 0.10/5 = 0.02`, unread slots charged |
| terminal date | 2027-03-31 (unchanged) |

---

## ⑥ ⚠️ The delisting assumption, which this event class cannot avoid

Every previous v27 read could treat delisting as a footnote. Spec #3's was
4 of 1,025 events — 0.4% — and excluding them moved the mean by +0.03%.

**This specification selects companies in breach of a listing rule.** A high
delisting share is the expected outcome, not a defect in the sample, and the
assumed return after a name stops trading will move the headline number
materially. It therefore has to be a declared number, not a default nobody
chose.

| | assumption | rationale |
|---|---|---|
| **base case** | **−30%** | the name is held to the last traded price, then marked down for the move to the pink sheets. Pessimistic against the hypothesis, which is the right direction for a number you are choosing yourself |
| sensitivity A | 0% | `qt.calendar_time.portfolio_daily_returns` default — holds flat after delisting. Optimistic |
| sensitivity B | −100% | total loss. Harsher than reality, since delisted names keep trading over the counter |

**Criterion 8 requires the verdict to be the same under all three.** A result
that exists only at 0% is an artifact of the most flattering available
assumption, and this is the specification most exposed to that.

### The cost assumption

**50 bp round trip**, and it is a judgment call made in advance. Spreads at
$50k–$5M ADV are routinely 50–150 bp, so 50 is already optimistic; specs #1–#3
were silent on costs entirely, which in this band is not conservatism but an
omission. The series is measured **net**. A gross edge that 50 bp erases is
not a result.

---

## ⑦ The criteria

All eight are fixed now. **None is renegotiable after the read** — S1, S2, S3,
E1 and all five reads to date failed without a single threshold being moved,
and that record is worth more than any model in this repository.

1. **≥ 750 trading days** carrying a book of at least 5 names
2. annualised **net** excess over IWM **> 0**
3. **t ≥ 2.0** on the mean daily net excess, Newey–West lag 10
4. **information ratio ≥ 0.5**
5. **maximum drawdown no worse than IWM's** over the same window
6. **stability** — final-third excess ≥ 50% of first-third; **undefined if the first third is ≤ 0, which is a MISS**
7. **WRC and SPA** at `p < 0.02`, unread slots charged
8. **the verdict is unchanged** under all three delisting assumptions in §⑥

### Reported, and deliberately not a bar

- the long-short series against individual names (§④)
- **5th-percentile event outcome** and the full delisting share — the mean is
  not the whole story, and a thin left tail can be unsurvivable at a positive
  average. `form4_cluster_buy_v2` recorded the mirror of this about its short
  side; the long side of a distressed book has the same problem pointing the
  other way
- book size distribution and the share of days below the 5-name floor

**Criterion 6 is the one to watch.** First-third excess ≤ 0 has now killed
three reads across two programmes and two unrelated mechanisms. If it happens
a fourth time with a mechanism this different, that is a finding about the
measurement or the sample, not about the hypothesis — and it is worth more
than the read itself.

---

## ⑧ What has to happen before this is declared

Each step costs nothing. None of them touches K.

1. ✅ **Extractor built** — `extract_8k_deficiency.py`
2. ✅ **Tests built** — `validate_extract_8k_deficiency.py`, including the
   structural assertion that no price threshold exists
3. ⬜ **Build the event file** — dispatch `run_extract`. Uploaded as an
   artifact, not committed
4. ⬜ **Availability rehearsal** — `rehearse_deficiency_availability.py`,
   COUNT ONLY. This answers the one question that cannot be settled from a
   desk: how many events exist, and how many days carry a book
5. ⬜ **Record the fingerprint** — rows, sha256, window, in the declaration
6. ⬜ **Operator signs a prior**, as a number, before any return exists
7. ⬜ **Declare** — this is the step that commits a slot

**If step 4 comes back thin, nothing has been spent.** The slot is still
there, and tax-loss reversal remains available as a fallback event class —
though it carries its own hard ceiling, since a seasonal effect makes the
*year* the unit of independence and ten years is ten observations.

---

## ⑨ What a pass would and would not establish

**Would:** that forced selling around listing-deficiency notices in
micro-liquidity names produced a positive, cost-adjusted, multiplicity-
corrected excess over IWM across 2015–2025, robust to how delisted names are
marked.

**Would not:** that it still does. A ten-year backtest is not a forward
result, and this would be a *first* finding, not a validated strategy. The
honest next step after a pass is paper trading with the book live and small,
not capital.

**And a pass is not the base case.** Five reads, no finding; 305 v25 models,
no finding. The prior should be set in that light, and it should be set before
the numbers exist rather than after.
