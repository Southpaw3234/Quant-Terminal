#!/usr/bin/env python3
"""Read-only probe: which calendar day does Alpaca's portfolio/history stamp?

WHY THIS EXISTS (2026-08-05)
---------------------------
`data/predictions/pnl_history.csv` carries every trading session under the
WRONG calendar label — one day late. The signature is unmistakable: across 48
rows the day-of-week distribution is Tue 10 / Wed 10 / Thu 10 / Fri 10 / Sat 8
and **zero Mondays**. Monday's session wears Tuesday's label, Friday's wears
Saturday's, so nothing ever lands on a Monday. (The two absent Saturdays are
2026-06-19 Juneteenth and 2026-07-03 July-4th-observed — market holidays, no
session to shift.) Three independent equity anchors confirm it, each matching a
morning kill-switch read taken ~9:40 ET, i.e. the PRIOR session's close:

    row 2026-07-31 pv 112,938.46 == 7/31 13:39Z equity $112,938  (Thu 7/30 close)
    row 2026-08-01 pv 113,327.65 == 8/03 13:39Z equity $113,328  (Fri 7/31 close)
    row 2026-08-04 pv 113,183.42 == 8/04 13:40Z equity $113,183  (Mon 8/03 close)

The suspected cause is quant_runner.py's history loop rendering the bar
timestamp in UTC (`utcfromtimestamp`) while the request asks for
`extended_hours=true`. An extended session ends 8 PM ET, which in EDT is
exactly 00:00 UTC the NEXT day — so every bar rolls over.

WHAT THIS PROBE SETTLES
-----------------------
The history is entirely EDT, so it cannot distinguish two candidate stamping
conventions that both produce a +1 UTC shift:

  (A) bar stamped at the extended-hours CLOSE (00:00 UTC / 8 PM EDT)
      -> rendering in ET recovers the correct session date. FIX WORKS.
  (B) bar stamped at 00:00 ET of the FOLLOWING day (04:00 UTC)
      -> ET rendering is ALSO next-day. FIX DOES NOT WORK; the bar needs a
         different anchor (e.g. drop extended_hours, or shift back one session).

One look at the raw epoch seconds settles it. This probe prints them.

SAFETY: GETs only. Submits nothing, cancels nothing, writes no repo state.
"""
import csv
import datetime as dt
import os
import sys
from collections import Counter

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception as _e:  # pragma: no cover - CI images all ship tzdata
    print(f"FATAL: zoneinfo unavailable ({_e}) — cannot render ET, aborting.")
    sys.exit(1)

