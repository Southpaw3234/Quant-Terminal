#!/usr/bin/env python3
"""Availability probe — can Finnhub supply earnings surprises for the A4 universe?

Candidate spec #3 is post-earnings-announcement drift (PEAD) in small caps.
Before a word of that specification is written, the same two questions that
preceded spec #2 apply, and they cost nothing against K:

  1. What does the endpoint actually return? Schema, and -- critically -- how
     much HISTORY. A free tier that serves four quarters cannot support a
     three-year study.
  2. What fraction of the 907-name universe has coverage at all? Small caps
     are exactly where analyst estimates thin out, and no estimate means no
     surprise means no event.

Two endpoints are probed because they may differ in depth:

  /stock/earnings          per-symbol quarterly actual/estimate/surprise
  /calendar/earnings       date-ranged; may carry deeper history per symbol

Samples the universe rather than sweeping it: 907 x 2 calls at 60/min is
half an hour, and a 30-name sample answers "which endpoint, how deep, what
coverage" for a minute of API time. A full sweep follows only if this clears.

Availability only. Computes no return, writes nothing under data/, spends
no K.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pandas as pd

BASE = "https://finnhub.io/api/v1"
UNIVERSE = os.environ.get("QT_U_FILE", "data/universe/v27_universe.csv")
N_SAMPLE = int(os.environ.get("QT_PROBE_N", "30"))
SEED = 20260905


def _universe(n: int) -> list:
    df = pd.read_csv(UNIVERSE)
    tk = sorted(df["ticker"].astype(str).str.upper().unique().tolist())
    # deterministic sample so the probe is reproducible
    import random
    rnd = random.Random(SEED)
    return sorted(rnd.sample(tk, min(n, len(tk))))


def _get(sess, path, **params):
    r = sess.get(f"{BASE}{path}", params=params, timeout=20)
    return r.status_code, (r.json() if r.status_code == 200 else r.text[:200])


def main() -> None:
    import requests
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        print("[probe] FINNHUB_API_KEY not set"); sys.exit(2)
    tickers = _universe(N_SAMPLE)
    print(f"[probe] {len(tickers)} sampled from {UNIVERSE}: {' '.join(tickers)}\n")
    s = requests.Session()

    # ── 1. /stock/earnings ──────────────────────────────────────────────
    print("=" * 70); print("1. /stock/earnings  (per-symbol quarterly surprises)"); print("=" * 70)
    schema_shown = False
    per = []
    for tk in tickers:
        code, body = _get(s, "/stock/earnings", symbol=tk, token=key)
        time.sleep(1.05)
        if code != 200:
            per.append((tk, "HTTP", 0, "", "")); continue
        rows = body if isinstance(body, list) else []
        if rows and not schema_shown:
            print(f"  record keys: {sorted(rows[0].keys())}")
            print(f"  sample: {json.dumps(rows[0])[:240]}")
            schema_shown = True
        periods = sorted(str(r.get("period", "")) for r in rows if r.get("period"))
        with_est = sum(1 for r in rows if r.get("estimate") is not None)
        per.append((tk, "ok", len(rows), periods[0] if periods else "", periods[-1] if periods else "",
                    with_est))
    print()
    for row in per:
        tk, st, n, lo, hi = row[:5]
        est = row[5] if len(row) > 5 else 0
        print(f"  {tk:<6} {st:<4} quarters={n:<3} est={est:<3} {lo} -> {hi}")
    ok = [r for r in per if r[1] == "ok"]
    covered = [r for r in ok if r[2] > 0]
    with_est = [r for r in ok if len(r) > 5 and r[5] > 0]
    depths = [r[2] for r in covered]
    print(f"\n  coverage: {len(covered)}/{len(tickers)} return any quarters; "
          f"{len(with_est)}/{len(tickers)} have at least one ESTIMATE (no estimate = no surprise = no event)")
    if depths:
        print(f"  depth: min={min(depths)} median={sorted(depths)[len(depths)//2]} max={max(depths)} quarters")
        earliest = min(r[3] for r in covered if r[3])
        print(f"  earliest period seen: {earliest}")
        if max(depths) <= 4:
            print("  🔴 CAPPED AT ~4 QUARTERS -- one year. Cannot support a 3-year PEAD study from this endpoint.")
        elif max(depths) >= 12:
            print("  ✅ 3+ years of quarters available on at least some names.")

    # ── 2. /calendar/earnings ──────────────────────────────────────────
    print("\n" + "=" * 70); print("2. /calendar/earnings  (date-ranged; per-symbol history?)"); print("=" * 70)
    frm, to = "2023-09-01", "2026-09-05"
    schema_shown = False
    per2 = []
    for tk in tickers[:15]:                       # cheaper: 15 names
        code, body = _get(s, "/calendar/earnings", symbol=tk, **{"from": frm, "to": to, "token": key})
        time.sleep(1.05)
        if code != 200:
            per2.append((tk, "HTTP", 0, "", "", 0)); continue
        rows = (body or {}).get("earningsCalendar", []) if isinstance(body, dict) else []
        if rows and not schema_shown:
            print(f"  record keys: {sorted(rows[0].keys())}")
            print(f"  sample: {json.dumps(rows[0])[:240]}")
            schema_shown = True
        dates = sorted(str(r.get("date", "")) for r in rows if r.get("date"))
        with_both = sum(1 for r in rows if r.get("epsActual") is not None and r.get("epsEstimate") is not None)
        per2.append((tk, "ok", len(rows), dates[0] if dates else "", dates[-1] if dates else "", with_both))
    print()
    for tk, st, n, lo, hi, wb in per2:
        print(f"  {tk:<6} {st:<4} dates={n:<3} actual+est={wb:<3} {lo} -> {hi}")
    ok2 = [r for r in per2 if r[1] == "ok" and r[2] > 0]
    print(f"\n  coverage: {len(ok2)}/{len(per2)} return dates in {frm}..{to}; "
          f"{sum(1 for r in per2 if r[5] > 0)}/{len(per2)} have actual+estimate pairs")
    if ok2:
        print(f"  depth: max {max(r[2] for r in ok2)} announcements per name over 3y; "
              f"earliest {min(r[3] for r in ok2 if r[3])}")

    print("\n[probe] done. Availability only -- no return computed, nothing written, K untouched.")


if __name__ == "__main__":
    main()
