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
TRIM_SLEEVE=sector (added 2026-07-31) trims ONE sector instead of a whole
sleeve: pro-rata sells the longs in TRIM_SECTOR until that sector is <=
TARGET_RATIO * equity. Note the different meaning of TARGET_RATIO on this path
— sector share of EQUITY, not gross/equity. This exists because the `equity`
sleeve is pro-rata across the whole book and therefore CANNOT change
composition: selling the same fraction of everything leaves the sector's share
of the book unchanged and only shrinks gross. See HANDOFF 7/31 ledger ⑨/⑩.
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
TRIM_SECTOR = os.environ.get("TRIM_SECTOR", "").strip()


def _load_sector_map():
    """Pull SECTOR_MAP out of quant_runner.py rather than keeping a copy.

    The map lives inside the CELL_13_PREPATCH string literal. A second copy
    here would drift from the one the sector cap enforces, and a trim that
    disagrees with the cap about what "Energy" means is worse than no trim at
    all. Same extraction the validate suite uses (validate_gross_cap.py §10),
    which also CI-enforces that every traded ticker is present.
    """
    import ast
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "quant_runner.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    prepatch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Name) and t.id == "CELL_13_PREPATCH"
                        and prepatch is None and isinstance(node.value, ast.Constant)):
                    prepatch = node.value.value
    if not prepatch or "SECTOR_MAP = {" not in prepatch:
        raise RuntimeError("SECTOR_MAP not found in quant_runner.py — refusing to guess")
    blk = prepatch[prepatch.index("SECTOR_MAP = {"):]
    blk = blk[:blk.index("\n}") + 2]
    ns = {}
    exec(blk, ns)
    return ns["SECTOR_MAP"]


