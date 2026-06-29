#!/usr/bin/env python3
"""
shadow_stat_arb.py — Frame 3 stat-arb SHADOW trading layer (P0)
==============================================================
Forward-only PAPER book that consumes the stat-arb scanner's daily signals and
produces a gate-readable market-neutral return series. This is the *trading*
layer the scanner (`stat_arb.py`) lacks: it maintains position state, marks the
book to market each day, applies entries/exits (incl. stops), charges costs, and
appends the book's daily net return to a CSV the Stage-1 GO/NO-GO gate can read.

IT IS SHADOW ONLY. It places NO Alpaca orders and touches NO live-model state.
It mirrors the Frame-1 cross-sectional shadow harness so a *second* evidence
clock accumulates in parallel toward the same mid-August alpha gate.

WHY FORWARD-ONLY (not a backtest): `stat_arb.generate_signals` z-scores each day
against the FULL-window spread mean/std (look-ahead), and `pairs.json` is the set
cointegrated *as of today* (survivorship). Re-running history would be silently
optimistic on both counts. So we start the paper book today and accumulate a
genuine out-of-sample track, exactly like Frame 1.

Inputs  (written by stat_arb.py each morning):
  data/stat_arb/signals.json   — today's per-pair z-score + ENTER/EXIT/HOLD action
  data/stat_arb/pairs.json     — today's cointegrated set (for de-cointegration exit)

State / outputs:
  data/stat_arb/shadow_positions.json  — open paper book (per-pair position state)
  data/stat_arb/stat_arb_ls.csv        — daily book net return series (gate input)
  data/stat_arb/shadow_trades.csv      — per-trade close log (audit)

Usage:
  python shadow_stat_arb.py            # run one daily book update (idempotent per date)
  python shadow_stat_arb.py --dry-run  # compute + print, write nothing
"""

import sys
import json
import argparse
import datetime
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

# ── Config — LOCKED defaults (user-confirmed 2026-06-29) ───────────────────────
STAT_ARB_DIR     = Path("data/stat_arb")
SIGNALS_FILE     = STAT_ARB_DIR / "signals.json"
PAIRS_FILE       = STAT_ARB_DIR / "pairs.json"
POSITIONS_FILE   = STAT_ARB_DIR / "shadow_positions.json"
LS_CSV           = STAT_ARB_DIR / "stat_arb_ls.csv"
TRADES_CSV       = STAT_ARB_DIR / "shadow_trades.csv"

NOTIONAL_PER_LEG = 10_000.0   # $ per leg; gross per pair = 2× (dollar-neutral)
MAX_CONCURRENT   = 8          # cap on simultaneously open pairs
COST_BPS         = 5.0        # per leg, charged on BOTH open and close
ENTRY_Z          = 2.0        # |z| at/above which a pair is entered (mirrors scanner)
EXIT_Z           = 0.5        # |z| at/below which a pair reverts out
STOP_Z           = 3.5        # |z| blowout → assume de-cointegrated, cut the pair
TIME_STOP_HL_X   = 3.0        # exit if days_held > this × the pair's half-life

# Book notional base for the return series denominator (fixed so the daily return
# is comparable across days regardless of how many pairs are open).
BOOK_BASE        = MAX_CONCURRENT * NOTIONAL_PER_LEG   # $80,000

COST_RATE        = COST_BPS / 1e4
TODAY            = datetime.date.today().isoformat()

STAT_ARB_DIR.mkdir(parents=True, exist_ok=True)


