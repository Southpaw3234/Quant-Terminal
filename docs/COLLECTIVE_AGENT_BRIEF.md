# Build brief — the collective agent

**This document is a prompt.** Hand it to a fresh Claude Code session working
in the `Southpaw3234/Quant-Terminal` repository. It is written to be read
cold, by someone with no memory of how this repo got here.

Read §0 and §1 before writing any code. They are not background; they are the
constraints that make the rest of the design the right one.

---

## §0 What you are building

Four cooperating agents that give one operator a daily, honest picture of the
market they are investing in, and a disciplined way to decide what to do.

```
  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
  │  OBSERVER   │──▶│  LIBRARIAN  │──▶│  ACTUARY    │──▶│  CHIEF OF   │
  │             │   │             │   │             │   │   STAFF     │
  │ collect     │   │ filter and  │   │ base rates, │   │ daily brief,│
  │ filings,    │   │ structure   │   │ exposure,   │   │ risk stance,│
  │ prices,     │   │ what bears  │   │ what would  │   │ decisions   │
  │ macro, news │   │ on a thesis │   │ falsify it  │   │ logged      │
  └─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘
                                                               │
                                                        ┌──────▼──────┐
                                                        │  OPERATOR   │
                                                        │ decides.    │
                                                        │ always.     │
                                                        └─────────────┘
```

The operator sets a risk stance (conservative ↔ aggressive), reads the brief,
and makes every decision. **No agent places an order. Ever.**

---

## §1 🔑 The one correction to make before you start

The operator described the third agent as deciding *"the best recommended
course of action."* **Build it to report distributions and falsifiers, not a
recommendation.** This is the single most important instruction in this
document, and it is not a style preference — it is the finding this repository
cost a year to produce.

What happened here already:

| what was tried | result |
|---|---|
| 305 models, 279 large caps, 5-day horizon | walk-forward AUC **0.4942** — below a coin flip |
| deflated Sharpe across those models | **0 of 305** positive |
| White Reality Check / Hansen SPA on the live run | p = **0.486** / **0.948** |
| 84 days of real paper trading | Sharpe 0.749 vs bootstrap 95th pct **3.57** |
| 5 pre-registered hypotheses, thresholds fixed in advance | **5 × NOT MET** |

And the diagnosis, from `docs/NEXT_ARCHITECTURE.md` §①, which you should read
in full:

> Every one of these is event-level, structured, human-readable data. Every
> one is flattened into a float, averaged across 279 names, and fed to a
> 5-day classifier. A Form 4 reading *"the CFO bought $2M on the open market,
> first purchase in six years"* becomes `0.31`. **That is where the
> information went.** The output layer was wrong, not the input layer.

So: an agent that collapses rich evidence into a score and emits a verdict is
the thing that already failed, rebuilt with a friendlier interface. An agent
that *surfaces the evidence legibly and states what would prove a thesis
wrong* is not, and the efficiency prior does not touch it.

**ACTUARY reports: "of the 47 historical cases resembling this, the median
90-day outcome was −2%, the interquartile range was −14% to +9%, and 3 of 47
delisted." It never reports: "BUY."**

---

## §2 What already exists — do not rebuild it

Read these before writing anything. Reusing them is most of the work saved.

**Library — `qt/`, tested, each with a validator**
| module | what it does |
|---|---|
| `tickers.py` | ticker-safe pandas. **Read its docstring first.** |
| `panel.py` | frozen point-in-time universe + `eligible_at()` look-ahead guard |
| `prices.py` | `screen_as_of`, point-in-time eligibility, forward returns |
| `calendar_time.py` | portfolio returns, Newey–West, stability, drawdown |
| `wrc.py` | White Reality Check / Hansen SPA |
| `referee.py` | the read budget. **Agents must never import this.** |
| `ledger.py`, `guards.py`, `reconcile.py` | append-only evidence, fail-closed guards |

**Running infrastructure**
- `daily_snapshot.py` + `.github/workflows/daily_snapshot.yml` — gathers the
  eligible universe, benchmark closes and EDGAR filings each weekday 22:05
  UTC, publishes `docs/daily.json` to the orphan `data` branch.
- `docs/index.html` — the live dashboard (Netlify). Reads registries from
  `master` and `daily.json` from the `data` branch, client-side.
