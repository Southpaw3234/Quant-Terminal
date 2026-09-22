# Carry Forward — what survives the sunset

**Written:** 2026-09-21
**Status:** ✅ **INVENTORY — decided content, open destination.** What to keep is settled below. Where it goes is not; see §⑥.
**Context:** the operator called the terminal verdict on 2026-09-21 — *"as a whole, this model has failed"* — and fixed the cron sunset at **2026-09-29** (an extension to 10/02 was offered and declined). This document is the asset inventory for that closure. It is **not** a proposal for a successor strategy and contains none.

> This supersedes `docs/NEXT_ARCHITECTURE.md` §④ ("What carries over unchanged"), which
> listed portable *patterns* on 2026-08-27. In the two weeks after that was written, most
> of those patterns were **actually built** as the `qt/` package. The answer changed from
> "remember these principles" to "here is a tested library — keep it."

---

## ① The finding: the carry-forward is code, not lessons

`qt/` is **12 modules, ~107KB, pure functions**, with **nine validation suites, all green** — last full run [`34177611690`](https://github.com/Southpaw3234/Quant-Terminal/actions/runs/34177611690) (2026-09-08, 30s, `ALL CHECKS PASSED` on every suite).

🔑 **`qt/` was never wired into the live pipeline.** That was a deliberate Phase-0 scope decision (`qt/__init__.py`: *"NOTHING HERE IS WIRED INTO THE LIVE PIPELINE"*), and it is now the reason the package survives the sunset **completely intact**. It imports nothing from `quant_runner.py`, owns no cron, and runs only on push.

✅ **Nothing in this inventory is at risk on 2026-09-29.** There is no deadline on any of it. The crons retire; `qt/` does not notice.

---

## ② Tier 1 — carry as-is

### The universal core

No market assumptions anywhere in these. They would be equally correct in a project with nothing to do with finance.

| module | what it is | why it carries |
|---|---|---|
| `ledger.py` | append-only frozen ledgers | **The single best artifact in the repo.** Any process that rebuilds history from a refetched source needs this, or written rows silently move. Already consolidated two divergent copies of `_freeze_first_write` (`analyze_rank_ic.py` keyed on `date`, `event_study.py` keyed on `event_id`) that had drifted apart in logging and error handling. |
| `referee.py` | pre-registration, K budget, veto | The rarest thing built here. Makes *proposer ≠ judge* structural rather than a promise. v25's discipline held four times by character; this makes it hold by construction. |
| `wrc.py` | White's Reality Check + Hansen's SPA | Required the moment more than one look is taken at anything. Before this module existed, the WRC clause was printed as a NOTE and never evaluated — a pass could not have been called on the day it happened. |
| `measurement.py` | the "only role permitted to compute a return" seam | The role separation is the value, not the arithmetic. Data never sees returns; Research proposes but cannot measure; Referee authorises but does not compute. |
| `calendar_time.py` | Newey-West SE, IR, max drawdown, stability | Correct handling of an autocorrelated series with a declared lag. Easy to get wrong in a way that silently overstates `t`. |
| `roster_update.py` | watermarked, append-only incremental sweep | Generalizes to any incremental collection: ask *"what is new since last time"*, never rebuild. Turns a 25-minute EDGAR sweep into seconds. |

### ⚠️ One gap to fix before the referee is reused

`Referee.authorize_read` (`qt/referee.py:161`) tests **four** things — declared, not already read, declaration precedes read, read precedes terminal date. **The signed prior is not one of them.** This was identified on 2026-09-08 as the reason a read dispatched without a written prior is a real protocol breach that no code will stop. If the referee carries forward, **this is the first patch**, before any use.

### Market-shaped — carry only if something touches prices again

* `prices.py` — as-of semantics and back-adjustment correctness, stated precisely rather than carried as folklore (returns are safe under back-adjustment; level-based screens are not).
* `liquidity.py` — Corwin-Schultz spread from daily bars. Hard-won: the first attempt quoted the live market on a Sunday and returned a 3,336bp median, identical across a hundredfold range of liquidity. A broken measurement, not a finding.
* `execution.py` — target portfolio to order list, pure planning, no broker.
* `reconcile.py` — implementation shortfall, completion rate, backtest-vs-live divergence.
* `guards.py` — the fail-closed guard stack.

### Model-specific

* `scoring.py` — the V29 value model (EBIT/EV, quality gate, risk tiers). **Carries only if V29 is alive.** See §⑥.

---

## ③ Tier 2 — carry the pattern, rebuild the wiring

These are not worth porting line by line, but every one was paid for with an incident and must not be rediscovered:

* **Matched sunset gates across workflows.** The pattern is not the gate — it is the rule that every copy must stay matched (`quant_daily.yml`, `morning_watchdog.yml`, `universe_watch.yml`). An unmatched watchdog pages forever over an expected absence.
* 🔑 **An alarm must not sit behind a permanently-red gate.** This cost two silent failures — the 8/21 broken retrain and the 8/25 evidence wipe — both hidden under HON's permanent red. The `Predictions ledger guard`, placed *after* the flat gate so it reports from underneath a failure, is the working example. **Write this rule down first in any successor.**
* **Fail-closed by default.** A guard that cannot determine whether an action is safe must refuse it. Learned when a bare `except: pass` was silently *disabling* the consecutive-loss brake rather than tripping it (`dc04017`, then `a0dd471`).
* **Append-only plus shrink detection on every scheduled writer.** Four separate incidents were crons rewriting a file they should only have extended: catch-up dispatch, wake-triggered tasks (×2), and a stale-base checkout that erased 921 rows before anyone noticed.
* **Preflight/validate as a push gate**, not something to remember to run.
* **Discord webhook paging independent of anyone opening the app.**

---

## ④ Tier 3 — the disciplines

The genuine output of the retired project. Not code, and the part that actually transfers:

* **Pre-registration with dated terminal criteria and an anti-deferral clause** — a bug found after a read does not reset the clock.
* **State the prior before writing code.** The last attempt was recorded at ≈10% combined, in advance, and came in consistent with that prior.
* **The `HANDOFF.md` ledger** — dated, numbered, with retractions recorded rather than edited away.
* **Honoring the stop.** S1, S2, S3 and E1 all failed and not one threshold was renegotiated. That is rarer than any model.

---

## ⑤ Leave behind — explicitly

| artifact | size | why |
|---|---|---|
| `quant_runner.py` | 404KB / 7,538 lines | Self-rewriting `[src rewrite]` cells; `SECTOR_MAP` inside a string literal, so `ast` cannot parse the file. **This is why the incident class was possible at all.** Do not port it — `qt/` already replaced what was worth keeping. |
| `trading_model_v*.ipynb` (×8) | ~2.75MB | Archival record only. |
| `model_intraday.py`, `stat_arb.py`, `shadow_stat_arb.py`, `analyze_rank_ic.py`, `analyze_shadow_intraday.py`, `analyze_stat_arb.py` | — | Frame-specific measurement for retired frames. |
| the 280-name `WATCHLIST` | — | Measured and closed. Includes nine chronically-stale tickers (`ANSS AVB EA EQR HOLX IPG K MMC SQ`). |
| every `probe_*.py` and one-off remediation workflow | — | They answered their question. History, not tools. |

❌ **Do not carry the 279-name large-cap / 5-day-horizon / daily-rebalance cell in any form.** It is measured, and the measurement is the point.

---

## ⑥ Open — not decided here

1. **Does `scoring.py` come along?** Equivalently: is V29 alive or closed? v27 stands at K **2/5** and V29 at K **0/3**, nothing read, unchanged since 2026-09-08. Both carry protocol debts (the signed prior; spec #3's choice between the frozen 1,869-row event file and the 1,294-row point-in-time one).
2. **Is the next project markets at all?** If not, Tier 1 shrinks to the four universal modules — `ledger`, `referee`, `wrc`, `measurement` (plus `calendar_time` wherever a daily series is evaluated) — and everything market-shaped stays in this repo.

---

## ⑦ Proposed first action

**Lift `qt/` and its nine validate suites into their own repository, before the sunset.**

Not because anything is at risk — §① establishes it is not — but because the carry-forward is currently entangled with a retired trading system: same repo, same root directory, surrounded by probes and archival notebooks. Extracted, it is a small tested library with a clear purpose. Left in place, every future use begins by explaining what Quant-Terminal was.

Mechanical, reversible, requires no decision about what comes after, and leaves this repository as a clean archive of a properly closed experiment.

---

## ⑧ The protocol layer — how Tiers 1–3 are actually executed

### The gap this closes

§②–④ as first written described *what* to keep. They did not say how any of it is
constructed or enforced, and that asymmetry was the whole problem:

| tier | form | enforced by | can it rot silently? |
|---|---|---|---|
| 1 | code | nine CI suites, green on every push | **No** |
| 2 | prose in `docs/` | *nothing* | **Yes** |
| 3 | prose in `docs/` | *nothing* | **Yes** |

🔑 **The argument against leaving Tiers 2 and 3 as prose is one this project already
made.** `qt/referee.py` exists because a rule holds only while the person bound by it is
disinterested — *"the proposer and the judge were the same person, and that arrangement
is only safe while that person is disinterested."* Tiers 2 and 3 were in precisely the
form the referee was built to replace. Same failure mode, one level up.

⚠️ **And they would not have survived §⑦.** Extracting `qt/` carries Tier 1 — it is the
files. Tier 2 and Tier 3 would have stayed behind in `docs/` in the archive. Day one of
any successor would have had the code and none of the discipline: exactly backwards.

### The two artifacts

**`validate_protocols.py`** — the executable half. Stdlib only (no pandas, numpy or
yaml) so it is the *first* file copied into a successor repo, before dependencies exist.
Six groups, spanning all three tiers:

| group | tier | what it pins |
|---|---|---|
| `test_tier1_purity` | 1 | declared-pure modules import nothing network-shaped |
| `test_tier1_no_silent_except` | 1 | no `except: pass` anywhere in `qt/` |
| `test_tier2_matched_constants` | 2 | a constant shared across workflows has ONE value |
| `test_tier2_alarm_placement` | 2 | every alarm carries `if: always()` |
| `test_tier2_ledger_integrity` | 2 | declared ledgers have unique, ordered keys |
| `test_tier3_protocol_file` | 3 | `CLAUDE.md` exists and declares its rules |

Three design choices worth stating, because each encodes a lesson rather than a taste:

* **Skips are loud and are not passes.** A fresh repo has no workflows and no ledgers;
  those checks print `[SKIP]` and are counted separately. Nothing goes green by silence.
* **Exemptions are declared in code, with a reason and a date.** `EXEMPT` is the only way
  to silence a finding. An undeclared exemption is a quiet failure wearing a hat.
* **Tier 3's file check can never skip.** If `CLAUDE.md` is absent the suite fails
  outright: a discipline not written where it is read at decision time does not bind.

**`CLAUDE.md`** — the advisory half, at the repo root so it is read every session rather
than at kickoff. Written as **triggers, not values**: *"before writing code that produces
a number someone might act on, state the prior as a number, in the repo, first"* — never
*"we value stating priors."* Four sections: Tier 1 construction, Tier 2 wiring, Tier 3
research conduct, and the repo's operational traps.

### ⚠️ The honest limit

`CLAUDE.md` binds an agent well but is still prose — a strong standing instruction, not a
gate. **Anything that can be made executable belongs in `validate_protocols.py` instead**,
and the advisory file is deliberately kept short for that reason. Do not grow it with
things that could have been code. The genuinely binding rules are the ones that fail a
build; the rest is a nudge, and should be described as one.

### What the suite found immediately

One true positive, on its first pass: `quant_daily.yml`'s *"Verify evidence-clock
persistence (alarm only)"* step is gated on run type with **no `if: always()`** — an
earlier failure skips it. That is a real instance of the exact rule, in the exact
workflow whose permanent red hid two silent failures in 2026-08. It is **declared exempt
rather than fixed**, because the workflow retires at the sunset and a wind-down edit
would perturb the final S4 reads. The exemption names that reasoning and dates it, and
says plainly: do not copy this step into a successor.
