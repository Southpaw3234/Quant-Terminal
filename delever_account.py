"""
delever_account.py — one-off de-lever for the duplicate-retrain leverage
(2026-07-06 diagnosis; Drive-sync stale-marker bug fixed in 497f277 — see
HANDOFF.md ledger 2026-07-06).

Duplicate morning retrains (2-3/day since ~6/30) kept re-submitting BUY
batches until the paper account hit its margin ceiling: at 2026-07-06 EOD it
held ~$342k gross long on ~$117.9k equity (cash -$224k, buying power ~$1.1k),
so the model's own intended entries were being rejected and the drawdown kill
switch was reading ~3x-beta equity swings.

This tool pro-rata sells LONG positions in ONE sleeve (TRIM_SLEEVE=equity
default: US equities, crypto untouched; TRIM_SLEEVE=crypto: crypto only,
equities untouched — added 7/15 when the crypto sleeve at ~0.71x equity became
the cap consumer, see HANDOFF 7/15) until projected gross exposure ~= equity *
TARGET_RATIO (default 1.0), netting out SELL orders the model itself already
has queued. Guardrails: the other sleeve untouched, shorts untouched, never
submits a BUY, never sells more than held-minus-queued.
TRIM_MODE=dry-run (default) prints the full plan and submits NOTHING;
TRIM_MODE=execute submits market sells (equity: DAY, fills at next open if the
market is closed; crypto: GTC fractional, fills ~immediately 24/7).

Run via the "Position Trim (one-off remediation)" workflow_dispatch.
"""
import json
import math
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
MODE = os.environ.get("TRIM_MODE", "dry-run").strip().lower()
TARGET_RATIO = float(os.environ.get("TRIM_TARGET_RATIO", "1.0"))
SLEEVE = os.environ.get("TRIM_SLEEVE", "equity").strip().lower()


def api(path, method="GET", body=None):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
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
    print(f"MODE={MODE}  TARGET_RATIO={TARGET_RATIO}  SLEEVE={SLEEVE}  BASE={BASE}")
    if MODE not in ("dry-run", "execute"):
        print(f"Unknown TRIM_MODE '{MODE}' — refusing.")
        return 1
    if SLEEVE not in ("equity", "crypto"):
        print(f"Unknown TRIM_SLEEVE '{SLEEVE}' — refusing.")
        return 1
    sleeve_class = "us_equity" if SLEEVE == "equity" else "crypto"

    acct = api("/v2/account")
    equity = float(acct["equity"])
    cash = float(acct["cash"])
    print(f"Account: equity=${equity:,.2f}  cash=${cash:,.2f}  "
          f"buying_power=${float(acct['buying_power']):,.2f}")

    positions = api("/v2/positions")
    longs_eq = [p for p in positions
                if p["side"] == "long" and p.get("asset_class") == sleeve_class]
    other = [p for p in positions if p not in longs_eq]
    gross_eq = sum(float(p["market_value"]) for p in longs_eq)
    gross_other = sum(abs(float(p["market_value"])) for p in other)
    gross = gross_eq + gross_other
    print(f"Positions: {len(longs_eq)} long {SLEEVE} (${gross_eq:,.0f}) + "
          f"{len(other)} other (${gross_other:,.0f} — untouched)  "
          f"gross=${gross:,.0f} = {gross / equity:.2f}x equity")
    comp = {}
    for p in positions:
        k = f"{p.get('asset_class', '?')}/{p['side']}"
        comp[k] = comp.get(k, [0, 0.0])
        comp[k][0] += 1
        comp[k][1] += abs(float(p["market_value"]))
    print("  composition: " + "  ".join(
        f"{k}: {n} pos ${mv:,.0f}" for k, (n, mv) in sorted(comp.items())))

    # SELL orders the model already queued — they de-lever at the open too.
    queued = {}
    for o in api("/v2/orders?status=open&limit=500"):
        if o["side"] == "sell":
            queued[o["symbol"]] = queued.get(o["symbol"], 0) + float(o["qty"])
    prices = {p["symbol"]: float(p["current_price"]) for p in longs_eq}
    queued_mv = sum(q * prices.get(s, 0) for s, q in queued.items())
    if queued:
        print("Already-queued SELLs (netted out): " +
              ", ".join(f"{s} x{q:.0f}" for s, q in sorted(queued.items())) +
              f"  (~${queued_mv:,.0f})")

    target_gross = equity * TARGET_RATIO
    to_sell = gross - queued_mv - target_gross
    print(f"\nTarget gross: ${target_gross:,.0f} ({TARGET_RATIO:.2f}x)  ->  "
          f"need to sell ~${to_sell:,.0f} beyond queued exits")
    if to_sell <= 0:
        print("Nothing to do — already at/below target after queued exits.")
        return 0

    sellable_mv = sum((float(p["qty"]) - queued.get(p["symbol"], 0)) * prices[p["symbol"]]
                      for p in longs_eq)
    if sellable_mv <= 0:
        print(f"No sellable long {SLEEVE} positions (see composition above) — nothing to do.")
        return 0
    f = min(1.0, to_sell / sellable_mv)
    print(f"Pro-rata factor on sellable equity book (${sellable_mv:,.0f}): {f:.3f}")

    print(f"\n  {'sym':10s} {'held':>12s} {'queued':>7s} {'price':>12s} "
          f"{'sell':>12s} {'sell $':>10s} {'P&L':>10s}")
    plan, plan_mv = [], 0.0
    for p in sorted(longs_eq, key=lambda x: -float(x["market_value"])):
        sym = p["symbol"]
        held = float(p["qty"])
        q = queued.get(sym, 0)
        avail = max(0.0, held - q)
        if SLEEVE == "crypto":
            # Fractional, floored to 6dp; skip dust (<$1 sells are rejected).
            sell = math.floor(avail * f * 1e6) / 1e6
            if sell <= 0 or sell * prices[sym] < 1.0:
                continue
            qty_s = f"{sell:.6f}".rstrip("0").rstrip(".")
        else:
            sell = math.floor(avail * f)
            if sell < 1:
                continue
            qty_s = str(int(sell))
        mv = sell * prices[sym]
        upl_frac = float(p["unrealized_plpc"])
        print(f"  {sym:10s} {held:12,.4f} {q:7.0f} {prices[sym]:12,.2f} "
              f"{qty_s:>12s} {mv:10,.0f} {mv * upl_frac / (1 + upl_frac):+10,.0f}")
        plan.append((sym, qty_s))
        plan_mv += mv

    tif = "gtc" if SLEEVE == "crypto" else "day"
    when = "fill ~immediately, 24/7" if SLEEVE == "crypto" else "fill at next open"
    print(f"\n── Plan: {len(plan)} market SELLs ({tif.upper()}, {when}), "
          f"~${plan_mv:,.0f} ──")
    print(f"Projected after queued + plan: gross ~${gross - queued_mv - plan_mv:,.0f} "
          f"({(gross - queued_mv - plan_mv) / equity:.2f}x equity), "
          f"cash ~${cash + queued_mv + plan_mv:,.0f}")

    if MODE == "dry-run":
        print("DRY-RUN — nothing submitted. Re-dispatch with mode=execute.")
        return 0

    print("\nEXECUTING…")
    for sym, qty in plan:
        r = api("/v2/orders", method="POST", body={
            "symbol": sym, "qty": str(qty), "side": "sell",
            "type": "market", "time_in_force": tif,
        })
        print(f"  SELL {sym} x{qty} submitted (id {r['id'][:8]}…)")
    print(f"Done — {len(plan)} sells queued. Verify account after next open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
