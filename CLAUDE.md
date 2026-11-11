# Operating protocols

**Read at the start of every session.** `docs/CARRY_FORWARD.md` §⑧ is the rationale;
this file is the operative version. Written as **triggers**, not values — a discipline
binds only if it fires at the moment of the decision, and a document read at kickoff
is not read at the moment of the decision.

⚠️ **This file is advisory where `validate_protocols.py` is not.** Anything that can be
made executable belongs in that suite instead. Treat a rule that lives only here as a
standing instruction that a careless session can still walk past — which is exactly why
the list below is short. Do not grow it with things that could have been code.

---

## 0. Status of this repository

🏁 **The trading programme is CLOSED.** Terminal verdict 2026-09-21: *"as a whole, this
model has failed."* Crons self-retire **2026-09-29**. The book is flat.

* **Do not propose model work** — no new features, frames, labels, universes, horizons,
  or successor strategies. Not as a suggestion, not as a closing line.
* **A bug found in the model or evidence pipeline is recorded, not acted on.** Finding
  one is not a reason to reopen a closed question. That reflex is the loop the stopping
  rule ends.
* Only a full Stage-1 pass (rank-IC ≥ +0.03 **and** t ≥ 2.0) that **also** clears WRC/SPA
  would reopen anything. Nothing short of both halves.

---

## 1. Tier 1 — constructing library code

**Trigger: you are about to add or edit a module in `qt/`.**

1. **Pure by default.** Arguments in, values out. No network, no file reads, no clock.
   The layer that *fetches* lives somewhere else on purpose — that separation is what
   makes the arithmetic testable against hand-computed answers.
   `validate_protocols.py::test_tier1_purity` enforces this.
2. **No *broad* handler may swallow a failure.** Never a bare `except:` or an
   `except Exception:` that does nothing — those catch genuine bugs alongside the
   expected error, and that is what silently disabled two v25 brakes. A narrow
   `except (TypeError, ValueError): pass` as a typed fall-through is fine: the rule is
   about **breadth, not about `pass`.** Name the errors you expect. **Fail closed:** a
   function that cannot determine whether an action is safe must refuse it.
   `validate_protocols.py::test_tier1_no_silent_except` enforces this.
3. **Name the incident.** Every guard exists because something went wrong. Say which
   thing, in the docstring, with the commit. A rule whose reason is lost gets deleted by
   the next person who finds it inconvenient.
4. **State deliberate differences at the call site.** Where new code intentionally
   departs from what ran before, say so where it happens rather than quietly improving.
5. **Ship the test with the module.** A Tier 1 module without a `validate_*.py` suite is
   not finished, and the suite runs on every push — never on request.

## 2. Tier 2 — constructing infrastructure

**Trigger: you are about to add a workflow, a scheduled job, an alarm, or a writer.**

1. **An alarm must not sit behind a failing gate.** Any alarm, guard or invariant check
   carries `if: always()` and sits where an upstream red cannot skip it. This rule cost
   two silent failures in 2026-08, both hidden under a permanently-red gate.
   `validate_protocols.py::test_tier2_alarm_placement` enforces this.
2. **Every scheduled writer is append-only, with shrink detection.** Four separate
   incidents were crons rewriting a file they should only have extended — one erased 921
   rows before anyone noticed. Written rows never move; first write wins.
   Use `qt.ledger`, never a bare `to_csv`.
3. **A constant shared across files must be checked, not remembered.** Sunset dates,
   thresholds, window starts. `validate_protocols.py::test_tier2_matched_constants`.
4. **Paging is independent of anyone looking.** An alarm nobody is paged by is a log line.
5. **Exemptions are declared in code, with a reason and a date.** `EXEMPT` in
   `validate_protocols.py` is the only way to silence a finding. An undeclared exemption
   is a quiet failure wearing a different hat.

## 3. Tier 3 — conducting research

**Trigger: you are about to write code that will produce a number someone might act on.**

1. 🔑 **State the prior first, as a number, in the repo, before the code exists.** The
   last programme recorded ≈10% in advance and came in consistent with that prior. If the
   number comes out low, that is information, not a reason to skip writing it down.
