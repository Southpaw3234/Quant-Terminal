"""Referee — v28 Phase 1. Owns the pre-registration and can VETO.

`docs/V28_AGENT_ARCHITECTURE.md` §3.4. This is the one role an agent team buys
that a single operator cannot easily give themselves.

WHY IT EXISTS
-------------
v25's discipline held four times — S1, S2, S3 and E1 all failed and not one
threshold was renegotiated. That is genuinely rare. But the proposer and the
judge were the same person, and that arrangement is only safe while that person
is disinterested. It stops being safe on exactly the read that matters most.

So: make it structural. Research proposes. Referee decides whether a read is
permitted at all, before any return is computed.

WHAT IT ENFORCES
----------------
1. **K budget.** `docs/V27_PREREGISTRATION.md` declares K = 5. One is spent.
2. **Declaration precedes reading.** A specification not declared before its
   read does not count — that is the entire difference between a
   pre-registration and a post-hoc rationalisation.
3. **Specifications are frozen once declared.** Editing parameters after
   declaration is a new specification, not an amendment, and consumes budget.
4. **Anti-deferral.** A specification that has been read cannot be re-read. No
   bug found afterwards reopens it.
5. **Terminal date.** No reads after 2027-03-31.

🔑 **THE REGISTRY IS THE MACHINE'S LEDGER; THE MARKDOWN IS THE HUMAN CONTRACT.**
This module deliberately does NOT parse `V27_PREREGISTRATION.md` — a regex over
prose is a fragile place to put a budget. The document remains authoritative for
humans; the registry is what the Referee counts.

⚠️ **A REBUILD DOES NOT RESET THE BUDGET.** v28 is new machinery, not a new
hypothesis. If the registry is ever cleared to "start fresh", the project has
become the rebound-building `NEXT_ARCHITECTURE.md` §⑤ warned about. The tell,
written down while it is cheap to admit: *rebuilding the machinery is
legitimate; resetting the counter is not.*
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

K_BUDGET = 5
TERMINAL_DATE = "2027-03-31"
DEFAULT_REGISTRY = Path("data/registry/specifications.json")


@dataclass
class Verdict:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class Spec:
    spec_id: str
    params: dict
    declared_at: str
    read_at: str = ""
    result: dict = field(default_factory=dict)

    @property
    def is_read(self) -> bool:
        return bool(self.read_at)


class Referee:
    """Counts K, refuses undeclared or over-budget reads, records verdicts."""

    def __init__(self, registry_path=DEFAULT_REGISTRY, k_budget: int = K_BUDGET,
                 terminal_date: str = TERMINAL_DATE):
        self.path = Path(registry_path)
        self.k_budget = k_budget
        self.terminal_date = terminal_date
        self.specs: dict = {}
        self._load()

    # ───────────────────────────────────────────────────────── persistence

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except Exception as exc:
            # FAIL CLOSED. An unreadable registry means the budget is unknown,
            # and an unknown budget must not be treated as an empty one.
            raise ValueError(
                f"registry at {self.path} is unreadable ({exc}) — refusing to "
                f"proceed with an unknown K budget") from exc
        for sid, d in (raw.get("specifications") or {}).items():
            self.specs[sid] = Spec(**d)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "k_budget": self.k_budget,
            "terminal_date": self.terminal_date,
            "specifications": {s: asdict(v) for s, v in self.specs.items()},
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    # ─────────────────────────────────────────────────────────── accounting

    def k_used(self) -> int:
        """Budget is consumed by a READ, not by a declaration.

        Declaring costs nothing: thinking about a specification is free and
        should stay free, or the incentive is to think less. Measuring is what
        is scarce.
        """
        return sum(1 for s in self.specs.values() if s.is_read)

    def k_remaining(self) -> int:
        return max(0, self.k_budget - self.k_used())

    # ───────────────────────────────────────────────────────────── decisions

    def declare(self, spec_id: str, params: dict, declared_at: str) -> Verdict:
        """Register a specification. Refuses to silently redefine one."""
        if spec_id in self.specs:
            existing = self.specs[spec_id]
            if existing.params != params:
                return Verdict(False,
                               f"'{spec_id}' already declared with different "
                               f"parameters — editing a declared specification "
                               f"is a NEW specification, not an amendment")
            return Verdict(True, f"'{spec_id}' already declared identically")
        self.specs[spec_id] = Spec(spec_id, params, declared_at)
        return Verdict(True, f"'{spec_id}' declared at {declared_at}")

    def authorize_read(self, spec_id: str, read_at: str) -> Verdict:
        """May this specification be measured? Checked BEFORE any return exists."""
        spec = self.specs.get(spec_id)
        if spec is None:
            return Verdict(False,
                           f"'{spec_id}' was never declared — a specification "
                           f"not declared before its read is post-hoc, and does "
                           f"not count")
        if spec.is_read:
            return Verdict(False,
                           f"'{spec_id}' was already read at {spec.read_at} — "
                           f"anti-deferral: a read specification cannot be "
                           f"re-read, and no bug found afterwards reopens it")
        if read_at < spec.declared_at:
            return Verdict(False,
                           f"read date {read_at} precedes declaration "
                           f"{spec.declared_at} — declaration must come first")
        if read_at > self.terminal_date:
            return Verdict(False,
                           f"{read_at} is past the terminal date "
                           f"{self.terminal_date} — the project is closed")
        if self.k_remaining() <= 0:
            return Verdict(False,
                           f"K budget exhausted ({self.k_used()}/{self.k_budget}) "
                           f"— testing beyond K invalidates E1 for every "
                           f"specification, including the ones already read")
        return Verdict(True,
                       f"authorised — this will be read "
                       f"{self.k_used() + 1}/{self.k_budget}")

    def record_read(self, spec_id: str, read_at: str, result: dict) -> Verdict:
        """Record a completed read. Re-checks authorisation; does not assume it."""
        v = self.authorize_read(spec_id, read_at)
        if not v:
            return v
        spec = self.specs[spec_id]
        spec.read_at = read_at
        spec.result = dict(result)
        return Verdict(True,
                       f"'{spec_id}' recorded — {self.k_used()}/{self.k_budget} "
                       f"spent, {self.k_remaining()} remaining")

    def status(self) -> str:
        lines = [f"K {self.k_used()}/{self.k_budget} spent, "
                 f"{self.k_remaining()} remaining; terminal {self.terminal_date}"]
        for sid, s in sorted(self.specs.items()):
            if s.is_read:
                verdict = s.result.get("verdict", "?")
                lines.append(f"  {sid:<28} READ {s.read_at}  {verdict}")
            else:
                lines.append(f"  {sid:<28} declared {s.declared_at}, unread")
        return "\n".join(lines)
