"""The thesis ledger — pre-registration applied to the operator's judgment.

Stdlib only. No network, no pandas, no clock except what the caller passes.

─────────────────────────────────────────────────────────────────────────────
WHY THIS IS THE MOST VALUABLE MODULE IN THE COLLECTIVE AGENT

This repository's single demonstrated achievement is not a model. It is that
five pre-registered thresholds were honoured when the data said stop, and not
one was renegotiated. `docs/NEXT_ARCHITECTURE.md` §3.5 is blunt about where
that is worth pointing next:

    That discipline -- honoring a pre-registered stop when the data says
    stop -- is rarer and worth more than any model, and most people cannot
    do it with their own money.

So the ledger does for a position what the registry does for a specification:
the falsifiers are written BEFORE the outcome is known, and the record of
what was claimed cannot be quietly rewritten afterwards.

─────────────────────────────────────────────────────────────────────────────
🔑 THE THREE PROPERTIES, AND HOW EACH IS ENFORCED

1. **A thesis cannot be opened without falsifiers and a review date.**
   Not validated after the fact -- `open_thesis` refuses. A thesis with no
   falsifier is a hope, and the entire point is to make the difference
   visible at the moment of writing.

2. **Nothing is ever edited. Every change is an APPEND.**
   There is no update function and no delete function in this module. A
   correction is a new entry that references the old one, so the original
   claim survives next to the correction.

3. **History cannot be rewritten silently.** Each entry carries the hash of
   the entry before it. Changing any past line breaks every hash after it,
   and `verify` reports exactly where. An append-only file that nobody
   verifies is a file somebody edits.

─────────────────────────────────────────────────────────────────────────────
🔑 THE OVERRIDE, WHICH IS THE WHOLE POINT

A falsifier firing while the operator keeps the position is not forbidden.
Markets are not a court and the operator owns the money.

It is *recorded*, as `kind: "override"`, and counted. `summary()` reports the
override count beside the closed count, because a ledger whose purpose is to
catch somebody renegotiating with themselves has to make that number
impossible to look away from.

An override that is never written down is the failure this module exists to
prevent. An override written down is the operator doing exactly the right
thing with a decision they have thought about.
"""

from __future__ import annotations

import hashlib
import json

GENESIS = "0" * 64

KINDS = ("open", "review", "override", "close", "note")
CLOSE_REASONS = ("falsifier_fired", "review_reached", "thesis_played_out",
                 "position_exited", "other")


# ── the chain ────────────────────────────────────────────────────────────────

def canonical(entry: dict) -> str:
    """The bytes an entry's hash is taken over: everything except the hash."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def hash_entry(entry: dict) -> str:
    return hashlib.sha256(canonical(entry).encode("utf-8")).hexdigest()


def seal(entry: dict, prev_hash: str) -> dict:
    """Attach the previous hash and this entry's own. Returns a new dict."""
    e = dict(entry)
    e.pop("hash", None)
    e["prev"] = prev_hash
    e["hash"] = hash_entry(e)       # canonical() drops the hash field itself
    return e


def verify(entries: list) -> dict:
    """Walk the chain. -> {"ok": bool, "n": int, "broken_at": int|None, "why": str}

    Reports WHERE the chain breaks rather than a bare False, because "the
    ledger was edited" and "the ledger was edited at entry 7, which is the
    close of the position you are about to discuss" are different facts.
    """
    prev = GENESIS
    for i, e in enumerate(entries or []):
        if e.get("prev") != prev:
            return {"ok": False, "n": len(entries), "broken_at": i,
                    "why": f"entry {i} claims prev={e.get('prev', '')[:12]}… "
                           f"but the chain is at {prev[:12]}…"}
        recomputed = hash_entry({k: v for k, v in e.items() if k != "hash"})
        if recomputed != e.get("hash"):
            return {"ok": False, "n": len(entries), "broken_at": i,
                    "why": f"entry {i} content does not match its hash — "
                           f"it was edited after it was written"}
        prev = e["hash"]
    return {"ok": True, "n": len(entries or []), "broken_at": None,
            "why": "chain intact"}


