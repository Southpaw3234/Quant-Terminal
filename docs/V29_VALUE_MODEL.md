# V29 — a systematic value model: design, and the decisions it forces

**Status:** ⚪ **DESIGN. Nothing declared, nothing measured, K untouched at 2/5.**

The goal, in the operator's words: *find stocks that are undervalued and good investments,
build a portfolio around them, maximise profits.*

That is a legitimate and buildable research programme. It is also the single most
degrees-of-freedom-rich thing in quantitative finance, and this document exists to make that
visible before anything is measured rather than after.

---

## ① The honest problem with what was asked

"Find undervalued stocks" does not name a hypothesis. It names a family of them.

Earnings yield, book to price, free-cash-flow yield, EBIT to enterprise value, sales to price.
Each raw, or winsorised, or sector-neutral, or size-adjusted. Combined by rank, by z-score, by
equal weight, by regression weight. Screened at the top decile, quintile, or fifty names.
Rebalanced monthly, quarterly, annually. Held equal-weight or value-weight. With a quality
overlay, or without.

That is comfortably **ten thousand defensible strategies**. Perhaps two hundred of them will show
a backtested Sharpe above 1.0 on any three-year sample **whether or not any of them works**, purely
because two hundred is what you get when you try ten thousand things against noise.

🔑 **This is the exact failure the v27 apparatus was built to prevent, and it is far more dangerous
here than in an event study.** Specs #1 to #3 had one parameter family each and still needed a
referee. A value model without a pre-registration is not a research programme; it is a machine for
producing a beautiful equity curve that does not repeat.

**So the first thing this needs is not code. It is a signed specification, before a single
backtest.**

## ② How this addresses the three problems already identified

**Features carried no signal.** The v25 frame used price and volume transforms — momentum,
volatility, technical shapes. Fundamentals are a genuinely different input class: they are not
functions of the price series, so they are not another rearrangement of the same information. The
point-in-time layer (`extract_fundamentals.py`) is built and keys every value on the SEC `filed`
date rather than the fiscal period end, which is the difference between a value screen and a
machine that knows next quarter's earnings three months early.

**The space is picked over.** Value is the *most* published anomaly of all — Fama and French
formalised it in 1992 and it has been traded ever since. Nothing rescues a plain large-cap value
screen. What is genuinely less arbitraged is value **in names institutions cannot fit into**, and
that is a structural edge a $10,000 account has and a $10,000,000 fund does not.

⚠️ **The current universe screen throws that edge away.** `ADV_MIN` is $250,000/day. With a
$10,000 allocation across twenty positions, a position is **$500**; at five percent participation
that needs **$10,000** of daily volume, not $250,000. The floor is twenty-five times higher than
the capital requires, and it excludes precisely the names where competition is thinnest. Lowering
it is free, and it is the sharpest single change available.

**The bar is strict and modest edges fail.** Two fixes, neither of which loosens the bar. First,
**extend the window**: three years is a legacy of Finnhub's insider history, while XBRL facts run
to 2011 and prices far further. Ten years roughly triples the sample and raises the t-statistic by
about 1.7 for the same effect. Second, measure as a **calendar-time long-short portfolio**
(`docs/V27_SPEC4_SKETCH.md`), which removes the market factor from the variance instead of
subtracting it from the mean.

## ③ The shape of the specification

| component | what must be fixed before any read |
|---|---|
| universe | point-in-time membership, ADV floor **to be set by capital, not habit** |
| window | proposed 10 years, 2015-2025, versus the current 3 |
| value metric | **ONE primary**, pre-declared. Not a menu tried in turn |
| quality gate | optional and pre-declared: profitable, not heavily levered, shares not ballooning |
| ranking | cross-sectional, sector-neutral or not — declared |
| portfolio | N names, weighting, rebalance frequency — all declared |
| costs | a round-trip assumption in basis points, applied **inside** the criterion |
| benchmark | a small-cap index, not SPY: beating SPY with small caps is not evidence of skill |
| objective | see §④ |

**A recommendation, so this is a decision and not an essay.** One primary metric with decades of
out-of-sample evidence behind it, one quality gate, no optimisation:

> Rank the point-in-time universe by **EBIT / enterprise value**. Require positive operating
> income and a debt-to-equity below some declared bound. Hold the top **20** names, equal weight,
> rebalanced **quarterly**. Long only. Measured against a small-cap benchmark, net of a declared
> round-trip cost.

Every one of those numbers is arguable. That is the point: argue them **now**, write them down,
and then never touch them again.

## ④ "Maximise profits" is not yet an objective

Three different systems come out of three readings of it, and they conflict:

- **Highest expected return.** Concentrated, high turnover, deep drawdowns. Maximised by taking
  more risk, which is not skill.
- **Highest risk-adjusted return.** Sharpe or similar. Diversified, moderate turnover. The
  standard research objective.
- **Highest return subject to a drawdown limit.** The one most people actually mean when they say
  the first, and the only one that survives contact with a real account.

This must be chosen before measurement, because the same backtest ranks strategies differently
under each.

## ⑤ Budget: this needs its own pre-registration

`docs/V27_PREREGISTRATION.md` declares K=5 for **event-study specifications**, and 2 are spent. A
portfolio model is not an event study, and quietly spending v27's remaining slots on it would blur
two programmes with different criteria.

The clean options are to open a **V29 pre-registration** with its own K, its own criteria and its
own terminal date, or to declare the value model **inside** v27 and spend a slot. The first is
cleaner. Either way the anti-deferral rule and the referee apply, or none of this is worth doing.

## ⑥ Honest prior

Value and quality in illiquid small caps is one of the better-evidenced corners of the literature
and one of the least reachable by large capital. It is also decades old, widely known, and
implemented by many.

My estimate that a pre-registered version clears a properly corrected bar on this data is roughly
**20–25%** — above the 15% v27 prior, because the mechanism is structural rather than
informational and because the size band is genuinely hard for institutions to trade. It is not a
high number and it should not be. Anyone quoting a higher one for a strategy built from free data
is not measuring honestly.

Two more things that are true and unwelcome. Value strategies underperform for **years** at a
stretch, so a three-year read can be negative while the strategy is sound, and positive while it
is not. And a 20-name portfolio of illiquid small caps carries real single-name risk that a
backtest reports as volatility and a live account experiences as something worse.

## ⑦ What is built, and what comes next

Built and tested here: **`extract_fundamentals.py`**, the point-in-time fundamentals layer. It
computes no ratio and selects no stock, deliberately, because those are specification choices.

Next, in order, and none of it spends budget until the last:

1. Widen the universe: **lower the ADV floor to match the capital**, extend the window.
2. Sweep fundamentals over the widened universe.
3. Write and **sign** the specification above, with the four §③ decisions filled in.
4. Build the calendar-time portfolio measurement.
5. Read it once.