BASE = (os.environ.get("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets").strip()
KEY = (os.environ.get("ALPACA_API_KEY") or "").strip()
SEC = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
if not (KEY and SEC):
    print("FATAL: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.")
    sys.exit(1)
HDR = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

N_SHOW = int(os.environ.get("PROBE_ROWS", "12"))
PNL_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", "predictions", "pnl_history.csv")


def _get(path, **params):
    r = requests.get(f"{BASE}{path}", headers=HDR, params=params or None, timeout=20)
    r.raise_for_status()
    return r.json()


def main() -> None:
    print("=" * 78)
    print("pnl_history date-shift probe — READ-ONLY (GETs only)")
    print("=" * 78)

    acct = _get("/v2/account")
    equity = float(acct.get("equity", 0) or 0)
    last_equity = float(acct.get("last_equity", 0) or 0)
    now_et = dt.datetime.now(ET)
    print(f"\nAnchor: now={now_et:%Y-%m-%d %H:%M:%S %Z}  "
          f"equity=${equity:,.2f}  last_equity=${last_equity:,.2f}")
    print("  (before ~16:00 ET, `equity` still tracks the PRIOR session's close;")
    print("   `last_equity` IS the prior session's close by Alpaca's definition.)")

    # EXACT same params as quant_runner.py's history pull — probing anything
    # else would answer a question we did not ask.
    ph = _get("/v2/account/portfolio/history",
              period="1A", timeframe="1D", extended_hours="true")
    ts = ph.get("timestamp", []) or []
    eq = ph.get("equity", []) or []
    pl = ph.get("profit_loss", []) or []
    print(f"\nportfolio/history: period=1A timeframe=1D extended_hours=true "
          f"-> {len(ts)} points")

    if not ts:
        print("FATAL: empty timestamp array — nothing to probe.")
        sys.exit(1)

    # ── the decisive table ───────────────────────────────────────────────────
    print(f"\nLast {N_SHOW} points — raw epoch rendered both ways:\n")
    print(f"  {'epoch':>12}  {'UTC instant':<20} {'UTC date':<11} "
          f"{'ET instant':<20} {'ET date':<11} {'dow(ET)':<4} {'equity':>12}")
    print("  " + "-" * 104)
    for i in range(max(0, len(ts) - N_SHOW), len(ts)):
        t = int(ts[i])
        u = dt.datetime.fromtimestamp(t, tz=dt.timezone.utc)
        e = u.astimezone(ET)
        v = float(eq[i]) if i < len(eq) and eq[i] not in (None, "") else float("nan")
        print(f"  {t:>12}  {u:%Y-%m-%d %H:%M:%SZ}  {u:%Y-%m-%d}  "
              f"{e:%Y-%m-%d %H:%M:%S}     {e:%Y-%m-%d}  {e:%a}   {v:>12,.2f}")

    # ── day-of-week signature under each rendering ───────────────────────────
    utc_dates, et_dates = [], []
    for i in range(len(ts)):
        t = int(ts[i])
        u = dt.datetime.fromtimestamp(t, tz=dt.timezone.utc)
        d_u = u.strftime("%Y-%m-%d")
        d_e = u.astimezone(ET).strftime("%Y-%m-%d")
        if d_u >= "2026-05-28":
            utc_dates.append(d_u)
        if d_e >= "2026-05-28":
            et_dates.append(d_e)

    def dow_report(label, dates):
        c = Counter(dt.date.fromisoformat(d).strftime("%a") for d in dates)
        order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        line = "  ".join(f"{k} {c.get(k, 0)}" for k in order)
        weekend = c.get("Sat", 0) + c.get("Sun", 0)
        print(f"\n  {label:<22} n={len(dates):<4} {line}")
        print(f"  {'':<22} weekend rows={weekend}   Monday rows={c.get('Mon', 0)}")
        return c.get("Mon", 0), weekend

    print("\nDay-of-week signature (rows on/after the 2026-05-28 epoch):")
    mon_u, wknd_u = dow_report("rendered in UTC", utc_dates)
    mon_e, wknd_e = dow_report("rendered in ET", et_dates)

    # ── what the committed file currently says ───────────────────────────────
    if os.path.exists(PNL_CSV):
        with open(PNL_CSV, newline="", encoding="utf-8-sig") as fh:
            file_dates = [r["date"] for r in csv.DictReader(fh)
                          if (r.get("date") or "") >= "2026-05-28"]
        dow_report("committed pnl_history", file_dates)
    else:
        print(f"\n  committed pnl_history: {PNL_CSV} absent (skipped)")

    # ── verdict ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    if wknd_e == 0 and mon_e > 0:
        print("VERDICT: convention (A) — ET rendering recovers the true session date.")
        print("  ET labels contain zero weekend rows and a normal count of Mondays.")
        print("  -> The quant_runner.py:5855 ET fix on this branch is CORRECT. Merge it.")
        verdict = 0
    elif wknd_e > 0 or mon_e == 0:
        print("VERDICT: convention (B) or unknown — ET rendering does NOT fix the labels.")
        print(f"  ET labels still show weekend={wknd_e} Monday={mon_e}.")
        print("  -> DO NOT MERGE the ET fix as-is. The bar is anchored past the ET")
        print("     session boundary; drop extended_hours or shift back one session.")
        print("     The raw epoch table above shows the true anchor instant.")
        verdict = 2
    else:
        print("VERDICT: inconclusive — inspect the raw table above by hand.")
        verdict = 2
    print(f"  (UTC rendering for contrast: weekend={wknd_u} Monday={mon_u})")
    print("=" * 78)
    sys.exit(verdict)


if __name__ == "__main__":
    main()
