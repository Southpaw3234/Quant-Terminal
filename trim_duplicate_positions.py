"""
trim_duplicate_positions.py — one-off remediation for the 2026-07-06
duplicate-retrain order flow (see HANDOFF.md ledger 2026-07-06 and fix 497f277).

The Drive-sync stale-marker bug made three FULL morning retrains run on
2026-07-06, each placing its own BUY batch. Batch 1 (13:35 UTC dispatch) is the
intended order flow; batches 2 (16:52 run) and 3 (18:09 run, submitted AFTER
the 20:00 UTC close, so likely still queued) are bug residue.

Remediation, reconciled against Alpaca's own records (not the run logs):
  1. CANCEL every open (unfilled) equity BUY order — the designed end-of-day
     state has no pending orders, so any open BUY right now is bug residue.
  2. SELL the excess of today's FILLED buys over the intended batch-1
     quantities, per ticker, capped at the currently held quantity.

Scope guardrails:
  - Only tickers bought by today's runs are ever touched (TRIM_SCOPE).
  - Crypto is never touched.
  - Never submits a BUY. Never sells more than currently held.
  - MODE=dry-run (default) prints the full plan and submits NOTHING.
    MODE=execute performs the cancels + sells (market, DAY — queue for open).

Run via the "Position Trim (one-off remediation)" workflow_dispatch.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
KEY = os.environ["ALPACA_API_KEY"]
SEC = os.environ["ALPACA_SECRET_KEY"]
MODE = os.environ.get("TRIM_MODE", "dry-run").strip().lower()
TRADE_DAY = os.environ.get("TRIM_DAY", "2026-07-06")

# Intended order flow = batch 1 fills (run 28795559115, 13:35 UTC dispatch).
INTENDED = {
    "JPM": 23, "USB": 130, "ABT": 83, "VRTX": 19, "IQV": 51, "BAX": 452,
    "BKNG": 54, "DG": 86, "DLTR": 68, "EXPE": 38, "HRL": 374, "INTC": 58,
    "AMAT": 14, "TER": 20, "GE": 27, "ITW": 36, "PPG": 82, "WELL": 43,
    "VTR": 66, "TTWO": 40, "SOXX": 9,
}
# Every ticker any of today's three batches bought (union) — nothing outside
# this set is ever cancelled or sold.
TRIM_SCOPE = set(INTENDED) | {
    "CAT", "CMCSA", "LYV",                                      # batch 2 extras
    "PNC", "LLY", "DHR", "SYK", "ZBH", "NCLH", "TDG",           # batch 3 extras
}


def api(path, method="GET", body=None):
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("APCA-API-KEY-ID", KEY)
    req.add_header("APCA-API-SECRET-KEY", SEC)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        print(f"  !! {method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")
        raise


def main():
    print(f"MODE={MODE}  TRADE_DAY={TRADE_DAY}  BASE={BASE}")
    if MODE not in ("dry-run", "execute"):
        print(f"Unknown TRIM_MODE '{MODE}' — refusing.")
        return 1

    acct = api("/v2/account")
    print(f"Account: equity=${float(acct['equity']):,.2f}  "
          f"cash=${float(acct['cash']):,.2f}  "
          f"buying_power=${float(acct['buying_power']):,.2f}")

    # ── 1. Open (unfilled) BUY orders in scope → cancel ─────────────────────
    open_orders = api("/v2/orders?status=open&limit=500")
    cancels = [o for o in open_orders
               if o["side"] == "buy" and o["symbol"] in TRIM_SCOPE]
    skipped_open = [o for o in open_orders if o not in cancels]
    print(f"\n── Open orders: {len(open_orders)} total, "
          f"{len(cancels)} in-scope BUYs to cancel ──")
    for o in cancels:
        print(f"  CANCEL {o['symbol']:6s} BUY x{o['qty']}  "
              f"(submitted {o['submitted_at']}, id {o['id'][:8]}…)")
    for o in skipped_open:
        print(f"  leave  {o['symbol']:6s} {o['side'].upper()} x{o['qty']} "
              f"(out of scope)")

    # ── 2. Today's FILLED buys per ticker (ground truth from Alpaca) ────────
    after = f"{TRADE_DAY}T00:00:00Z"
    q = urllib.parse.urlencode({"status": "closed", "after": after,
                                "limit": 500, "direction": "asc"})
    closed = api(f"/v2/orders?{q}")
    filled_today = {}
    for o in closed:
        if o["side"] != "buy" or o["symbol"] not in TRIM_SCOPE:
            continue
        fq = float(o.get("filled_qty") or 0)
        if fq > 0 and (o.get("filled_at") or "").startswith(TRADE_DAY):
            filled_today[o["symbol"]] = filled_today.get(o["symbol"], 0) + fq

    positions = {p["symbol"]: float(p["qty"]) for p in api("/v2/positions")}

    print(f"\n── Filled buys on {TRADE_DAY} vs intended (batch 1) ──")
    print(f"  {'sym':6s} {'filled':>7s} {'intend':>7s} {'excess':>7s} "
          f"{'held':>8s} {'sell':>7s}")
    sells = []
    for sym in sorted(filled_today):
        filled = filled_today[sym]
        intended = INTENDED.get(sym, 0)
        excess = max(0, filled - intended)
        held = positions.get(sym, 0)
        sell_qty = int(min(excess, held))
        print(f"  {sym:6s} {filled:7.0f} {intended:7d} {excess:7.0f} "
              f"{held:8.0f} {sell_qty:7d}")
        if sell_qty > 0:
            sells.append((sym, sell_qty))

    print(f"\n── Plan: cancel {len(cancels)} open BUYs, "
          f"submit {len(sells)} market SELLs (DAY, queue for next open) ──")
    if MODE == "dry-run":
        print("DRY-RUN — nothing submitted. Re-dispatch with mode=execute "
              "to perform the above.")
        return 0

    print("\nEXECUTING…")
    for o in cancels:
        api(f"/v2/orders/{o['id']}", method="DELETE")
        print(f"  cancelled {o['symbol']} BUY x{o['qty']}")
    for sym, qty in sells:
        r = api("/v2/orders", method="POST", body={
            "symbol": sym, "qty": str(qty), "side": "sell",
            "type": "market", "time_in_force": "day",
        })
        print(f"  SELL {sym} x{qty} submitted (id {r['id'][:8]}…)")
    print("Done. Verify positions after next market open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