# ── Price loading ──────────────────────────────────────────────────────────────
def load_last_closes(tickers: list[str]) -> dict[str, float]:
    """Most-recent close per ticker via yfinance. Returns {} on failure (the run
    then degrades gracefully: it skips mark-to-market but still records state)."""
    tickers = sorted(set(t for t in tickers if t))
    if not tickers:
        return {}
    try:
        import yfinance as yf
        raw = yf.download(tickers, period="5d", auto_adjust=True,
                          progress=False, threads=True)
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"] if "Close" in raw else raw.xs("Close", axis=1, level=0)
        else:
            close = raw["Close"] if "Close" in raw else raw
        if isinstance(close, pd.Series):           # single ticker → Series
            close = close.to_frame(tickers[0])
        out = {}
        for tk in tickers:
            if tk in close.columns:
                s = close[tk].dropna()
                if len(s):
                    out[tk] = float(s.iloc[-1])
        return out
    except Exception as e:
        print(f"  [shadow_sa] price load error: {e}")
        return {}


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ── Book update ────────────────────────────────────────────────────────────────
def update_book(signals: list[dict], pairs: list[dict],
                positions: dict, prices: dict) -> tuple[dict, dict, list[dict]]:
    """Advance the paper book by one day. Returns (positions, day_stats, closed)."""
    sig_by_pair  = {s["pair"]: s for s in signals}
    live_pairs   = {f"{p['y']}/{p['x']}" for p in pairs}

    gross_pnl = 0.0       # mark-to-market $ across open book (pre-cost)
    cost      = 0.0       # transaction $ charged today
    closed    = []        # per-trade close records for the audit log

    # ── 1) mark existing positions to market, then decide exits ──────────────
    for pair, pos in list(positions.items()):
        y, x, side = pos["y"], pos["x"], pos["side"]
        py, px = prices.get(y), prices.get(x)

        if py is not None and px is not None and pos.get("last_y") and pos.get("last_x"):
            ry = py / pos["last_y"] - 1.0
            rx = px / pos["last_x"] - 1.0
            pnl = side * NOTIONAL_PER_LEG * (ry - rx)   # dollar-neutral pair P&L
            gross_pnl += pnl
            pos["cum_pnl"] = pos.get("cum_pnl", 0.0) + pnl
        # roll the mark forward (so a missing price day doesn't double-count later)
        if py is not None:
            pos["last_y"] = py
        if px is not None:
            pos["last_x"] = px
        pos["days_held"] = pos.get("days_held", 0) + 1

        sig = sig_by_pair.get(pair)
        z   = abs(sig["z_score"]) if sig else None
        hl  = pos.get("half_life", 0) or 0
        reason = None
        if pair not in live_pairs:
            reason = "decointegrated"
        elif z is not None and z >= STOP_Z:
            reason = "stop_blowout"
        elif z is not None and z <= EXIT_Z:
            reason = "reversion"
        elif hl and pos["days_held"] > TIME_STOP_HL_X * hl:
            reason = "time_stop"

        if reason:
            cost += 2 * COST_RATE * NOTIONAL_PER_LEG   # close both legs
            closed.append({"date": TODAY, "pair": pair, "side": side,
                           "days_held": pos["days_held"],
                           "cum_pnl": round(pos.get("cum_pnl", 0.0), 2),
                           "exit_reason": reason})
            del positions[pair]

    # ── 2) open new entries from today's ENTER_* signals (capacity-capped) ────
    n_entries = 0
    for s in signals:
        if len(positions) >= MAX_CONCURRENT:
            break
        pair, action = s["pair"], s["action"]
        if pair in positions or action not in ("ENTER_LONG_Y", "ENTER_SHORT_Y"):
            continue
        y, x = s["y"], s["x"]
        py, px = prices.get(y), prices.get(x)
        if py is None or px is None:
            continue  # can't mark a leg we have no price for → skip the entry
        side = 1 if action == "ENTER_LONG_Y" else -1   # +1 long Y/short X
        positions[pair] = {
            "y": y, "x": x, "side": side,
            "entry_date": TODAY, "entry_z": s["z_score"],
            "half_life": s.get("half_life", 0), "hedge_ratio": s.get("hedge_ratio", 0),
            "last_y": py, "last_x": px, "days_held": 0, "cum_pnl": 0.0,
        }
        cost += 2 * COST_RATE * NOTIONAL_PER_LEG       # open both legs
        n_entries += 1

    net_pnl   = gross_pnl - cost
    day_stats = {
        "date": TODAY,
        "book_return": round(net_pnl / BOOK_BASE, 8),
        "gross_pnl": round(gross_pnl, 2),
        "cost": round(cost, 2),
        "net_pnl": round(net_pnl, 2),
        "n_open": len(positions),
        "n_entries": n_entries,
        "n_exits": len(closed),
    }
    return positions, day_stats, closed


# ── CSV append helpers (idempotent on date) ────────────────────────────────────
def append_row(path: Path, row: dict):
    df_new = pd.DataFrame([row])
    if path.exists() and path.stat().st_size > 0:
        df_old = pd.read_csv(path)
        if "date" in df_old.columns:
            df_old = df_old[df_old["date"].astype(str) != row.get("date", "")]
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_all = df_new
    df_all.to_csv(path, index=False)


def append_trades(path: Path, closed: list[dict]):
    if not closed:
        return
    df_new = pd.DataFrame(closed)
    if path.exists() and path.stat().st_size > 0:
        df_new = pd.concat([pd.read_csv(path), df_new], ignore_index=True)
    df_new.to_csv(path, index=False)


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Frame 3 stat-arb shadow book (P0)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + print, write nothing")
    args = ap.parse_args()

    print(f"\n{'='*55}\nSHADOW STAT-ARB BOOK -- {TODAY}\n{'='*55}")

    signals = _load_json(SIGNALS_FILE, [])
    pairs   = _load_json(PAIRS_FILE, [])
    if not signals:
        print(f"  no signals at {SIGNALS_FILE} — run stat_arb.py first. Exiting.")
        return
    positions = _load_json(POSITIONS_FILE, {})
    print(f"  loaded {len(signals)} signals, {len(pairs)} live pairs, "
          f"{len(positions)} open positions")

    # Idempotency: don't double-advance the book if already run today.
    last = None
    if LS_CSV.exists() and LS_CSV.stat().st_size > 0:
        prev = pd.read_csv(LS_CSV)
        if len(prev):
            last = str(prev["date"].iloc[-1])
    if last == TODAY and not args.dry_run:
        print(f"  book already advanced for {TODAY} (last row in {LS_CSV.name}). "
              f"Re-running would double-count — skipping. Use --dry-run to inspect.")
        return

    tickers = [s["y"] for s in signals] + [s["x"] for s in signals] \
            + [p["y"] for p in positions.values()] + [p["x"] for p in positions.values()]
    prices = load_last_closes(tickers)
    print(f"  marked {len(prices)}/{len(set(tickers))} tickers to market")

    positions, day, closed = update_book(signals, pairs, positions, prices)

    print(f"  -> entries {day['n_entries']}  exits {day['n_exits']}  "
          f"open {day['n_open']}")
    print(f"  -> gross ${day['gross_pnl']:+,.2f}  cost ${day['cost']:,.2f}  "
          f"net ${day['net_pnl']:+,.2f}  book_return {day['book_return']:+.4%}")
    for c in closed:
        print(f"    EXIT {c['pair']:12s} {c['exit_reason']:14s} "
              f"held {c['days_held']}d  pnl ${c['cum_pnl']:+,.2f}")

    if args.dry_run:
        print("  [dry-run] nothing written.")
        return

    POSITIONS_FILE.write_text(json.dumps(positions, indent=2))
    append_row(LS_CSV, day)
    append_trades(TRADES_CSV, closed)
    print(f"  wrote {POSITIONS_FILE.name}, {LS_CSV.name}"
          + (f", {TRADES_CSV.name}" if closed else ""))
    print(f"{'='*55}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