def tip(entries: list) -> str:
    return entries[-1]["hash"] if entries else GENESIS


# ── appending ────────────────────────────────────────────────────────────────

def open_thesis(entries: list, thesis_id: str, ticker: str, claim: str,
                falsifiers: list, review_date: str, at: str,
                stance: str = "", sizing: str = "") -> dict:
    """Open a thesis. REFUSES without falsifiers and a review date.

    🔑 The refusal is the feature. A thesis with no stated falsifier cannot
    be wrong, which means it cannot be right either -- and the moment to
    notice that is while writing it, not at the review.
    """
    fs = [str(f).strip() for f in (falsifiers or []) if str(f).strip()]
    if not thesis_id or not str(thesis_id).strip():
        raise ValueError("a thesis needs an id")
    if not str(claim).strip():
        raise ValueError("a thesis needs a claim")
    if not fs:
        raise ValueError(
            "a thesis needs at least one FALSIFIER, stated now. What would "
            "have to be true for this to be wrong? If nothing would, this is "
            "not a thesis.")
    if not str(review_date).strip():
        raise ValueError(
            "a thesis needs a REVIEW DATE. An open position with no review "
            "date is one that gets reviewed when it hurts.")
    if any(e.get("kind") == "open" and e.get("thesis_id") == thesis_id
           for e in entries or []):
        raise ValueError(f"thesis '{thesis_id}' is already open — a second "
                         f"open would make the record ambiguous")
    return seal({"kind": "open", "thesis_id": str(thesis_id).strip(),
                 "at": at, "ticker": str(ticker).strip().upper(),
                 "claim": str(claim).strip(), "falsifiers": fs,
                 "review_date": str(review_date).strip(),
                 "stance": str(stance or "").strip(),
                 "sizing": str(sizing or "").strip()},
                tip(entries))


def review(entries: list, thesis_id: str, at: str, note: str,
           falsifiers_fired: list = None, next_review: str = "") -> dict:
    """Record a review. Naming a fired falsifier does NOT close the thesis —
    that is a separate, deliberate act (`close` or `override`)."""
    _require_open(entries, thesis_id)
    return seal({"kind": "review", "thesis_id": thesis_id, "at": at,
                 "note": str(note or "").strip(),
                 "falsifiers_fired": [str(f) for f in (falsifiers_fired or [])],
                 "next_review": str(next_review or "").strip()},
                tip(entries))


def override(entries: list, thesis_id: str, at: str, falsifier: str,
             reasoning: str) -> dict:
    """A falsifier fired and the position is being KEPT. Recorded, not hidden.

    Requires naming the falsifier and giving reasoning. Both are mandatory
    because an override without them is indistinguishable from forgetting.
    """
    _require_open(entries, thesis_id)
    if not str(falsifier).strip():
        raise ValueError("an override must name the falsifier that fired")
    if not str(reasoning).strip():
        raise ValueError(
            "an override must say WHY the thesis survives a falsifier it "
            "declared in advance. This is the sentence future-you needs.")
    return seal({"kind": "override", "thesis_id": thesis_id, "at": at,
                 "falsifier": str(falsifier).strip(),
                 "reasoning": str(reasoning).strip()}, tip(entries))


def close(entries: list, thesis_id: str, at: str, reason: str,
          note: str = "", falsifier: str = "") -> dict:
    """Close a thesis. The reason is from a fixed list, not free text."""
    _require_open(entries, thesis_id)
    if reason not in CLOSE_REASONS:
        raise ValueError(f"reason must be one of {CLOSE_REASONS}")
    if reason == "falsifier_fired" and not str(falsifier).strip():
        raise ValueError("closing on a falsifier must name which one")
    return seal({"kind": "close", "thesis_id": thesis_id, "at": at,
                 "reason": reason, "falsifier": str(falsifier or "").strip(),
                 "note": str(note or "").strip()}, tip(entries))


