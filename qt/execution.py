"""Execution — turning a target portfolio into orders. V29.

The model emits a target: twenty names, equal weight, at a quarterly
rebalance. This is the arithmetic between that and a list of orders.

PURE PLANNING ONLY. Nothing here talks to a broker, and that is deliberate:
the plan can then be tested against hand-computed answers, printed for review,
and diffed run to run, none of which is possible once order placement is
tangled into the same function. Placement is a separate, later, guarded step,
and entries remain switched off in any case.

FIVE THINGS THIS GETS RIGHT BECAUSE THE LIVE SYSTEM ONCE GOT THEM WRONG

1. **An empty position read is not a flat book.** On 2026-09-01 the broker
   returned zero positions, every guard believed it, and the system recorded
   "flat" while short. `plan_orders` REFUSES to plan against an untrusted
   read rather than helpfully liquidating a book it cannot see.

2. **Sells before buys.** Buying first needs settled cash the account does not
   have yet, so orders are emitted sells-first and the ordering is part of the
   contract, not an accident of iteration.

3. **Never a market order.** A market order in a stock trading $50,000 a day
   is how several percent disappears in one print. Limits only, priced off
   the quote, with the aggression declared.

4. **Participation is capped.** A position larger than a declared share of
   daily volume is split across days rather than shoved through in one.

5. **Whole shares.** Most of this universe has no fractional trading, so a
   target is floored, and the remainder stays in cash rather than becoming a
   rejected order.
"""
from __future__ import annotations

import math

import numpy as np

SELL, BUY = "sell", "buy"


def target_shares(equity: float, n_names: int, price: float,
                  gross: float = 1.0) -> int:
    """Whole shares for one equal-weighted sleeve. Floored, never rounded.

    Rounding up would overshoot the sleeve and, across twenty names, quietly
    lever the book above the intended gross.
    """
    for v in (equity, price):
        if v is None or not np.isfinite(v) or v <= 0:
            return 0
    if n_names <= 0 or gross <= 0:
        return 0
    return int(math.floor((float(equity) * float(gross) / int(n_names)) / float(price)))


def plan_orders(targets: dict, current: dict, prices: dict,
                position_read_ok: bool = True) -> list:
    """-> [{ticker, side, qty}], SELLS FIRST. Raises if the read is untrusted.

    `targets` is ticker -> desired shares, `current` is ticker -> held shares.
    A name held but absent from targets is fully sold.

    The refusal is the important part. Planning against a position read that
    might be wrong is how a book gets doubled or liquidated by accident, and
    the caller is forced to deal with it rather than being handed a plan that
    looks fine.
    """
    if not position_read_ok:
        raise ValueError(
            "position read is not trustworthy — refusing to plan orders. An "
            "empty or stale read must never be treated as a flat book (the "
            "2026-09-01 false-flat)")
    sells, buys = [], []
    for tk in sorted(set(targets) | set(current)):
        want = int(targets.get(tk, 0))
        have = int(current.get(tk, 0))
        delta = want - have
        if delta == 0:
            continue
        if tk not in prices or not np.isfinite(prices.get(tk, float("nan"))):
            continue                      # unpriceable: no order, not a guess
        (buys if delta > 0 else sells).append(
            {"ticker": tk, "side": BUY if delta > 0 else SELL, "qty": abs(delta)})
    return sells + buys


def participation_cap(qty: int, adv_shares: float, max_pct: float = 0.05) -> int:
    """Largest slice tradeable today at a declared share of daily volume."""
    if adv_shares is None or not np.isfinite(adv_shares) or adv_shares <= 0:
        return 0
    if max_pct <= 0:
        return 0
    return max(0, min(int(qty), int(math.floor(float(adv_shares) * float(max_pct)))))


def split_over_days(qty: int, adv_shares: float, max_pct: float = 0.05,
                    max_days: int = 5) -> list:
    """Slice a position into daily tranches under the participation cap.

    Returns fewer shares than requested when `max_days` binds. That shortfall
    is REAL -- it is the part of the position the market cannot absorb in the
    window -- and returning it short rather than padding the last day is what
    keeps the constraint honest.
    """
    per_day = participation_cap(qty, adv_shares, max_pct)
    if per_day <= 0:
        return []
    out, left = [], int(qty)
    while left > 0 and len(out) < int(max_days):
        take = min(per_day, left)
        out.append(take)
        left -= take
    return out


def limit_price(side: str, bid: float, ask: float, aggression: float = 0.0) -> float:
    """A limit that never crosses further than `aggression` of the spread.

    0.0 joins the near side and pays nothing away; 1.0 crosses fully and takes
    the far side. Anything between splits the spread. A quarterly rebalance can
    afford patience, so the default is the passive end.
    """
    if bid is None or ask is None:
        return float("nan")
    if not (np.isfinite(bid) and np.isfinite(ask)) or bid <= 0 or ask < bid:
        return float("nan")
    a = min(1.0, max(0.0, float(aggression)))
    if side == BUY:
        return float(bid + a * (ask - bid))
    if side == SELL:
        return float(ask - a * (ask - bid))
    raise ValueError(f"side must be {BUY!r} or {SELL!r}, got {side!r}")


def order_key(rebalance_date: str, ticker: str, side: str) -> str:
    """Idempotency key. Same rebalance, same name, same side -> same key.

    This repo has produced duplicate runs from catch-up crons three separate
    times. An order ledger keyed on this, first-write-wins, is what stops a
    repeated run from doubling the book.
    """
    return f"{str(rebalance_date)[:10]}|{str(ticker).upper()}|{str(side).lower()}"
