# The trading / investing fork — SETTLED

**Decided:** 2026-09-04
**Decision:** **long-horizon systematic.** Same event-study machinery, horizon
moved out of the days-scale cell toward the one `NEXT_ARCHITECTURE.md` §②
describes as less competed. The autonomous-execution goal is retained; the
instrument panel of §③ is **not** what is being built.

---

## ① Why the fork existed

`NEXT_ARCHITECTURE.md` §② draws a hard line:

| | trading | investing |
|---|---|---|
| horizon | days | years |
| competition | brutal — **measured**, not assumed | far thinner for a patient individual |
| is it a model problem? | yes | **mostly not** |

⚠️ **v27 as built was on the TRADING side of that line and nobody had said so.**
Spec #1 used a **21-trading-day** horizon — one month. That is slower than v25's
five days and nowhere near "years". The project had been described as investing
while being measured as trading.

## ② The mechanical argument for a longer horizon

E1 spec #1 failed because **`sd = 19.22%` swamped a `+1.76%` mean** — not
because the effect was small. For an effect that accrues over the holding
period, mean scales roughly **linearly** with horizon while dispersion scales
with its **square root**, so `t` improves with `√horizon`.

That makes horizon the one lever which attacks the actual binding constraint
*and* moves toward the thinner-competition cell. Extending the **lookback**
(the original spec #2 draft) only buys sample size; it does nothing about
dispersion.

## ③ How long — measured, not chosen

Against the frozen 205-event set spanning `2025-09-04 → 2026-08-31`:

| horizon | settled events |
|---|---|
| 21d (month) | 193 |
| **63d (quarter)** | **170** |
| 252d (year) | **0** |

🔑 **A one-year horizon has ZERO settled observations on one year of event
history.** It is not a judgement call — it is arithmetically unavailable. The
longest horizon feasible on existing data is **one quarter (63 bars)**, and even
that surrenders sample to the settlement rule.

**Decision: horizon = 63 trading bars.** Anything longer requires buying
history first, and would still be measuring a handful of events.

## ④ 🔴 The constraint this decision makes binding

**Survivorship and horizon multiply each other, and the product is now the
worst problem in the project.**

At 21 days, a name delisting mid-hold is rare. At 63 days it is materially more
common, and in *this* universe — small, illiquid, uncovered — it is common
outright. **A delisting is usually a near-total loss, and every one of them is
absent from the universe**, because the symbol source lists survivors only.

So the longer the horizon, the larger the share of missing mass that is
specifically the bad outcomes. **Lengthening the horizon improves the t-stat and
worsens the bias at the same time, in the same direction as a false pass.**

⚠️ This is no longer an engineering problem. `build_universe.py` cannot fix it
on Finnhub's free tier, and `qt/prices.py` explicitly cannot either — as-of
computation cannot resurrect a name that is not in the source. **It is a
data-purchase problem: a point-in-time constituent source with delisted names.**

**Until that exists, any long-horizon E1 pass must be reported with the bias
stated beside the number, and treated as an upper bound rather than an
estimate.**

## ⑤ What this changes about spec #2

The draft in `V27_SPEC2_PROPOSAL.md` proposed one change — lookback 365 → 1,095
days. Under this decision spec #2 becomes:

```
horizon    21 bars  ->  63 bars          (the fork decision)
lookback   365 days ->  1,095 days       (power; and see below)
```

**Two parameters, still ONE specification.** A specification is the whole
configuration, not a single knob, so the K cost is unchanged.

⚠️ **But attributability is weakened, and that is a real cost.** If spec #2
fails, it will not say whether the horizon or the sample was wrong. The
alternative — one parameter per specification — would spend two of the four
remaining on a single question, which is worse. Recorded so the ambiguity is a
known price rather than a later surprise.

**Rough power, if the effect is real and scales as assumed:**

| configuration | N (est.) | mean | sd | t (est.) |
|---|---|---|---|---|
| spec #1 as read | 160 | +1.76% | 19.2% | 1.16 |
| 63d, 1yr lookback | ~110 | ~+5.3% | ~33% | ~1.7 |
| 63d, 3yr lookback | ~330 | ~+5.3% | ~33% | ~2.9 |

These are **projections under an assumption that may be false** — that the
effect accrues linearly. If spec #1's +1.76% was a right-skewed noise draw,
tripling the horizon multiplies noise, not signal, and the t-stat does not move.

## ⑥ Blocking checks — free, and they do not touch K

1. **Does Finnhub return 1,095 days of insider history on this key?** The
   09-01 probe requested 365 only. If the tier caps below three years, the
   power argument collapses and the specification must be revised **before**
   declaration.
2. **How many events, and how many survive de-duplication at 63 bars?** The
   overlap window triples, so same-ticker merging will cut harder than the
   205 → 160 seen at 21 bars. If the survivor count lands near 40, the
   specification is not worth spending.

Both are **availability** questions. Neither computes a return.

## ⑦ What is NOT being built

`NEXT_ARCHITECTURE.md` §③'s instrument panel — filing watcher, filing diff,
constraint screen, base-rate lookup, thesis ledger. It remains the alternative
if the systematic path exhausts K without a pass, and §③'s argument that *"nobody
has arbitraged away reading the 10-K carefully"* is not withdrawn by this
decision. It is deferred, not refuted.