def note(entries: list, thesis_id: str, at: str, text: str) -> dict:
    _require_open(entries, thesis_id)
    if not str(text).strip():
        raise ValueError("an empty note is not a record of anything")
    return seal({"kind": "note", "thesis_id": thesis_id, "at": at,
                 "text": str(text).strip()}, tip(entries))


def _require_open(entries: list, thesis_id: str) -> None:
    st = state(entries).get(thesis_id)
    if st is None:
        raise ValueError(f"no thesis '{thesis_id}' in the ledger")
    if st["status"] == "closed":
        raise ValueError(
            f"thesis '{thesis_id}' is closed. A closed thesis is not reopened "
            f"— open a new one that references it, so the original claim and "
            f"the second thought both survive.")


# ── reading ──────────────────────────────────────────────────────────────────

def state(entries: list) -> dict:
    """Current state per thesis, folded from the append-only log."""
    out = {}
    for e in entries or []:
        tid = e.get("thesis_id")
        if not tid:
            continue
        if e.get("kind") == "open":
            out[tid] = {"thesis_id": tid, "ticker": e.get("ticker", ""),
                        "claim": e.get("claim", ""),
                        "falsifiers": list(e.get("falsifiers") or []),
                        "review_date": e.get("review_date", ""),
                        "stance": e.get("stance", ""),
                        "opened_at": e.get("at", ""), "status": "open",
                        "fired": [], "overrides": 0, "reviews": 0,
                        "closed_at": "", "close_reason": ""}
        elif tid in out:
            s = out[tid]
            k = e.get("kind")
            if k == "review":
                s["reviews"] += 1
                for f in e.get("falsifiers_fired") or []:
                    if f not in s["fired"]:
                        s["fired"].append(f)
                if e.get("next_review"):
                    s["review_date"] = e["next_review"]
            elif k == "override":
                s["overrides"] += 1
                if e.get("falsifier") and e["falsifier"] not in s["fired"]:
                    s["fired"].append(e["falsifier"])
            elif k == "close":
                s["status"] = "closed"
                s["closed_at"] = e.get("at", "")
                s["close_reason"] = e.get("reason", "")
                if e.get("falsifier") and e["falsifier"] not in s["fired"]:
                    s["fired"].append(e["falsifier"])
    return out


def due(entries: list, today: str) -> list:
    """Open theses whose review date has arrived. Sorted oldest first."""
    return sorted(
        (s for s in state(entries).values()
         if s["status"] == "open" and s["review_date"]
         and s["review_date"] <= today),
        key=lambda s: s["review_date"])


def unreviewed_fired(entries: list) -> list:
    """🔑 Open theses with a fired falsifier and NO override recorded.

    The single most important query in this module. A falsifier fired, the
    position is still on, and no reasoning was written down. That is not an
    override -- an override is a decision. This is drift, and it is exactly
    what the ledger exists to surface.
    """
    return [s for s in state(entries).values()
            if s["status"] == "open" and s["fired"] and s["overrides"] == 0]


def summary(entries: list, today: str = "") -> dict:
    st = state(entries)
    openi = [s for s in st.values() if s["status"] == "open"]
    return {"theses": len(st),
            "open": len(openi),
            "closed": sum(1 for s in st.values() if s["status"] == "closed"),
            # Reported beside the others on purpose. A ledger meant to catch
            # somebody renegotiating with themselves must make this number
            # impossible to look away from.
            "overrides": sum(s["overrides"] for s in st.values()),
            "closed_on_falsifier": sum(
                1 for s in st.values()
                if s["close_reason"] == "falsifier_fired"),
            "reviews_due": len(due(entries, today)) if today else None,
            "drifting": len(unreviewed_fired(entries)),
            "chain": verify(entries)}


# ── file i/o ─────────────────────────────────────────────────────────────────

def load(path: str) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except FileNotFoundError:
        return []
    return out


def append(path: str, entry: dict) -> None:
    """Append one sealed entry. The ONLY write in this module.

    There is no update and no delete, by design -- not as a convention a
    future edit can relax, but as an absence of code to call.
    """
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