def trim_sector():
    """TRIM_SLEEVE=sector: pro-rata sell ONE sector's longs to a target weight.

    Added 2026-07-31, after the energy concentration behind the 7/30 kill-switch
    trip. The plain `equity` sleeve is pro-rata across the WHOLE book, so it
    shrinks gross WITHOUT changing composition — selling the same fraction of
    every position leaves the sector's share of the book exactly where it
    started. Reducing a concentration needs a selector; this is it.

    TARGET_RATIO here means "this sector as a share of EQUITY" (e.g. 0.25 to
    match QT_MAX_SECTOR), NOT gross/equity as it does for the other sleeves.

    Guardrails, all inherited from the equity path: longs only, one sector only,
    shorts untouched, every other sector untouched, never submits a BUY, never
    sells more than held-minus-queued, and dry-run submits nothing.
    """
    sector_map = _load_sector_map()
    if not TRIM_SECTOR:
        print("TRIM_SECTOR is empty — refusing (name the sector to trim).")
        return 1
    known = sorted(set(sector_map.values()))
    match = [s for s in known if s.lower() == TRIM_SECTOR.lower()]
    if not match:
        print(f"Unknown sector '{TRIM_SECTOR}' — refusing. Known: {', '.join(known)}")
        return 1
    sector = match[0]
    if TARGET_RATIO >= 1.0:
        print(f"TARGET_RATIO={TARGET_RATIO} would mean '{sector} <= "
              f"{TARGET_RATIO:.0%} of equity', which can never bind — refusing. "
              f"For a sector trim this is the sector's share of EQUITY; pass "
              f"0.25 to match the QT_MAX_SECTOR default.")
        return 1

    acct = api("/v2/account")
    equity = float(acct["equity"])
    cash = float(acct["cash"])
    print(f"Account: equity=${equity:,.2f}  cash=${cash:,.2f}")

    positions = api("/v2/positions")
    longs = [p for p in positions
             if p["side"] == "long" and p.get("asset_class") == "us_equity"]
    in_sector = [p for p in longs if sector_map.get(p["symbol"], "Other") == sector]
    sector_mv = sum(float(p["market_value"]) for p in in_sector)
    book_mv = sum(abs(float(p["market_value"])) for p in positions)
    print(f"Sector {sector}: {len(in_sector)} of {len(longs)} long equities, "
          f"${sector_mv:,.0f} = {sector_mv / equity:.1%} of equity, "
          f"{(sector_mv / book_mv if book_mv else 0):.1%} of book  "
          f"(gross ${book_mv:,.0f} = {book_mv / equity:.2f}x)")
    if not in_sector:
        print(f"No long {sector} positions — nothing to do.")
        return 0

    # A held symbol missing from SECTOR_MAP falls to "Other" and is therefore
    # NOT trimmed here even if it belongs to this sector economically. §10 of
    # the validate suite makes that a build failure, but say so out loud.
    unmapped = sorted({p["symbol"] for p in longs if p["symbol"] not in sector_map})
    if unmapped:
        print(f"  ⚠️ {len(unmapped)} held symbol(s) absent from SECTOR_MAP — "
              f"counted as 'Other', NOT as {sector}: {', '.join(unmapped)}")

    queued = {}
    for o in api("/v2/orders?status=open&limit=500"):
        if o["side"] == "sell":
            queued[o["symbol"]] = queued.get(o["symbol"], 0) + float(o["qty"])
    prices = {p["symbol"]: float(p["current_price"]) for p in in_sector}
    queued_mv = sum(queued.get(s, 0) * prices[s] for s in prices)
    if queued_mv:
        print(f"Already-queued SELLs in {sector} (netted out): ~${queued_mv:,.0f}")

    target_mv = equity * TARGET_RATIO
    to_sell = sector_mv - queued_mv - target_mv
    print(f"\nTarget: {sector} <= ${target_mv:,.0f} ({TARGET_RATIO:.0%} of equity)"
          f"  ->  need to sell ~${to_sell:,.0f} beyond queued exits")
    if to_sell <= 0:
        print(f"Nothing to do — {sector} already at/below target after queued exits.")
        return 0

    sellable_mv = sum((float(p["qty"]) - queued.get(p["symbol"], 0)) * prices[p["symbol"]]
                      for p in in_sector)
    if sellable_mv <= 0:
        print(f"No sellable {sector} positions (all held qty already queued) — nothing to do.")
        return 0
    f = min(1.0, to_sell / sellable_mv)
    print(f"Pro-rata factor within {sector} (${sellable_mv:,.0f} sellable): {f:.3f}")

    print(f"\n  {'sym':10s} {'held':>12s} {'queued':>7s} {'price':>12s} "
          f"{'sell':>12s} {'sell $':>10s} {'P&L':>10s}")
    plan, plan_mv = [], 0.0
    for p in sorted(in_sector, key=lambda x: -float(x["market_value"])):
        sym = p["symbol"]
        held = float(p["qty"])
        q = queued.get(sym, 0)
        avail = max(0.0, held - q)
        sell = math.floor(avail * f)
        if sell < 1:
            continue
        mv = sell * prices[sym]
        upl_frac = float(p["unrealized_plpc"])
        print(f"  {sym:10s} {held:12,.4f} {q:7.0f} {prices[sym]:12,.2f} "
              f"{int(sell):12d} {mv:10,.0f} {mv * upl_frac / (1 + upl_frac):+10,.0f}")
        plan.append((sym, str(int(sell))))
        plan_mv += mv

    after = sector_mv - queued_mv - plan_mv
    print(f"\n── Plan: {len(plan)} market SELLs (DAY, fill at next open), "
          f"~${plan_mv:,.0f} ──")
    print(f"Projected {sector} after queued + plan: ${after:,.0f} = "
          f"{after / equity:.1%} of equity (target {TARGET_RATIO:.0%})")
    print(f"Projected gross: ${book_mv - queued_mv - plan_mv:,.0f} "
          f"({(book_mv - queued_mv - plan_mv) / equity:.2f}x), "
          f"cash ~${cash + queued_mv + plan_mv:,.0f}")
    print(f"Every other sector is untouched — only {sector} names appear above.")

    if MODE == "dry-run":
        print("DRY-RUN — nothing submitted. Re-dispatch with mode=execute.")
        return 0
    print(f"\nEXECUTE — submitting {len(plan)} market SELLs…")
    for sym, qty in plan:
        r = api("/v2/orders", method="POST", body={
            "symbol": sym, "qty": qty, "side": "sell",
            "type": "market", "time_in_force": "day",
        })
        print(f"  SELL {sym} x{qty} submitted (id {r['id'][:8]}…)")
    print(f"\nSubmitted {len(plan)} sells (~${plan_mv:,.0f}). DAY orders fill at "
          f"the next open if the market is closed.")
    return 0


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


