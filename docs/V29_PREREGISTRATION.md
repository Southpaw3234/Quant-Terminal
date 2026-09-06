# V29 pre-registration — systematic value, risk-tiered

**Status:** ✅ **SIGNED 2026-09-06.** Parameters, criteria and prior are frozen.
Separate from `docs/V27_PREREGISTRATION.md`. v27's K=5 covers **event studies** and 2 are spent;
spending its remainder on a portfolio model would blur two programmes with different criteria.

| | |
|---|---|
| budget | **K = 3** reads |
| terminal date | **2027-06-30** |
| registry | `data/registry/v29_specifications.json` |
| referee | `qt.referee`, unchanged — declaration is free, a READ spends a slot |
| anti-deferral | a read specification cannot be re-read; no bug found afterwards reopens it |

K is deliberately smaller than v27's. The reality-check correction charges every declared slot,
read or not, so three shots face a materially easier bar than five. Fewer, better-motivated
specifications is the cheapest power available.

---

## ① Objective

The operator's words: *maximise profits with minimal risk.* That resolves to **risk-adjusted
return**, and it is fixed here because the same backtest ranks strategies differently under each
reading of the phrase.

The criterion is therefore excess return **per unit of risk**, not excess return, and it carries an
explicit drawdown condition so that "minimal risk" is a bar the strategy must clear rather than an
adjective attached to it afterwards.

## ② The specification — every parameter frozen

| component | value |
|---|---|
| universe | point-in-time monthly membership, screened as of each date |
| ADV floor | **$50,000/day** (was $250,000) |
| ADV ceiling | $5,000,000/day, unchanged |
| price floor | $2.00, unchanged |
| history minimum | 200 bars, unchanged |
| window | **2015-09-01 to 2025-09-01**, ten years |
| fundamentals | SEC XBRL, keyed on `filed`, flows from annual filings |
| **primary value metric** | **EBIT / enterprise value**, one metric, no alternatives |
| enterprise value | market cap + long-term debt − cash |
| quality gate | operating income > 0, book equity > 0, debt/equity ≤ 2.0 |
| risk components | volatility, max drawdown, leverage, illiquidity, thin equity |
| risk composite | equal-weighted mean of cross-sectional percentiles, ≥3 components required |
| tiers | terciles of the day's scored set: low / moderate / high |
| **primary tier** | **moderate** |
| holdings | **20 names, equal weight, long only** |
| rebalance | **quarterly**, at month-end, entering the following session |
| costs | **75 bps round trip**, applied to turnover, **inside** the criterion |
| benchmark | **IWM**, one benchmark |
| measurement | calendar-time daily return series, Newey–West lag 10 |

**Why the ADV floor moves.** With $10,000 across 20 positions a position is $500. At five percent
participation that needs $10,000 of daily volume. The old floor of $250,000 was twenty-five times
what the capital requires and excluded exactly the names large capital cannot reach. $50,000 keeps
fivefold headroom over the arithmetic minimum and still sits far below institutional reach.

## ③ The risk knob, and the multiple-testing it creates

Three tiers are **three strategies**. Reporting whichever performed best would be the purest form
of the selection this document exists to prevent.

- **Moderate is PRIMARY** and is the only tier that can produce a pass.
- Low and high are reported as **description**. They cannot pass and cannot be substituted.
- If the operator later wants low or high to count, that is a **new declaration spending another
  slot**, not a reinterpretation of this one.

The five risk components are declared above and are **not fitted**. Equal weights are a choice made
before any data was seen, and the alternative — fitting weights — would manufacture a risk score
that looks calibrated on this sample and nowhere else.

## ④ Criteria — all must hold, on the MODERATE tier, net of costs

1. **N ≥ 1,000 trading days** with a live book.
2. **≥ 60 eligible names per tier** at the median rebalance. Below that, 20 holdings is not a
   selection and the specification **MISSES** — it is not quietly resized.
3. **Annualised excess return over IWM > 0.**
4. **t ≥ 2.0** on the mean daily excess return, Newey–West lag 10.
5. **Information ratio ≥ 0.5.**
6. **Maximum drawdown no worse than IWM's over the same window.** This is where "minimal risk"
   becomes a bar rather than a hope.
7. **Stability**: final-third excess return ≥ 50% of first-third. Undefined if the first third is
   ≤ 0, which is a MISS.
8. **White's Reality Check / Hansen's SPA at p < α/K_declared = 0.0167**, unread slots charged.

## ⑤ Honest prior

**20–25%**, accepted by the operator on 2026-09-05 against the range proposed. Midpoint **22.5%**.

Above v27's 15% because the mechanism is structural rather than informational: value in names too
small for institutional capital is not arbitraged away by publication, it is left alone because it
cannot be traded at size. Not higher, because value is the most-published anomaly in finance and
this is built entirely from data anyone can obtain free.

**Two things that are true and unwelcome.** Value underperforms for years at a stretch, so a
ten-year read can be negative while the strategy is sound and positive while it is not. And twenty
illiquid small caps carry single-name risk that a backtest reports as volatility and a live account
experiences as something considerably worse.

## ⑥ Known limitations, declared before the read

- **Survivorship is corrected only from 2023.** The delisted roster covers 2023-2026; the declared
  window starts 2015. Form 25 filings reach further back and the roster can be extended, but until
  it is, the first eight years of this window carry the same upward bias every v27 read carried.
  **This is the largest known weakness in the specification.**
- **Long-term debt only.** XBRL supplies it reliably; short-term borrowings are missing, which
  understates enterprise value and overstates EBIT/EV for revolver-funded companies.
- **Annual flows.** Fundamentals are up to fifteen months stale by construction. Conservative, and
  it costs responsiveness.
- **Costs are an assumption, not a measurement.** 75 bps is a judgement about a band where real
  spreads vary enormously.
- **No short leg**, so no market hedge. Excess return over IWM is the only adjustment.

## ⑦ Signature

The prior, the parameters and the criteria above are frozen on signature. After it, changing any of
them is a **new specification consuming another slot**, and `qt.referee.declare` refuses to
redefine one silently.

```
Prior accepted:  20-25% (midpoint 22.5%)
Operator:        Southpaw3234            Date: 2026-09-06
```

**SIGNED 2026-09-06.** The operator countersigned the recorded 20-25% range, midpoint 22.5%,
before any ratio was computed and before the backtest existed. Every parameter in section 2 and
every criterion in section 4 is frozen from this point. Changing one is a new specification
consuming another of the three slots.
