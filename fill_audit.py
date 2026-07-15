"""
fill_audit.py — measurement-only: reconcile the dashboard trades ledger
against Alpaca order history (broker ground truth).

The ledger (data/paper_trades/trade_history.csv) records submission-poll
status, not fills — three phantom-fill incidents (5/12, 7/6, 7/7) showed rows
marked "filled" for orders the broker rejected, cancelled, or never filled.
This audit joins every ledger row to the broker order by order_id and
classifies it:

    OK                 broker filled, qty matches the ledger row
    PARTIAL_FILL       broker filled less qty than the ledger claims
    PHANTOM_FILL       ledger says filled; broker says canceled/rejected/
                       expired/open with zero filled qty
    UNKNOWN_TO_BROKER  order_id not in broker history (never accepted)
    NO_ORDER_ID        ledger row carries no order_id to check

Reverse check: broker fills in the window with no ledger row (expected for
remediation tooling — delever_account.py / cancel_open_buys.py — but anything
else is the model trading without a ledger record).

Read-only: GETs only, submits/cancels NOTHING, writes NOTHING to the repo.
Run via the "Fill Audit (read-only)" workflow_dispatch.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

import pandas as pd

LEDGER = "data/paper_trades/trade_history.csv"


def _clean(v):
    # Repo secrets carry a UTF-8 BOM from the original PowerShell pipe.
    return v.strip().strip("\ufeff").strip()


BASE = _clean(os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")).rstrip("/")
KEY = _clean(os.environ["ALPACA_API_KEY"])
SEC = _clean(os.environ["ALPACA_SECRET_KEY"])


def api(path):
    req = urllib.request.Request(f"{BASE}{path}")
    req.add_header("APCA-API-KEY-ID", KEY)
    req.add_header("APCA-API-SECRET-KEY", SEC)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def fetch_all_orders(after_iso):
    """Every order (any status) submitted after `after_iso`, paginated ascending."""
    orders, seen, after = [], set(), after_iso
    for _ in range(60):  # hard stop: 60 pages x 500 = 30k orders
        q = urllib.parse.urlencode(
            {"status": "all", "limit": 500, "direction": "asc", "after": after})
        batch = api(f"/v2/orders?{q}")
        fresh = [o for o in batch if o["id"] not in seen]
        for o in fresh:
            seen.add(o["id"])
        orders.extend(fresh)
        if len(batch) < 500 or not fresh:
            break
        after = batch[-1]["submitted_at"]
    return orders


def main():
    led = pd.read_csv(LEDGER)
    led["ts"] = pd.to_datetime(led["ts"], format="mixed")
    start = (led["ts"].min() - pd.Timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Ledger: {len(led)} rows  {led['ts'].min().date()} -> {led['ts'].max().date()}")

    orders = fetch_all_orders(start)
    by_id = {o["id"]: o for o in orders}
    print(f"Broker: {len(orders)} orders in window (status=all)\n")

    rows = []
    for _, r in led.iterrows():
        oid = r.get("order_id")
        oid = "" if pd.isna(oid) else str(oid).strip()
        qty = 0.0 if pd.isna(r.get("qty")) else float(r["qty"])
        notional = 0.0 if pd.isna(r.get("notional")) else float(r["notional"])
        if not oid:
            cls = "NO_ORDER_ID"
            broker_status, fqty = "", 0.0
        elif oid not in by_id:
            cls = "UNKNOWN_TO_BROKER"
            broker_status, fqty = "", 0.0
        else:
            o = by_id[oid]
            broker_status = o["status"]
            fqty = float(o.get("filled_qty") or 0)
            if broker_status == "filled" and abs(fqty - qty) < 1e-9:
                cls = "OK"
            elif fqty > 0:
                cls = "PARTIAL_FILL"
            else:
                cls = "PHANTOM_FILL"
        rows.append({"date": str(r["ts"].date()), "ticker": r["ticker"],
                     "action": r["action"], "qty": qty, "notional": notional,
                     "class": cls, "broker_status": broker_status, "filled_qty": fqty})

    audit = pd.DataFrame(rows)
    print("=== ledger -> broker reconciliation ===")
    for cls, grp in audit.groupby("class"):
        print(f"  {cls:<18} {len(grp):>4} rows   ${grp['notional'].sum():>12,.0f} notional")

    bad = audit[~audit["class"].isin(["OK"])]
    if len(bad):
        print("\n--- non-OK rows by day ---")
        day = (bad.groupby(["date", "class"])
               .agg(n=("ticker", "size"), dollars=("notional", "sum")).reset_index())
        for _, d in day.iterrows():
            print(f"  {d['date']}  {d['class']:<18} {d['n']:>3} rows  ${d['dollars']:>10,.0f}")
        print("\n--- 15 largest non-OK rows ---")
        for _, b in bad.nlargest(15, "notional").iterrows():
            print(f"  {b['date']}  {b['action']:<4} {b['ticker']:<6} qty {b['qty']:>7.2f}  "
                  f"${b['notional']:>9,.0f}  {b['class']}"
                  f"{'  broker=' + b['broker_status'] if b['broker_status'] else ''}"
                  f"  filled_qty={b['filled_qty']:g}")

    # Reverse check: broker fills the ledger never recorded.
    ledger_ids = set(audit_oid for audit_oid in led["order_id"].dropna().astype(str).str.strip() if audit_oid)
    unrecorded = [o for o in orders
                  if float(o.get("filled_qty") or 0) > 0 and o["id"] not in ledger_ids]
    dollars = sum(float(o.get("filled_avg_price") or 0) * float(o.get("filled_qty") or 0)
                  for o in unrecorded)
    print(f"\n=== broker -> ledger reverse check ===")
    print(f"  broker fills with NO ledger row: {len(unrecorded)}  (${dollars:,.0f})")
    print("  (remediation tooling — delever/cancel workflows — is expected here;")
    print("   anything else is order flow the dashboard never saw)")
    if unrecorded:
        per_day = {}
        for o in unrecorded:
            d = (o.get("filled_at") or o["submitted_at"])[:10]
            v = float(o.get("filled_avg_price") or 0) * float(o.get("filled_qty") or 0)
            n, tot = per_day.get(d, (0, 0.0))
            per_day[d] = (n + 1, tot + v)
        for d in sorted(per_day):
            n, tot = per_day[d]
            print(f"  {d}  {n:>3} fills  ${tot:>10,.0f}")
        # Per-order detail for the most recent active days — attribution
        # (which tool/code path submitted what) is the point of this check.
        recent = sorted(per_day)[-3:]
        print(f"  --- unrecorded order detail ({', '.join(recent)}) ---")
        for o in sorted(unrecorded,
                        key=lambda x: (x.get("filled_at") or x["submitted_at"])):
            d = (o.get("filled_at") or o["submitted_at"])[:10]
            if d not in recent:
                continue
            px = float(o.get("filled_avg_price") or 0)
            fq = float(o.get("filled_qty") or 0)
            print(f"  {d}  {o['side']:<4} {o['symbol']:<6} x{fq:>7g} @ {px:>9,.2f} "
                  f"${px * fq:>9,.0f}  submitted {o['submitted_at'][11:19]}Z")

    n_ok = (audit["class"] == "OK").sum()
    print(f"\nVERDICT: {n_ok}/{len(audit)} ledger rows are broker-confirmed fills; "
          f"{len(bad)} rows (${bad['notional'].sum():,.0f}) are phantom/partial/unverifiable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
