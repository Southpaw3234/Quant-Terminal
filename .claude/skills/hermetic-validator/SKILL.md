---
name: hermetic-validator
description: Write or review a validate_*.py suite for this repository — the check() pattern, structural guards that cannot be fooled by prose, tripwires, and the impossible-zero rule. Use whenever adding or changing a validator, adding a script that produces a number someone might act on, or reviewing a diff that touches data selection, counting, or measurement.
---

# Hermetic validators, the way this repo writes them

A validator here is not a unit-test suite. It is an argument that a specific
class of silent wrongness **cannot** occur, written so the argument fails
loudly when it stops holding.

Twenty-two `validate_*.py` files exist. Match them.

## The shape

```python
#!/usr/bin/env python3
"""Pure-logic validation for <thing>. No network, no clock.

Say here WHAT CLASS OF WRONGNESS this file exists to prevent. Not "tests
the extractor" — "proves the selection could not have seen the future".
"""
from __future__ import annotations
import ast, inspect, sys

import the_module_under_test as ex

PASS, FAIL = [], []

def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))
```

`why` is not decoration. It carries the reason the check exists, so a
failure three months from now explains itself. Write it for someone who has
never seen the file.

End with the count, the failed names, and `sys.exit(1)`:

```python
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL: print(f"  FAILED  {f}")
    sys.exit(1)
print("All checks passed. No network touched, no criterion evaluated.")
```

**Encoding.** This repo's prose uses `🔑`, `⚠️`, `§⑥`. A Windows console is
cp1252 and raises on the first one, making a suite that PASSED look like a
crash. Fix the stream, not the prose:

```python
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

## 🔑 Structural guards, never textual ones

**This is the rule that has been learned the hard way three separate times
in one day.** A guard that greps source for a banned word fails on the
comment explaining the ban.

```python
# ❌ WRONG — fails on the docstring that documents the guard
src = inspect.getsource(ex)
check("no-referee", "referee" not in src)

# ✅ RIGHT — what makes a capability unreachable is the absence of a BINDING
def _imports(mod) -> set:
    tree = ast.parse(inspect.getsource(mod))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names: out.add(a.name)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            for a in n.names: out.add(f"{n.module}.{a.name}")
    return out

check("does-not-import-qt.referee", "qt.referee" not in _imports(ex),
      "a scheduled job must not be able to spend a read")
```

Three guard idioms, in order of strength:

| guard | catches | example |
|---|---|---|
| **import absent** (`ast`) | the capability is unreachable | no `qt.referee`, no broker SDK |
| **signature pinned** (`inspect.signature`) | the data is not in scope | `select_cycle(eligible, ytd_by_ticker, quantile)` — no exit price exists to leak |
| **identifier absent** (AST names) | nothing is bound to an outcome | no identifier containing `forward`, `outcome`, `pnl` |

Pinning a signature is stronger than testing behaviour. A behavioural test
proves the code did not look ahead *on the inputs you gave it*. A signature
proves it **could not have**.

For a banned URL or host, walk `ast.Constant` string literals — not raw
source, which contains the comment naming the host in order to forbid it.

## 🔑 The impossible-zero rule

**A zero that cannot be non-zero is not a measurement.**

This retired an entire pre-registered specification, then reappeared the
same day in an unrelated feed. Both times the number looked completely
ordinary.

- Spec #4's rehearsal reported **0 delistings of 2,507** events in a sample
  of companies *in breach of a listing rule*. The classifier was fine; the
  price source does not serve delisted companies, so the sample was
  survivors.
- The daily feed reported **0 Form 4 filings in universe of 555**. EDGAR
  lists no ticker on that form at all, so the intersection was structurally
  unreachable and would have read `0` every day forever.

So: whenever a count can be structurally unreachable, **the code must emit
`null` and say why**, and the validator must have a case for it.

```python
check("no-tickers-anywhere-is-unknown-not-zero",
      g["in_universe"] is None and g["tickers_resolvable"] is False,
      "not one filing yielded a ticker, so the intersection is UNKNOWN. "
      "Reported as 0 it reads as a finding, every day, forever")
check("real-zero-still-reports-zero", h["in_universe"] == 0,
      "tickers WERE resolvable and none matched — 'quiet day' and "
      "'cannot see' must stay distinguishable")
```

Test **both** sides. A rule that turns every zero into `null` is just as
wrong in the other direction.

## Tripwires

A check that asserts the *current* state and is **meant** to go red when it
changes. Say so in the name and the comment, or someone will "fix" it.

```python
# ⚠️ TRIPWIRE. MEANT to go red the day this specification is declared.
check("tax-loss-not-declared",
      not any(s.startswith("tax_loss") for s in specs),
      "the extractor exists, the slot does not")
```

When the event happens, **flip the tripwire, do not delete it** —
`registry-still-unread` became `registry-read-and-frozen`, pinning the new
state.

## Bounds, not equalities, for anything append-only

K counts only go up. An equality check goes stale on the next read and
fails for a reason that has nothing to do with the thing it guards.

```python
# ❌ check("k-used", r.k_used() == 3)
# ✅
check("k-within-budget", 0 <= r.k_used() <= r.k_budget)
check("spec2-k-counted", s2.is_read and 2 <= r.k_used() <= r.k_budget)
```

## Hermetic means hermetic

The CI job installs `numpy` and `pandas` and **not** `yfinance` or
`requests`, so the suite cannot reach a price even by accident. That is what
makes "selection is blind" a demonstrated property rather than a claim.
Keep network code behind a lazy import inside the function that needs it.

Pair every suite with:

```yaml
- name: Assert nothing under data/ changed
  run: |
    set -u
    if ! git diff --quiet -- data/ 2>/dev/null; then
      echo "::error::validation modified data/ — it must be read-only"; exit 1
    fi
```

## Ticker safety

`NA` is Nano Labs. pandas destroys it in **two** independent places —
`read_csv` defaults and DataFrame construction under pandas 3's string
dtype. It has cost this repo real data twice.

Always go through `qt/tickers.py` (`read_csv`, `normalise`, `frame`,
`as_labels`, `drop_blank`), and put a case in the suite:

```python
check("ticker-NA-survives", f(["NANO LABS LTD  (NA)"]) == {"NA"})
```

## What a good check looks like

Each one should name a **specific way the world goes wrong**, and the `why`
should let a stranger reconstruct the failure:

```python
check("missing-base-dropped", "NOBASE" not in got,
      "a missing base price defaulted to zero would rank the name as "
      "unchanged and keep it OUT of the bottom quintile")

check("dedupes-repeat-filers", got["4"]["in_universe"] == 1,
      "EXMP filed two Form 4s; it is ONE name in the book, not two")

check("post-formation-crash-invisible", abs(got["FLAT"]) < 1e-9,
      "FLAT falls 90% after the formation date; the ranking must not see it")
```

That last one is the pattern worth copying: construct a fixture where the
look-ahead, if present, would produce a **dramatically** different answer.
A subtle fixture proves nothing.

## Before you call it done

- [ ] The docstring names the class of wrongness, not the file under test.
- [ ] Every look-ahead guard is structural (import / signature / identifier).
- [ ] Every count that could be structurally unreachable has a `null` case **and** a real-zero case.
- [ ] Tripwires say they are tripwires.
- [ ] No equality on an append-only counter.
- [ ] Ticker-safe reads, with a `NA` case.
- [ ] Runs with no network installed.
- [ ] Asserts `data/` is untouched.
- [ ] You ran it. A validator nobody ran is not evidence.
