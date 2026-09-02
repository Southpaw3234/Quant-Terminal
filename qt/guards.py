"""Fail-closed trading guards — v28 Phase 0.

Ports the guard stack out of `quant_runner.py` into pure, testable functions.
Every one of these was written *because something went wrong*, and the incident
is named at each. They are the hardest part of v25 to rebuild from scratch and
the easiest to get subtly wrong.

THE PRINCIPLE, STATED ONCE
--------------------------
**A guard that cannot determine whether an action is safe must refuse it.**
v25 learned this the expensive way: the consecutive-loss brake sat behind a bare
`except: pass`, so any error silently DISABLED the brake rather than tripping it
(fixed `dc04017`, then `a0dd471` for the drawdown block). A guard that fails
open is worse than no guard, because it is trusted.

⚠️ ONE DELIBERATE DIFFERENCE FROM v25 — READ THIS
--------------------------------------------------
v25's **sector cap fails OPEN**. The pre-trim reads:

    if _SECTOR_CAP["ok"] and _GROSS_CAP["equity"]:
        ... block ...

so when the sector read fails, the cap simply does not apply and BUYs proceed
unconstrained. The gross cap is the hard gate and does fail closed, which is why
this was survivable.

`sector_cap_allows()` here defaults to **fail-CLOSED** (`ok=False` refuses).
That is a genuine behavioural change and it is flagged rather than slipped in:
pass `fail_closed=False` to reproduce v25 exactly. The 2026-07-30 concentration
halt is the incident that argues for the stricter default.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────── gross exposure

@dataclass
class GrossCapResult:
    room: float          # dollars of headroom, never negative
    slots: int           # new BUY slots this run
    leverage: float      # gross_mv / equity
    ok: bool             # was the account read trustworthy
    reason: str = ""


def gross_cap(equity: float, gross_mv: float, ratio: float,
              slot_size: float, acct_ok: bool = True) -> GrossCapResult:
    """Headroom and BUY slots under a gross-exposure cap.

    `ratio` is the cap as a multiple of equity. `QT_MAX_GROSS='0'` is how the
    2026-08-09 wind-down turned entries off permanently: ratio 0 makes room
    negative for any non-empty book, so every BUY is pre-blocked while SELLs
    (which use `oversell_cap`) continue.

    FAILS CLOSED. If the account read did not succeed, `slots` is 0 — the
    2026-07-15 incident was an unguarded SELL path building an accidental
    22-name short book worth $81.5k, and the lesson generalises: without a
    trustworthy position read, no order is safe.
    """
    if not acct_ok:
        return GrossCapResult(0.0, 0, 0.0, False,
                              "account read failed — fail-closed, 0 slots")
    try:
        equity = float(equity)
        gross_mv = float(gross_mv)
        ratio = float(ratio)
        slot_size = float(slot_size)
    except (TypeError, ValueError) as exc:
        return GrossCapResult(0.0, 0, 0.0, False,
                              f"unparseable inputs ({exc}) — fail-closed")
    if equity <= 0:
        return GrossCapResult(0.0, 0, 0.0, False,
                              "non-positive equity — fail-closed")

    leverage = gross_mv / equity
    room = max(0.0, ratio * equity - gross_mv)
    slots = int(math.floor(room / slot_size)) if slot_size > 0 else 0
    reason = "" if slots else "no headroom"
    return GrossCapResult(room, max(0, slots), leverage, True, reason)


# ───────────────────────────────────────────────────────────── sector limit

def sector_cap_allows(sector: str, exposure: dict, pending: dict,
                      slot_size: float, ratio: float, equity: float,
                      ok: bool = True, fail_closed: bool = True) -> bool:
    """Would adding one slot in `sector` breach the per-sector cap?

    `exposure` is live dollars per sector; `pending` is what this run's pre-trim
    has already budgeted, so a single run cannot fill one sector by counting
    only the book it started with.

    ⚠️ `fail_closed=True` (default) REFUSES when `ok` is False. v25 allowed in
    that case — see the module docstring. Pass `fail_closed=False` for v25
    behaviour.

    Incident: 2026-07-30, a concentration that built up over days with nothing
    in the log to show it. The cap is half the fix; printing the split every
    run is the other half.
    """
    if not ok:
        return not fail_closed
    try:
        equity = float(equity)
        ratio = float(ratio)
        slot_size = float(slot_size)
    except (TypeError, ValueError):
        return not fail_closed
    if equity <= 0:
        return not fail_closed
    projected = (float(exposure.get(sector, 0.0))
                 + float(pending.get(sector, 0.0))
                 + slot_size)
    return projected <= ratio * equity


# ──────────────────────────────────────────────────────────── oversell gate

@dataclass
class OversellResult:
    allowed: int
    blocked: bool
    capped: bool
    reason: str = ""


def oversell_cap(qty, live_qty: float, already_sold: float = 0.0,
                 enforce: bool = True, pos_ok: bool = True) -> OversellResult:
    """The SELL quantity actually permitted. 0 refuses the order.

    Incident (2026-07-15): unguarded SELLs turned into an accidental 22-name
    SHORT book worth $81.5k, and `close_long`'s `abs()` doubled shorts instead
    of covering them. A SELL beyond the live long quantity is not a sale, it is
    a naked short entered by accident.

    FAILS CLOSED twice over: unknown positions refuse, and any exception
    refuses. `enforce=False` is local paper mode where there is no broker and
    therefore no short to open.
    """
    try:
        q = int(qty)
    except (TypeError, ValueError) as exc:
        return OversellResult(0, True, False, f"unparseable qty ({exc}) — fail-closed")
    if q <= 0:
        return OversellResult(0, False, False, "non-positive qty")
    if not enforce:
        return OversellResult(q, False, False, "enforcement off (local paper)")
    if not pos_ok:
        return OversellResult(0, True, False,
                              "live positions unknown — fail-closed")
    try:
        held = float(live_qty) - float(already_sold)
    except (TypeError, ValueError) as exc:
        return OversellResult(0, True, False, f"unparseable position ({exc}) — fail-closed")

    allow = int(min(float(q), max(0.0, held)))
    if allow <= 0:
        return OversellResult(0, True, False,
                              f"live long qty {held:g} — refusing naked short")
    if allow < q:
        return OversellResult(allow, False, True,
                              f"capped {q} -> {allow} by live long qty")
    return OversellResult(allow, False, False, "")


# ────────────────────────────────────────────────────────────── kill switch

def drawdown_halt(equity: float, peak_equity: float, max_dd: float,
                  known: bool = True) -> tuple:
    """Should trading halt on drawdown? Returns (halt, drawdown, reason).

    FAILS CLOSED: `known=False` halts. v25's brake sat behind a bare
    `except: pass`, so an error silently disabled it — the exact inversion of
    what a brake is for (`dc04017`, `a0dd471`).
    """
    if not known:
        return True, 0.0, "equity history unknown — fail-closed halt"
    try:
        equity = float(equity)
        peak_equity = float(peak_equity)
        max_dd = float(max_dd)
    except (TypeError, ValueError) as exc:
        return True, 0.0, f"unparseable inputs ({exc}) — fail-closed halt"
    if peak_equity <= 0:
        return True, 0.0, "non-positive peak equity — fail-closed halt"
    dd = (peak_equity - equity) / peak_equity
    if dd >= max_dd:
        return True, dd, f"drawdown {dd:.1%} >= limit {max_dd:.1%}"
    return False, dd, ""
