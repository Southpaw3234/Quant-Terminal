#!/usr/bin/env python3
"""Validate the qt package — v28 Phase 0. No network.

Every guard here exists because something went wrong in v25, so the tests are
written against the INCIDENTS rather than against the code:

  * a guard that cannot tell whether an action is safe must REFUSE it
  * a written ledger row must never move, even when a recompute disagrees
  * QT_MAX_GROSS=0 must block every BUY while leaving SELLs alone
  * a SELL beyond the live long quantity is a naked short, not a sale

The fail-closed cases are the important ones. v25's consecutive-loss brake sat
behind a bare `except: pass`, so an error silently DISABLED it — a guard that
fails open is worse than no guard, because it is trusted.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

from qt import guards, ledger

FAILURES: list = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)


# ──────────────────────────────────────────────────────────────── ledger

def test_ledger_freeze():
    print("\n--- ledger: first write wins, always ---")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "l.csv"
        v1 = pd.DataFrame({"date": ["2026-01-01", "2026-01-02"],
                           "value": [0.10, 0.20]})
        rep = ledger.write_ledger(v1, p, key="date")
        check("ledger-first-write", p.exists() and not rep.frozen,
              f"{len(v1)} rows written, nothing to freeze")

        # Same keys, DIFFERENT values -- the full-overwrite scenario that moved
        # a three-week-old row by 22% on 2026-08-10.
        v2 = pd.DataFrame({"date": ["2026-01-01", "2026-01-02", "2026-01-03"],
                           "value": [0.99, 0.88, 0.30]})
        rep = ledger.write_ledger(v2, p, key="date")
        got = pd.read_csv(p).set_index("date")["value"].to_dict()
        check("ledger-frozen", got["2026-01-01"] == 0.10 and got["2026-01-02"] == 0.20,
              f"written rows kept originals {got['2026-01-01']}, {got['2026-01-02']} "
              f"despite a recompute of 0.99, 0.88")
        check("ledger-appended", got.get("2026-01-03") == 0.30,
              "a genuinely new row was still appended")
        check("ledger-drift-reported", len(rep.frozen) == 2,
              f"drift REPORTED not discarded: {rep.frozen}")
        check("ledger-summary", "FROZE 2" in rep.summary("test"),
              "summary names the count so a caller can log it loudly")


def test_ledger_key_and_modes():
    print("\n--- ledger: configurable key, mutable escape, bad file ---")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "e.csv"
        a = pd.DataFrame({"event_id": ["x1"], "abn": [0.05]})
        ledger.write_ledger(a, p, key="event_id")
        b = pd.DataFrame({"event_id": ["x1"], "abn": [0.99]})
        ledger.write_ledger(b, p, key="event_id")
        val = pd.read_csv(p)["abn"].iloc[0]
        check("ledger-key", val == 0.05,
              f"event_id key works too ({val}) — one implementation, not two")

        os.environ["QT_TEST_MUTABLE"] = "1"
        _, rep = ledger.freeze_first_write(b, p, key="event_id",
                                           mutable_env="QT_TEST_MUTABLE")
        del os.environ["QT_TEST_MUTABLE"]
        check("ledger-mutable-flagged", rep.mutable and "MAY MOVE" in rep.summary(),
              "the mutable escape hatch announces itself so CI can refuse it")

        bad = Path(td) / "bad.csv"
        bad.write_text("this is not, a valid\ncsv\"\"\"row,,,\n")
        merged, rep2 = ledger.freeze_first_write(a, bad, key="event_id")
        check("ledger-bad-file", merged is not None,
              "an unreadable existing file degrades to writing fresh, not a crash")


# ──────────────────────────────────────────────────────────────── guards

def test_gross_cap():
    print("\n--- guards: gross cap ---")
    r = guards.gross_cap(equity=100_000, gross_mv=50_000, ratio=1.0,
                         slot_size=5_000)
    check("gross-room", r.room == 50_000 and r.slots == 10,
          f"room=${r.room:,.0f} slots={r.slots} lev={r.leverage:.2f}x")

    # The wind-down: QT_MAX_GROSS='0'. This is how entries went off on 8/09.
    r0 = guards.gross_cap(equity=116_590, gross_mv=2_781, ratio=0.0,
                          slot_size=5_000)
    check("gross-wind-down", r0.slots == 0 and r0.room == 0.0,
          f"ratio 0 with a live book -> {r0.slots} slots (entries permanently off)")

    # FAIL CLOSED
    rf = guards.gross_cap(equity=100_000, gross_mv=0, ratio=2.0,
                          slot_size=1_000, acct_ok=False)
    check("gross-fail-closed", rf.slots == 0 and not rf.ok,
          f"account read failed -> 0 slots ({rf.reason})")
    rn = guards.gross_cap(equity=None, gross_mv=0, ratio=2.0, slot_size=1_000)
    check("gross-garbage-fail-closed", rn.slots == 0,
          f"unparseable equity -> 0 slots ({rn.reason})")
    rz = guards.gross_cap(equity=0, gross_mv=0, ratio=2.0, slot_size=1_000)
    check("gross-zero-equity", rz.slots == 0,
          "zero equity -> 0 slots rather than a division that never happens")

    # Over-levered book: room must clamp at 0, never go negative into slots.
    ro = guards.gross_cap(equity=100_000, gross_mv=332_000, ratio=1.0,
                          slot_size=5_000)
    check("gross-over-levered", ro.room == 0.0 and ro.slots == 0,
          f"3.32x book (the 7/06 drive-sync incident) -> room ${ro.room:,.0f}, "
          f"{ro.slots} slots")


def test_sector_cap():
    print("\n--- guards: sector cap (fail-closed by DEFAULT, unlike v25) ---")
    exp = {"Industrials": 20_000}
    ok = guards.sector_cap_allows("Industrials", exp, {}, 5_000, 0.25, 100_000)
    check("sector-allows", ok, "20k + 5k <= 25% of 100k -> allowed")
    no = guards.sector_cap_allows("Industrials", exp, {}, 6_000, 0.25, 100_000)
    check("sector-blocks", not no, "20k + 6k > 25k -> blocked")

    pend = {"Industrials": 4_000}
    no2 = guards.sector_cap_allows("Industrials", exp, pend, 2_000, 0.25, 100_000)
    check("sector-pending", not no2,
          "pending budget counts — one run cannot fill a sector by ignoring "
          "what it already allocated this run")

    check("sector-fail-closed",
          not guards.sector_cap_allows("X", {}, {}, 1.0, 0.25, 100_000, ok=False),
          "sector read failed -> REFUSE (v28 default)")
    check("sector-v25-compat",
          guards.sector_cap_allows("X", {}, {}, 1.0, 0.25, 100_000, ok=False,
                                   fail_closed=False),
          "fail_closed=False reproduces v25, which allowed in this case")


def test_oversell():
    print("\n--- guards: oversell gate ---")
    r = guards.oversell_cap(100, live_qty=100)
    check("oversell-normal", r.allowed == 100 and not r.blocked,
          "selling exactly what is held is allowed")
    r2 = guards.oversell_cap(150, live_qty=100)
    check("oversell-capped", r2.allowed == 100 and r2.capped,
          f"150 -> {r2.allowed} ({r2.reason})")
    r3 = guards.oversell_cap(50, live_qty=100, already_sold=80)
    check("oversell-already-sold", r3.allowed == 20,
          f"20 left after 80 already sold this run -> {r3.allowed}")

    # The 2026-07-15 incident: this is the naked short.
    r4 = guards.oversell_cap(100, live_qty=0)
    check("oversell-naked-short", r4.allowed == 0 and r4.blocked,
          f"selling a name not held -> REFUSED ({r4.reason})")
    r5 = guards.oversell_cap(100, live_qty=-50)
    check("oversell-already-short", r5.allowed == 0 and r5.blocked,
          "selling while already short -> REFUSED, not doubled")

    r6 = guards.oversell_cap(100, live_qty=100, pos_ok=False)
    check("oversell-fail-closed", r6.allowed == 0 and r6.blocked,
          f"positions unknown -> REFUSED ({r6.reason})")
    r7 = guards.oversell_cap("banana", live_qty=100)
    check("oversell-garbage", r7.allowed == 0 and r7.blocked,
          "unparseable qty -> REFUSED rather than raising")
    r8 = guards.oversell_cap(100, live_qty=100, enforce=False)
    check("oversell-local-paper", r8.allowed == 100,
          "enforcement off (no broker, no short possible) -> allowed")


def test_drawdown():
    print("\n--- guards: drawdown halt ---")
    halt, dd, _ = guards.drawdown_halt(90_000, 100_000, 0.15)
    check("dd-under", not halt and abs(dd - 0.10) < 1e-9,
          f"10% drawdown under a 15% limit -> no halt")
    halt2, dd2, why = guards.drawdown_halt(80_000, 100_000, 0.15)
    check("dd-over", halt2, f"20% drawdown -> HALT ({why})")

    # The inversion v25 shipped: an error DISABLED the brake.
    halt3, _, why3 = guards.drawdown_halt(90_000, 100_000, 0.15, known=False)
    check("dd-fail-closed", halt3,
          f"unknown equity history -> HALT ({why3}) — v25's bare `except: pass` "
          f"silently disabled this instead")
    halt4, _, _ = guards.drawdown_halt("x", 100_000, 0.15)
    check("dd-garbage", halt4, "unparseable input -> HALT, not a silent pass")


def main():
    print("=" * 70)
    print("qt package — Phase 0 validation (no network)")
    print("=" * 70)
    test_ledger_freeze()
    test_ledger_key_and_modes()
    test_gross_cap()
    test_sector_cap()
    test_oversell()
    test_drawdown()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED -> {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    print("Every guard refuses when it cannot determine safety, and a written")
    print("ledger row does not move even when a recompute disagrees.")


if __name__ == "__main__":
    main()