- `.github/workflows/research_bot.yml` — the pre-read pipeline pattern:
  `validate` → `build` → `rehearse` → `checklist`. **Copy this shape.**
- Secrets already configured: `SEC_USER_AGENT`, `FINNHUB_API_KEY`,
  `FRED_API_KEY`, `NEWS_API_KEY`, `DISCORD_WEBHOOK_URL`,
  `NETLIFY_BUILD_HOOK`, `ALPACA_*` (paper; **do not use**).

**Data**
- `data/universe/v29_universe_pit.csv` — frozen PIT panel, 3,259 tickers,
  152,006 name-months, 2015-09 .. 2025-08. ADV $50k–$5M, price ≥ $2.
- `data/registry/*.json` — the pre-registration record. **Read-only to every
  agent.**

---

## §3 The four agents, as contracts

Each is a scheduled GitHub Actions workflow plus a Python module. Each writes
one artifact with a schema. Each states its own freshness. None imports
`qt.referee`, `qt.wrc`, `qt.calendar_time`, or any broker SDK — and a
hermetic validator asserts that by parsing imports with `ast`, not by
grepping for words.

### 3.1 OBSERVER — collect

**Does:** pulls filings (8-K, 10-K/10-Q, Form 4, 424B5), prices, macro series
(FRED), and news headlines. Normalises and stamps everything with
`observed_at` and the source URL.

**Never:** scores anything, ranks anything, or drops a record because it
looks unimportant.

**Output:** `observations/YYYY-MM-DD.jsonl` — one record per observation,
append-only, each with `source`, `url`, `observed_at`, `entity`, `raw`.

**Notes that will save you a day:**
- `www.sec.gov` answers **403 from CI**. Use `efts.sec.gov/LATEST/search-index`
  (full-text search; accepts `forms` + dates with an **empty `q`**, which
  matters — any keyword silently biases a census into a sample) and
  `data.sec.gov` for submissions.
- EFTS indexes **documents, not filings**. Dedupe on accession number or you
  will triple-count.
- `display_names` is `'BIOVIE INC.  (BIVI, BIVIW)  (CIK 0001580149)'` — the
  last parenthetical is the CIK, not a ticker; one company can list several
  tickers. Form 4 carries **no ticker at all**.

### 3.2 LIBRARIAN — filter and structure

**Does:** decides which observations bear on an open thesis or a watched
name, and turns them into structured claims: *who, what, when, how much,
versus what baseline.* Diffs 10-K/10-Q risk factors and MD&A quarter over
quarter — **removed language is frequently more informative than added
language.**

**Never:** assigns a numeric score to a document.

**Output:** `briefs/YYYY-MM-DD.json` — claims, each linking back to the
observation ids that support it.

### 3.3 ACTUARY — base rates and exposure

**Does:** three things, all of them distributional.
1. **Base rate.** Given an event type, what has historically followed? Report
   the full distribution — median, quartiles, hit rate, worst case, and the
   count. Never a point forecast.
2. **Exposure.** Given the operator's current positions and stance, what is
   concentrated — sector, factor, single-name, liquidity.
3. **Falsifiers.** For each open thesis, what observation would make it
   wrong, and has any arrived.

**Never:** emits a buy, sell, hold, score, rating, target price, or
confidence percentage.

**Output:** `analysis/YYYY-MM-DD.json`.

🔑 **Report `n` beside every distribution, and report "insufficient history"
rather than a distribution computed from 4 cases.**

### 3.4 CHIEF OF STAFF — the operator's interface

**Does:** composes one daily brief. Takes the operator's stance from a config
file (`conservative | balanced | aggressive`) and uses it to set *what is
shown first and how much*, never to change a number. Records every decision
the operator makes into a **thesis ledger**: entry reasoning, falsifiers
stated in advance, review date. Append-only, freeze-on-write.

**Never:** places an order, moves money, or logs a decision the operator did
not make.

**Output:** the daily brief (Discord + the dashboard) and
`data/theses/ledger.jsonl`.

🔑 **The thesis ledger is the most valuable module here.** It is
pre-registration pointed at the operator's own judgment. This repository's
one durable, demonstrated achievement is that it honoured five pre-registered
thresholds when the data said stop, and renegotiated none of them. Most
people cannot do that with their own money. Build the ledger so it is easy to
state a falsifier in advance and impossible to quietly edit one afterwards.

---

## §4 Hard constraints

