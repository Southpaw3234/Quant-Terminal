"""
cancel_open_buys.py — one-off cancel of open BUY orders (2026-07-07 incident).

The Windows Task Scheduler catch-up fired a stale run_type=morning dispatch at
18:31 UTC (PC-wake, see run_logs/trigger.log), which bypassed the marker gate
and ran a full duplicate morning cycle. That run submitted ~26 duplicate BUY
orders (~$143k of logged intents) at 20:33-20:38 UTC — after the close — so
any that Alpaca ACCEPTED sit queued to fill at the next 9:30 ET open and would
re-lever the account the 7/6 de-lever just fixed.

This tool lists ALL open orders, then cancels the open BUYs (sells are left
alone — the model's own queued exits de-lever). CANCEL_MODE=dry-run (default)
prints the plan and cancels NOTHING; CANCEL_MODE=execute cancels.

Run via the "Cancel Open BUY Orders (one-off remediation)" workflow_dispatch.
"""
import json
import os
import sys
import urllib.request


def _clean(v):
    # The repo secrets carry a UTF-8 BOM from the original PowerShell
    # `$val | gh secret set` pipe; urllib rejects it in header values.
    return v.strip().strip("\ufeff").strip()


BASE = _clean(os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")).rstrip("/")
KEY = _clean(os.environ["ALPACA_API_KEY"])
SEC = _clean(os.environ["ALPACA_SECRET_KEY"])
MODE = os.environ.get("CANCEL_MODE", "dry-run").strip().lower()


def api(path, method="GET"):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    req.add_header("APCA-API-KEY-ID", KEY)
    req.add_header("APCA-API-SECRET-KEY", SEC)
    try:
        with urllib.request.urlopen(req) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        print(f"  !! {method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")
        raise


def main():
    print(f"MODE={MODE}  BASE={BASE}")
    if MODE not in ("dry-run", "execute"):
        print(f"Unknown CANCEL_MODE '{MODE}' — refusing.")
        return 1

    acct = api("/v2/account")
    print(f"Account: equity=${float(acct['equity']):,.2f}  "
          f"cash=${float(acct['cash']):,.2f}  "
          f"buying_power=${float(acct['buying_power']):,.2f}")

    orders = api("/v2/orders?status=open&limit=500") or []
    print(f"\nOpen orders: {len(orders)}")
    print(f"  {'side':4s} {'sym':8s} {'qty':>8s} {'type':8s} {'tif':4s} "
          f"{'status':14s} submitted_at")
    buys, buy_notional = [], 0.0
    for o in sorted(orders, key=lambda x: (x["side"], x["symbol"])):
        print(f"  {o['side']:4s} {o['symbol']:8s} {float(o['qty']):8.0f} "
              f"{o['type']:8s} {o['time_in_force']:4s} {o['status']:14s} "
              f"{o['submitted_at']}")
        if o["side"] == "buy":
            buys.append(o)

    if not buys:
        print("\nNo open BUY orders — nothing to cancel.")
        return 0

    print(f"\n── Plan: cancel {len(buys)} open BUY orders "
          f"(SELLs untouched — they de-lever) ──")
    if MODE == "dry-run":
        print("DRY-RUN — nothing cancelled. Re-dispatch with mode=execute.")
        return 0

    print("\nEXECUTING…")
    ok = 0
    for o in buys:
        try:
            api(f"/v2/orders/{o['id']}", method="DELETE")
            print(f"  CANCELLED {o['symbol']} x{float(o['qty']):.0f} (id {o['id'][:8]}…)")
            ok += 1
        except urllib.error.HTTPError:
            print(f"  !! failed to cancel {o['symbol']} (id {o['id'][:8]}…) — check manually")
    remaining = api("/v2/orders?status=open&limit=500") or []
    still_buy = [o for o in remaining if o["side"] == "buy"]
    print(f"\nDone — cancelled {ok}/{len(buys)}. "
          f"Open orders now: {len(remaining)} ({len(still_buy)} BUYs remain).")
    return 1 if still_buy else 0


if __name__ == "__main__":
    sys.exit(main())