def cover_shorts():
    """TRIM_SLEEVE=short-cover: BUY-to-cover every SHORT position, exactly.

    Added 2026-07-15 for the short-book incident (22 accidental shorts,
    ~$81.5k — oversell bug, see HANDOFF 7/15 + quant_runner _oversell_cap).
    Guardrails: only positions with side == "short"; BUY qty == abs(position
    qty), never more; never touches longs; TARGET_RATIO ignored. Market DAY
    orders — submitted after hours they fill at the next open.
    """
    acct = api("/v2/account")
    equity = float(acct["equity"])
    cash = float(acct["cash"])
    print(f"Account: equity=${equity:,.2f}  cash=${cash:,.2f}")

    positions = api("/v2/positions")
    shorts = [p for p in positions if p["side"] == "short"]
    gross = sum(abs(float(p["market_value"])) for p in positions)
    short_mv = sum(abs(float(p["market_value"])) for p in shorts)
    print(f"Positions: {len(positions)} total, gross ${gross:,.0f} "
          f"({gross / equity:.2f}x) | {len(shorts)} SHORT (${short_mv:,.0f})")
    if not shorts:
        print("No short positions — nothing to cover.")
        return 0
    if short_mv > cash:
        print(f"REFUSING: cover cost ${short_mv:,.0f} exceeds cash ${cash:,.0f}.")
        return 1

    # Net out any open BUY orders already working these symbols.
    open_buys = {}
    for o in api("/v2/orders?status=open&limit=500"):
        if o["side"] == "buy":
            open_buys[o["symbol"]] = open_buys.get(o["symbol"], 0) + float(o["qty"])

    print(f"\n  {'sym':6s} {'short':>8s} {'openBUY':>8s} {'price':>10s} "
          f"{'cover':>6s} {'cost $':>10s} {'P&L':>10s}")
    plan, plan_mv, pnl = [], 0.0, 0.0
    for p in sorted(shorts, key=lambda x: -abs(float(x["market_value"]))):
        sym = p["symbol"]
        qty = int(abs(float(p["qty"])))
        cover = qty - int(open_buys.get(sym, 0))
        if cover < 1:
            continue
        px = float(p["current_price"])
        mv = cover * px
        upl = float(p["unrealized_pl"])
        print(f"  {sym:6s} {float(p['qty']):8.0f} {open_buys.get(sym, 0):8.0f} "
              f"{px:10,.2f} {cover:6d} {mv:10,.0f} {upl:+10,.0f}")
        plan.append((sym, cover))
        plan_mv += mv
        pnl += upl
    print(f"\n── Plan: {len(plan)} market BUY-to-cover (DAY, fill at next open), "
          f"~${plan_mv:,.0f}, realizes ~${pnl:+,.0f} ──")
    print(f"Projected after covers: gross ~${gross - short_mv:,.0f} "
          f"({(gross - short_mv) / equity:.2f}x equity), zero shorts")

    if MODE == "dry-run":
        print("DRY-RUN — nothing submitted. Re-dispatch with mode=execute.")
        return 0

    print("\nEXECUTING…")
    for sym, qty in plan:
        r = api("/v2/orders", method="POST", body={
            "symbol": sym, "qty": str(qty), "side": "buy",
            "type": "market", "time_in_force": "day",
        })
        print(f"  BUY-to-cover {sym} x{qty} submitted (id {r['id'][:8]}…)")
    print(f"Done — {len(plan)} covers queued. Verify zero shorts after next open.")
    return 0


def main():
    print(f"MODE={MODE}  TARGET_RATIO={TARGET_RATIO}  SLEEVE={SLEEVE}"
          + (f"  SECTOR={TRIM_SECTOR}" if SLEEVE == "sector" else "")
          + f"  BASE={BASE}")
    if MODE not in ("dry-run", "execute"):
        print(f"Unknown TRIM_MODE '{MODE}' — refusing.")
        return 1
    if SLEEVE not in ("equity", "crypto", "short-cover", "sector"):
        print(f"Unknown TRIM_SLEEVE '{SLEEVE}' — refusing.")
        return 1
    sleeve_class = "us_equity" if SLEEVE == "equity" else "crypto"
    if SLEEVE == "short-cover":
        return cover_shorts()
    if SLEEVE == "sector":
        return trim_sector()

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
    shorts = [p for p in positions if p["side"] == "short"]
    if shorts:
        print(f"  ⚠️ SHORT positions ({len(shorts)} — the model is long-only; "
              f"these are likely overlapping-SELL residue):")
        print(f"    {'sym':6s} {'qty':>8s} {'basis':>10s} {'price':>10s} "
              f"{'|mv|':>10s} {'uP&L':>10s}")
        for p in sorted(shorts, key=lambda x: -abs(float(x["market_value"]))):
            print(f"    {p['symbol']:6s} {float(p['qty']):8.0f} "
                  f"{float(p['avg_entry_price']):10,.2f} "
                  f"{float(p['current_price']):10,.2f} "
                  f"{abs(float(p['market_value'])):10,.0f} "
                  f"{float(p['unrealized_pl']):+10,.0f}")

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