2. **Pre-registration before measurement.** Declare the specification, the threshold, the
   terminal date and the K budget *before* any return is computed. `qt.referee` owns this
   and can veto. ⚠️ **`authorize_read` does not check the prior** (`qt/referee.py:161`
   tests four things and that is not one) — until patched, the prior is on the operator.
3. **Anti-deferral.** A read is spent when taken. A bug found afterwards does not reopen
   it and does not reset the clock.
4. 🔑 **Bundle the pre-read steps into one PR — but one commit per step, in order.**
   Signing the prior, landing the input guard, and recording the fingerprint and the
   availability rehearsal used to be one PR each; spec #3 and `value_ebit_ev_v2` cost
   five apiece. They land together now. **The order is not a convenience and does not
   bundle with them**: everything pre-registration claims rests on the prior having
   been fixed before anyone saw how the data would behave. That order used to be proved
   by git — separate PRs, separate squash commits, separate timestamps — and spec #2's
   prior cites "see git timestamps" as its evidence. **A squash-merged bundle destroys
   that proof**, collapsing the steps into one commit that could have been written in
   any order, including after the numbers were known. So record `prior_signed_at` and
   `inputs_fingerprint.recorded_at` as **data in the registry**, where
   `prior_signed_at <= inputs_recorded_at <= read_at` is enforced by
   `validate_qt_phase1.test_pre_read_ordering` and visible in the diff. **Merge the
   bundle with rebase, not squash**, so the per-step commits survive too. The record
   must not depend on a reader reconstructing history from commit metadata.
5. **Do not renegotiate a threshold after seeing the result.** S1, S2, S3 and E1 all
   failed and not one was moved. That record is worth more than any model in this repo.
6. **When a measurement and a narrative disagree, report the measurement.** Including
   when the narrative is one you wrote earlier in the same session.
7. **Never quote a series the code itself calls invalid.** The legacy `confidence`
   rank-IC prints a loud warning banner and must not be used as evidence.

## 4. Working in this repository

* `HANDOFF.md` is ~684KB with single lines over 30KB. **Never read it whole.** Derive
  line ranges (`docs/WEEKLY_REVIEW.md` has two tested, self-locating recipes) and pipe
  through `cut -c1-600`.
* **Monday is the review**, inline, against `docs/WEEKLY_REVIEW.md`. On other days
  "nothing needs you today" is usually the correct answer.
* `predictions.csv` has quoted dict fields, so `awk -F,` sees 29/33/37 fields per row.
  Read `rank_score` as `$NF`, never a fixed index.
* **Never put `/` inside a `gh --jq` expression** — it gets path-mangled and fails
  silently. Query fields separately.
* **Local Python works** (3.12, with `numpy`/`pandas` installed as of 2026-11-11).
  Run the hermetic suites locally before pushing — a CI round-trip per typo is
  the slowest way to find one. Network-dependent scripts still run on CI, where
  the secrets live. *This line previously said there was no usable local Python;
  that was true of a Windows Store stub and is no longer true.*
* `quant_runner.py` cannot be `ast`-parsed — `SECTOR_MAP` lives inside a string literal.
* Trades are fired by the operator, never by Claude.

## 5. Agent scaffolding

`.claude/` carries the house patterns so they do not have to be rediscovered:

| | |
|---|---|
| `skills/hermetic-validator/` | how a `validate_*.py` suite is written here — structural guards, tripwires, the impossible-zero rule |
| `skills/gated-workflow/` | the validate/side-effect job split, double gating, artifact-not-commit, fingerprint-before-counting |
| `agents/integrity-reviewer` | reviews a diff for measurement-integrity defects only. Use it before merging anything that counts something |
| `agents/validator-author` | writes a suite against the skill, and inverts it to prove the checks are wired |
| `agents/module-builder` | builds one module of `docs/COLLECTIVE_AGENT_BRIEF.md` |

🔑 **Parallel agents suit construction, not measurement.** Building a module
against a written spec parallelises well. Deciding whether a number means
anything does not, and this repository's failures have been measurement
failures, not throughput failures. Keep the orchestrator on anything that
interprets a result.
