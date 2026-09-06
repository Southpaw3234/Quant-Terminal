#!/usr/bin/env python3
"""Validation for qt.execution and qt.reconcile — V29. No network, no broker.

The execution planner is checked hardest on the things the LIVE system already
got wrong once, because those are not hypothetical failure modes here:

  * an empty position read treated as a flat book (2026-09-01);
  * buys emitted before sells, needing cash that has not settled;
  * a repeated run doubling the book, which catch-up crons caused three times.

The reconciliation is checked on the one thing that makes a cost number
meaningless if reversed: a buy filled high and a sell filled low are BOTH
costs, and averaging them with opposite signs would make an expensive book
look free.
"""
from __future__ import annotations

import sys

import numpy as np

from qt import execution as ex
from qt import reconcile as rc

FAILURES = []


def check(name, cond, msg=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{': ' + msg if msg else ''}")
    if not cond:
        FAILURES.append(name)


def test_sizing():
    print("\n--- sizing a sleeve ---")
    check("floors-never-rounds",
          ex.target_shares(equity=10_000, n_names=20, price=7.0) == 71,
          "$10,000 / 20 = $500 a sleeve; $500 / $7 = 71.4 -> 71 shares. Rounding "
          "UP would overshoot every sleeve and quietly lever the book")
    check("gross-scales",
          ex.target_shares(10_000, 20, 7.0, gross=0.5) == 35,
          "half gross halves the sleeve")
    check("bad-inputs-give-zero",
          ex.target_shares(0, 20, 7.0) == 0 and ex.target_shares(10_000, 20, 0) == 0
          and ex.target_shares(10_000, 0, 7.0) == 0,
          "no equity, no price or no names yields zero shares, not an exception")
    check("sub-share-sleeve-is-zero",
          ex.target_shares(10_000, 20, 900.0) == 0,
          "a $500 sleeve cannot buy a $900 share, and the remainder stays in cash "
          "rather than becoming a rejected fractional order")


def test_planning():
    print("\n--- planning orders ---")
    prices = {"AAA": 10.0, "BBB": 5.0, "CCC": 2.0}
    plan = ex.plan_orders(targets={"AAA": 100, "CCC": 50},
                          current={"AAA": 40, "BBB": 30}, prices=prices)
    sides = [o["side"] for o in plan]
    check("sells-come-first",
          sides.index(ex.SELL) < sides.index(ex.BUY),
          f"order of sides is {sides} — buying before the sells settle needs cash "
          f"the account does not have")
    check("exits-a-dropped-name",
          any(o["ticker"] == "BBB" and o["side"] == ex.SELL and o["qty"] == 30
              for o in plan),
          "a name held but absent from the target is fully sold")
    check("tops-up-a-kept-name",
          any(o["ticker"] == "AAA" and o["side"] == ex.BUY and o["qty"] == 60
              for o in plan),
          "40 held against a target of 100 buys 60, not 100")
    check("no-order-when-already-correct",
          ex.plan_orders({"AAA": 40}, {"AAA": 40}, prices) == [],
          "a position already at target generates nothing")
    check("unpriceable-name-skipped",
          ex.plan_orders({"ZZZ": 10}, {}, prices) == [],
          "a name with no price produces no order rather than a guessed one")

    raised = False
    try:
        ex.plan_orders({"AAA": 10}, {}, prices, position_read_ok=False)
    except ValueError as exc:
        raised = "false-flat" in str(exc) or "not trustworthy" in str(exc)
    check("refuses-an-untrusted-position-read", raised,
          "THE 2026-09-01 FAILURE: the broker returned zero positions, every "
          "guard believed it, and the system recorded flat while short. Planning "
          "against that read is refused outright")


def test_participation():
    print("\n--- participation and slicing ---")
    check("caps-at-declared-share",
          ex.participation_cap(10_000, adv_shares=100_000, max_pct=0.05) == 5_000,
          "5% of 100,000 shares a day is 5,000")
    check("cap-never-exceeds-the-order",
          ex.participation_cap(100, adv_shares=100_000, max_pct=0.05) == 100,
          "a small order is not inflated to the cap")
    check("no-volume-no-trade",
          ex.participation_cap(1_000, adv_shares=0) == 0,
          "a name with no volume yields no tradeable size")
    slices = ex.split_over_days(12_000, adv_shares=100_000, max_pct=0.05, max_days=5)
    check("splits-across-days",
          slices == [5_000, 5_000, 2_000],
          f"12,000 shares at 5,000 a day is {slices}")
    short = ex.split_over_days(100_000, adv_shares=100_000, max_pct=0.05, max_days=3)
    check("shortfall-is-returned-short",
          sum(short) == 15_000,
          f"three days at 5,000 fills only {sum(short):,} of 100,000. The gap is "
          f"REAL — the market cannot absorb the rest in the window — and padding "
          f"the last day would hide the constraint")


def test_limits():
    print("\n--- limit prices ---")
    check("passive-buy-joins-the-bid",
          ex.limit_price(ex.BUY, bid=10.0, ask=10.5, aggression=0.0) == 10.0,
          "aggression 0 pays nothing away")
    check("aggressive-buy-crosses",
          ex.limit_price(ex.BUY, 10.0, 10.5, aggression=1.0) == 10.5,
          "aggression 1 takes the offer")
    check("midpoint-splits",
          ex.limit_price(ex.BUY, 10.0, 10.5, aggression=0.5) == 10.25,
          "half the spread")
    check("passive-sell-joins-the-ask",
          ex.limit_price(ex.SELL, 10.0, 10.5, aggression=0.0) == 10.5,
          "the sell side mirrors")
    check("crossed-book-is-nan",
          not np.isfinite(ex.limit_price(ex.BUY, 10.5, 10.0)),
          "an ask below the bid yields NaN rather than a nonsense limit")
    check("aggression-is-clamped",
          ex.limit_price(ex.BUY, 10.0, 10.5, aggression=5.0) == 10.5,
          "aggression above 1 cannot walk the book past the offer")


def test_idempotency():
    print("\n--- idempotency ---")
    a = ex.order_key("2025-03-31", "aaa", "BUY")
    b = ex.order_key("2025-03-31", "AAA", "buy")
    check("key-is-stable-across-case", a == b, f"{a}")
    check("key-separates-sides",
          ex.order_key("2025-03-31", "AAA", "sell") != a,
          "a buy and a sell of the same name on the same day are different orders")
    check("key-separates-rebalances",
          ex.order_key("2025-06-30", "AAA", "buy") != a,
          "the same name next quarter is a new order — this is what stops a "
          "repeated run doubling the book, which catch-up crons caused three times")


def test_shortfall():
    print("\n--- implementation shortfall ---")
    check("buy-filled-high-is-a-cost",
          abs(rc.implementation_shortfall_bps(10.0, 10.10, rc.BUY) - 100.0) < 1e-9,
          "paying 10.10 against a 10.00 decision is +100 bps of cost")
    check("sell-filled-low-is-also-a-cost",
          abs(rc.implementation_shortfall_bps(10.0, 9.90, rc.SELL) - 100.0) < 1e-9,
          "THE SIGN TRAP: receiving 9.90 against a 10.00 decision is ALSO +100 bps. "
          "Signing these the same way is what lets them be averaged; signing them "
          "opposite would make an expensive book look free")
    check("price-improvement-is-negative",
          rc.implementation_shortfall_bps(10.0, 9.95, rc.BUY) < 0,
          "buying below the decision price is a gain, reported negative")
    s = rc.summarize_fills([
        {"decision_price": 10.0, "fill_price": 10.10, "side": rc.BUY},
        {"decision_price": 10.0, "fill_price": 9.90, "side": rc.SELL},
    ])
    check("both-sides-average-as-costs",
          abs(s["mean_bps"] - 100.0) < 1e-9 and s["n"] == 2,
          f"a +100 bps buy and a +100 bps sell average to {s['mean_bps']:.0f} bps, "
          f"not to zero")
    check("empty-fills-safe",
          rc.summarize_fills([])["n"] == 0,
          "no fills yields zeros rather than an exception")


def test_completion_and_divergence():
    print("\n--- completion and divergence ---")
    c = rc.completion_rate({"A": 100, "B": 100}, {"A": 100, "B": 5})
    check("partial-fill-counts-as-partial",
          abs(c["share_filled"] - 0.525) < 1e-9 and c["unfilled_names"] == ["B"],
          f"105 of 200 shares is {c['share_filled']:.1%}. A name filled 5% is NOT "
          f"a name the strategy owns, and counting names rather than shares would "
          f"call this 50% complete")
    check("full-fill-is-one",
          rc.completion_rate({"A": 10}, {"A": 10})["share_filled"] == 1.0,
          "everything filled is 100%")
    v = rc.cost_vs_assumption(250.0, 75.0)
    check("flags-a-wrong-assumption",
          v["verdict"] == "assumption wrong by a multiple" and "SIGNED" in v["note"],
          f"250 bps against 75 declared is {v['ratio']:.1f}x — flagged, and the "
          f"note says the parameter does not move")
    check("accepts-a-close-assumption",
          rc.cost_vs_assumption(80.0, 75.0)["verdict"] == "assumption holds",
          "80 against 75 is within tolerance")
    d = rc.return_divergence([0.01, 0.01, 0.01], [0.01, 0.01, 0.01])
    check("identical-series-do-not-diverge",
          d["within_tolerance"] and abs(d["mean_bps"]) < 1e-9,
          "a matching backtest and live series diverge by nothing")
    d2 = rc.return_divergence([0.02, 0.01], [0.01, 0.01], tolerance_bps=25)
    check("a-real-gap-is-flagged",
          not d2["within_tolerance"] and d2["days_over_tolerance"] == 1,
          "100 bps on one day exceeds a 25 bps tolerance — the shape a bug makes "
          "on days the book did not trade")


def main():
    print("=" * 70)
    print("qt.execution + qt.reconcile — V29 (no network, no broker)")
    print("=" * 70)
    test_sizing()
    test_planning()
    test_participation()
    test_limits()
    test_idempotency()
    test_shortfall()
    test_completion_and_divergence()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("An untrusted position read is refused; sells precede buys; a")
    print("repeated run cannot double the book; and a buy filled high and a")
    print("sell filled low are both counted as costs.")


if __name__ == "__main__":
    main()
