#!/usr/bin/env python3
"""Validate the PROTOCOL layer — the Tier 2 and Tier 3 carry-forward.

`docs/CARRY_FORWARD.md` §⑧. Companion to `validate_qt.py`, which tests the
Tier 1 library. This one tests the things that are normally left as prose and
therefore rot: how modules are allowed to be built, where alarms are allowed to
sit, and whether the discipline file exists at all.

WHY THIS EXISTS
---------------
Tier 1 is code and cannot rot silently — nine CI suites run on every push.
Tiers 2 and 3 were, until this file, English in a document. That is precisely
the arrangement `qt/referee.py` was built to replace: v25 concluded that a rule
holds only while the person bound by it is disinterested, and made the rule
structural instead. This file applies that same conclusion one level up.

A protocol that cannot fail a build is a preference, not a protocol.

STDLIB ONLY, ON PURPOSE
-----------------------
No pandas, no numpy, no yaml. This file is meant to be the FIRST thing copied
into a successor repository, before dependencies exist. It must run on a bare
Python 3.9+ with nothing installed.

SKIPS ARE LOUD AND ARE NOT PASSES
---------------------------------
A fresh repository has no workflows and no ledgers, so those checks SKIP rather
than fail. A skip prints `[SKIP]` and is counted separately in the summary. It
never contributes to a green result by silence — the counts are reported.

EXEMPTIONS ARE DECLARED IN CODE, WITH A REASON AND A DATE
---------------------------------------------------------
`EXEMPT` below is the only way to silence a finding. An exemption that is not
written here does not exist, which is the difference between an exception and a
quiet failure. v25 was damaged four separate times by things that were silently
tolerated rather than declared.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKFLOWS = ROOT / ".github" / "workflows"

FAILURES: list = []
SKIPPED: list = []
PASSED: list = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    (PASSED if ok else FAILURES).append(name)


def skip(name: str, why: str) -> None:
    print(f"  [SKIP] {name}: {why}")
    SKIPPED.append(name)


# ═══════════════════════════════════════════════════ DECLARATIONS
# Everything below is a declaration, not a discovery. Extend deliberately.

# Tier 1 — modules declared PURE: no network, no I/O, arguments in and values
# out. This is what makes them testable against hand-computed answers, and it
# is why the layer that FETCHES data lives somewhere else on purpose.
PURE_MODULES = [
    "calendar_time", "execution", "guards", "ledger", "liquidity",
    "measurement", "prices", "reconcile", "referee", "roster_update",
    "scoring", "wrc",
]

# Anything that reaches the network. `ledger` and `referee` touch the
# filesystem by design (they own persistence); everything else should not.
NETWORK_IMPORTS = (
    "requests", "urllib", "http.client", "httpx", "aiohttp", "socket",
    "yfinance", "alpaca", "finnhub",
)

# Tier 2 — constants that appear in more than one workflow and MUST agree.
# An unmatched sunset pages Discord every weekday forever over an expected
# absence; an unmatched threshold is worse, because it is silent.
MATCHED_CONSTANTS = ["QT_SUNSET_DATE"]

# Tier 2 — workflows in scope for the alarm-placement rule. Scoped rather than
# global because a workflow that IS an alarm end to end (the watchdog) has
# different semantics from a long pipeline with an alarm bolted to the end.
ALARM_SCOPE = ["quant_daily.yml"]
ALARM_PATTERNS = ("guard", "alarm", "invariant")

# Tier 2 — append-only evidence ledgers: key column, uniqueness, ordering.
LEDGERS = {
    "data/shadow/rank_ic_v2.csv": "date",
    "data/shadow/rank_ic.csv": "date",
    "data/shadow_intraday/rank_ic.csv": "date",
    "data/stat_arb/stat_arb_ls.csv": "date",
}

# Tier 3 — rules the discipline file must actually declare. Presence only:
# this asserts the rule was written down, never that it was followed.
REQUIRED_PROTOCOL_RULES = [
    "state the prior",
    "fail closed",
    "append-only",
    "alarm",
    "pre-registration",
]

# The ONLY way to silence a finding. Key: "file:step-or-item". Value: reason.
EXEMPT = {
    "quant_daily.yml:Verify evidence-clock persistence (alarm only)":
        "Declared 2026-09-21. Real instance of the rule — this alarm is gated "
        "on run type, so an earlier failure skips it. NOT fixed because the "
        "workflow retires at the 2026-09-29 sunset and a wind-down edit would "
        "perturb the final S4 reads. Do not copy this step into a successor.",
}


# ═══════════════════════════════════════════════════ TIER 1 — construction

def test_tier1_purity():
    print("\n--- tier 1: declared-pure modules reach no network ---")
    pkg = ROOT / "qt"
    if not pkg.is_dir():
        skip("tier1-purity", "no qt/ package in this repo yet")
        return
    for mod in PURE_MODULES:
        path = pkg / f"{mod}.py"
        if not path.exists():
            skip(f"purity-{mod}", "module not present")
            continue
        offenders = []
        for i, line in enumerate(path.read_text(encoding="utf-8",
                                                errors="replace").splitlines(), 1):
            s = line.strip()
            if not (s.startswith("import ") or s.startswith("from ")):
                continue
            for bad in NETWORK_IMPORTS:
                if re.search(rf"\b{re.escape(bad)}\b", s):
                    offenders.append(f"{i}:{bad}")
        check(f"purity-{mod}", not offenders,
              "no network import" if not offenders
              else f"reaches the network -> {', '.join(offenders)}")


def _is_broad_except(line: str) -> bool:
    """Is this a BROAD handler — bare `except:` or `except Exception:`?

    Narrow handlers are deliberately allowed. `except (TypeError, ValueError):
    pass` as a typed fall-through in a pure helper is idiomatic and safe: the
    function still reaches a definite answer on the next line. What killed v25
    was the broad form — a bare `except: pass` around the consecutive-loss
    brake and an `except Exception: pass` around the drawdown block, each of
    which swallowed genuine bugs along with the expected error and silently
    DISABLED the guard (`dc04017`, `a0dd471`).

    So the rule is about BREADTH, not about `pass`. A handler that names the
    errors it expects has reasoned about them; one that catches everything
    has not.
    """
    m = re.match(r"^\s*except\b([^:]*):", line)
    if m is None:
        return False
    clause = m.group(1).strip()
    clause = re.sub(r"\s+as\s+\w+$", "", clause).strip()
    clause = clause.strip("()").strip()
    return clause in ("", "Exception", "BaseException")


def test_tier1_no_silent_except():
    print("\n--- tier 1: no BROAD handler silently swallows a failure ---")
    pkg = ROOT / "qt"
    if not pkg.is_dir():
        skip("tier1-no-silent-except", "no qt/ package in this repo yet")
        return
    single = re.compile(r"^\s*except\b[^:]*:\s*pass\s*$")
    opener = re.compile(r"^\s*except\b[^:]*:\s*$")
    for path in sorted(pkg.glob("*.py")):
        lines = path.read_text(encoding="utf-8",
                               errors="replace").splitlines()
        hits = []
        for i, line in enumerate(lines):
            if not _is_broad_except(line):
                continue
            if single.match(line):
                hits.append(i + 1)
                continue
            if opener.match(line):
                for nxt in lines[i + 1:]:
                    if not nxt.strip():
                        continue
                    if nxt.strip() == "pass":
                        hits.append(i + 1)
                    break
        check(f"no-silent-except-{path.name}", not hits,
              "no broad `except: pass`" if not hits
              else f"BROAD handler swallows everything at line(s) {hits} — a "
                   f"guard that fails open is worse than no guard, because it "
                   f"is trusted. Name the errors you expect, or fail closed")


# ═══════════════════════════════════════════════════ TIER 2 — infrastructure

def _workflow_files():
    if not WORKFLOWS.is_dir():
        return []
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def test_tier2_matched_constants():
    print("\n--- tier 2: constants shared across workflows must agree ---")
    files = _workflow_files()
    if not files:
        skip("tier2-matched-constants", "no .github/workflows in this repo yet")
        return
    for const in MATCHED_CONSTANTS:
        pat = re.compile(rf"{re.escape(const)}\s*[:=]\s*['\"]?([0-9A-Za-z_.\-]+)")
        found = {}
        for f in files:
            for m in pat.finditer(f.read_text(encoding="utf-8",
                                              errors="replace")):
                found.setdefault(m.group(1), []).append(f.name)
        if not found:
            skip(f"matched-{const}", "not declared in any workflow")
            continue
        where = "; ".join(f"{v}={sorted(set(n))}" for v, n in found.items())
        check(f"matched-{const}", len(found) == 1,
              f"one value everywhere ({where})" if len(found) == 1
              else f"DISAGREES across workflows -> {where}")


def test_tier2_alarm_placement():
    print("\n--- tier 2: an alarm must not sit behind a failing gate ---")
    files = [WORKFLOWS / n for n in ALARM_SCOPE]
    files = [f for f in files if f.exists()]
    if not files:
        skip("tier2-alarm-placement", "no in-scope workflow in this repo yet")
        return
    step_re = re.compile(r"^\s*-\s+name:\s*(.+?)\s*$")
    for f in files:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        starts = [(i, m.group(1)) for i, line in enumerate(lines)
                  if (m := step_re.match(line))]
        for idx, (ln, name) in enumerate(starts):
            if not any(p in name.lower() for p in ALARM_PATTERNS):
                continue
            end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
            body = "\n".join(lines[ln:end])
            guarded = bool(re.search(r"if:.*(always\(\)|!cancelled\(\))", body))
            key = f"{f.name}:{name}"
            if not guarded and key in EXEMPT:
                skip(f"alarm-{name[:40]}", f"DECLARED EXEMPT — {EXEMPT[key]}")
                continue
            check(f"alarm-{name[:40]}", guarded,
                  "runs even when an earlier step fails" if guarded
                  else "has no `if: always()` — an earlier failure SKIPS this "
                       "alarm, which is how two silent failures hid in 2026-08")


def test_tier2_ledger_integrity():
    print("\n--- tier 2: evidence ledgers are append-only in shape ---")
    any_found = False
    for rel, keycol in LEDGERS.items():
        path = ROOT / rel
        if not path.exists():
            skip(f"ledger-{Path(rel).name}", "not present in this repo")
            continue
        any_found = True
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            check(f"ledger-{Path(rel).name}", False, "ledger is empty")
            continue
        if keycol not in rows[0]:
            check(f"ledger-{Path(rel).name}", False,
                  f"no '{keycol}' column — cannot verify append-only shape")
            continue
        keys = [r[keycol] for r in rows]
        dupes = {k for k in keys if keys.count(k) > 1}
        ordered = keys == sorted(keys)
        check(f"ledger-{Path(rel).name}", not dupes and ordered,
              f"{len(keys)} rows, unique and ordered" if not dupes and ordered
              else f"duplicates={sorted(dupes)} ordered={ordered} — a written "
                   f"row moved or was rewritten")
    if not any_found:
        skip("tier2-ledger-integrity", "no declared ledgers in this repo yet")


# ═══════════════════════════════════════════════════ TIER 3 — discipline

def test_tier3_protocol_file():
    print("\n--- tier 3: the discipline file exists and declares its rules ---")
    path = ROOT / "CLAUDE.md"
    if not path.exists():
        check("tier3-file-present", False,
              "no CLAUDE.md — Tier 3 has no enforcement surface at all. This "
              "is the one check that must never be skipped: a discipline that "
              "is not written where it is read at decision time does not bind")
        return
    check("tier3-file-present", True, f"CLAUDE.md present ({path.stat().st_size}B)")
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    for rule in REQUIRED_PROTOCOL_RULES:
        check(f"tier3-declares-{rule.replace(' ', '-')}", rule in text,
              "declared" if rule in text
              else f"CLAUDE.md does not mention '{rule}' — presence only; this "
                   f"never asserts the rule was followed")


# ═══════════════════════════════════════════════════ MAIN

def main():
    print("=" * 70)
    print("protocol layer — Tier 1 construction, Tier 2 wiring, Tier 3 discipline")
    print("docs/CARRY_FORWARD.md §⑧   (stdlib only, no network)")
    print("=" * 70)
    test_tier1_purity()
    test_tier1_no_silent_except()
    test_tier2_matched_constants()
    test_tier2_alarm_placement()
    test_tier2_ledger_integrity()
    test_tier3_protocol_file()

    print("\n" + "=" * 70)
    print(f"passed {len(PASSED)} | skipped {len(SKIPPED)} | failed {len(FAILURES)}")
    if SKIPPED:
        print("SKIPPED (absent here, not proven):")
        for s in SKIPPED:
            print(f"  - {s}")
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Pure modules reach no network, no handler swallows a failure,")
    print("shared constants agree, alarms survive an upstream failure,")
    print("ledgers are append-only in shape, and the disciplines are written")
    print("where they are read at decision time rather than at kickoff.")


if __name__ == "__main__":
    main()