Violating any of these makes the system worse than nothing, because it will
be confidently wrong.

1. **❌ No autonomous trading.** No broker call, no order object, no
   `ALPACA_*` in scope. The panel informs; the operator acts.
2. **❌ No agent imports `qt.referee`, `qt.wrc` or `qt.calendar_time`.**
   Those spend pre-registered reads or evaluate criteria. Assert this with
   `ast`, in a test.
3. **❌ No backtest, AUC, rank-IC or Sharpe on the agent's own output.**
   It is not a predictor; scoring it like one recreates the trap.
4. **❌ No point forecasts and no composite scores.** Distributions with `n`,
   or nothing.
5. **❌ Never let a zero stand in for "cannot see."** If a field is
   structurally unmeasurable, emit `null` and say why. **A zero that cannot
   be non-zero is not a measurement** — this repo shipped that bug twice in
   one day and it retired an entire specification.
6. **❌ Never write to `data/registry/`, `data/predictions/`, `data/shadow/`,
   or any frozen ledger.** CI asserts `git diff --quiet -- data/` after every
   gathering job.
7. **❌ No backfilling a feed that was down.** A gap is evidence. Fill it and
   you have manufactured history.

---

## §5 Engineering invariants, each paid for in a real failure

- **A feed that stops must look stopped.** Every artifact carries
  `generated_at` and the session it describes. The dashboard computes *feed
  age* and *data age* separately and renders a **dark** block instead of
  numbers when either is stale. A previous dashboard froze when its producer
  was deleted and showed a 17-position book the account had not held for a
  month. *A record that can quietly stop being true is worse than no record.*
- **A green run is not a delivered artifact.** Assert the output exists, has
  the expected shape, and is fingerprinted — not merely that the job exited 0.
- **Fingerprint every input before counting anything.** sha256 with CRLF
  normalised, so a Windows checkout and a Linux runner agree.
- **Append-only, freeze-on-write, with shrink detection.** A ledger that got
  shorter is an incident, not a diff.
- **Fail closed.** A guard that cannot evaluate must block, not pass.
- **Structural guards beat textual ones.** Pin a function's signature with
  `inspect`; check imports with `ast`. Grepping source for banned words fails
  on the comment explaining the ban — that happened three separate times here
  in one day.
- **`NA` is a real ticker** (Nano Labs) and pandas eats it in at least two
  places. Use `qt/tickers.py` for every read and every DataFrame construction.

---

## §6 Build order

One module at a time. Each must be independently useful before the next
starts, and each lands as its own reviewed PR with a hermetic validator that
runs on push.

| # | module | done when |
|---|---|---|
| 1 | **OBSERVER — filings only** | a daily Discord post lists Form 4 cluster buys, first-purchase-in-N-years, and 8-K items for watched names, each linking to the filing |
| 2 | **CHIEF OF STAFF — thesis ledger** | the operator can open a thesis with falsifiers and a review date, and cannot silently edit one |
| 3 | **LIBRARIAN — filing diff** | risk-factor and MD&A changes quarter over quarter, removals highlighted |
| 4 | **ACTUARY — base rates** | any event type returns a distribution with `n`, or "insufficient history" |
| 5 | **CHIEF OF STAFF — daily brief** | one page, stance-aware ordering, on the dashboard and in Discord |
| 6 | **OBSERVER — macro + news** | FRED series and headlines, same freshness contract |
| 7 | **ACTUARY — exposure** | concentration by sector, factor, name, liquidity |

Start with **1**. It reuses a feed that already works, needs no model, cannot
lose money, and produces something readable on day one.

🔑 **The honest test, from `NEXT_ARCHITECTURE.md` §⑦: if after a few weeks
the operator does not find themselves actually reading the output, that is a
true and valuable finding.** Learn it for the price of one module rather than
the price of another architecture.

---

## §7 What success is, and what it is not

**Success:** the operator reaches a decision faster, with the relevant filing
in front of them, with the base rate anchoring their expectation, and with
their reasoning written down before the outcome is known.

**Not success:** the system producing recommendations that look authoritative.
That is the failure mode with the highest cost here, because it is
indistinguishable from the success case right up until money moves.

There is no claim anywhere in this system that it beats a benchmark. Five
pre-registered attempts to establish such a claim returned NOT MET, and the
record of that is published. If this system ever starts implying otherwise,
something has gone wrong in it.
