#!/usr/bin/env python3
"""V29 integration driver — universe + fundamentals + prices -> portfolios -> read.

Every part of the value model existed before this file; nothing ran them in
order. This does, and it is the last thing standing between the specification
and a result.

⚠️ TWO MODES, AND THE DIFFERENCE IS THE WHOLE POINT

  select  (default)  Builds the quarterly portfolios and stops. Which names,
                     which tier, how many cleared each gate. Computes NO
                     return. Spends NO budget. This is the free rehearsal, and
                     it also answers criterion 2 -- at least 60 eligible names
                     per tier at the median rebalance -- before a slot is at
                     risk.

  read               Computes the daily return series, evaluates every
                     criterion, and RECORDS THE READ. This spends one of
                     K=3 and cannot be undone: anti-deferral means a read
                     specification can never be re-read, and no bug found
                     afterwards reopens it.

`read` requires BOTH the Referee's authorisation and an explicit
QT_V29_CONFIRM_READ=1. Two independent switches, because the failure mode here
is not a wrong number -- it is spending a third of the budget by running a
script that looked like a rehearsal.

THE ORDER OF OPERATIONS IS DECLARED AND IS NOT NEGOTIABLE: quality gate, then
risk tier, then rank on value. Ranking first and filtering afterwards produces
a different portfolio, and the registry says which one this is.
"""
from __future__ import annotations

import collections
import json
import os
import sys
import time

import numpy as np
import pandas as pd

from qt import calendar_time as ct
from qt import referee as ref
from qt import scoring as sc

REGISTRY = os.environ.get("QT_V29_REGISTRY", "data/registry/v29_specifications.json")
SPEC_ID = os.environ.get("QT_V29_SPEC", "value_ebit_ev_v1")
PANEL = os.environ.get("QT_V29_PANEL", "data/universe/v29_universe_pit.csv")
FUNDS = os.environ.get("QT_V29_FUNDS", "data/fundamentals/fundamentals_pit.csv")
OUT_PORT = os.environ.get("QT_V29_PORTFOLIOS", "data/portfolios/value_ebit_ev_v1.csv")
MODE = os.environ.get("QT_V29_MODE", "select").strip().lower()
CONFIRM = os.environ.get("QT_V29_CONFIRM_READ", "").strip() == "1"

# Declared parameters. Every one of these is frozen in the registry.
BENCHMARK = "IWM"
N_HOLD = 20
TIER = "moderate"
REBALANCE_EVERY = 3            # months
COST_BPS = 75.0
MAX_DE = 2.0
NW_LAG = 10
MIN_PER_TIER = 60


def _load_prices(tickers, start):
    import yfinance as yf
    out = {}
    need = sorted(set(tickers) | {BENCHMARK})
    for k in range(0, len(need), 100):
        chunk = need[k:k + 100]
        raw = yf.download(chunk, start=start, end=None, auto_adjust=True,
                          progress=False, threads=True, group_by="column")
        if raw is None or len(raw) == 0:
            continue
        multi = isinstance(raw.columns, pd.MultiIndex)
        for tk in chunk:
            try:
                c = raw["Close"][tk] if multi else raw["Close"]
            except (KeyError, TypeError):
                continue
            c = pd.to_numeric(c, errors="coerce").dropna()
            if len(c) < 60:
                continue
            c.index = pd.DatetimeIndex(c.index).tz_localize(None).normalize()
            out[tk] = c
        print(f"  prices {min(k+100, len(need))}/{len(need)} — {len(out)} usable")
    return out


def _score_one_date(asof, members, funds_at, prices):
    """Value, risk and tier for every candidate at one rebalance date."""
    rows = []
    for tk, adv in members.items():
        f = funds_at.get(tk)
        px = prices.get(tk)
        if f is None or px is None:
            continue
        hist = px[px.index < pd.Timestamp(asof)]
        if len(hist) < 60:
            continue
        price = float(hist.iloc[-1])
        ev = sc.enterprise_value(price, f.get("shares"), f.get("debt_lt"), f.get("cash"))
        ok, why = sc.passes_quality(f.get("op_income"), f.get("equity"),
                                    f.get("debt_lt"), MAX_DE)
        rows.append({
            "ticker": tk, "price": price, "adv": adv,
            "ebit_ev": sc.ebit_ev(f.get("op_income"), ev),
            "quality_ok": ok, "gate_reason": why,
            "_vol": sc.realized_vol(hist), "_mdd": sc.max_drawdown(hist),
            "_lev": sc.leverage_ratio(f.get("debt_lt"), f.get("equity")),
            "_illiq": sc.illiquidity(adv),
            "_thin": sc.thin_equity(f.get("equity"), f.get("assets")),
        })
    if not rows:
        return []
    ranks = {c: sc.percentile_rank([r[k] for r in rows]) for c, k in
             zip(sc.RISK_COMPONENTS, ("_vol", "_mdd", "_lev", "_illiq", "_thin"))}
    comps = [sc.composite_risk({c: ranks[c][i] for c in sc.RISK_COMPONENTS})
             for i in range(len(rows))]
    tiers = sc.assign_tiers(comps)
    for i, r in enumerate(rows):
        r["risk_score"] = comps[i]
        r["risk_tier"] = tiers[i]
    return rows


def main() -> None:
    if MODE not in ("select", "read"):
        print(f"[v29] QT_V29_MODE must be 'select' or 'read', got {MODE!r}"); sys.exit(2)
    for p in (PANEL, FUNDS, REGISTRY):
        if not os.path.exists(p):
            print(f"[v29] missing {p}"); sys.exit(1)

    # ── the gate ───────────────────────────────────────────────────────
    r = ref.Referee(registry_path=REGISTRY)
    print(f"[v29] mode={MODE}")
    print(r.status())
    if MODE == "read":
        today = time.strftime("%Y-%m-%d")
        v = r.authorize_read(SPEC_ID, today)
        if not v:
            print(f"\n[v29] REFEREE REFUSES: {v.reason}"); sys.exit(3)
        if not CONFIRM:
            print(f"\n[v29] Referee would allow this read ({v.reason}), but "
                  f"QT_V29_CONFIRM_READ is not set.")
            print("[v29] REFUSING. A read spends one of K=3 and anti-deferral makes it")
            print("      permanent -- it must be deliberate, not a default.")
            sys.exit(4)
        print(f"\n[v29] ⚠️ AUTHORISED READ — this will spend one of "
              f"{r.k_budget}. {v.reason}")

    # ── inputs ─────────────────────────────────────────────────────────
    panel = pd.read_csv(PANEL)
    funds = pd.read_csv(FUNDS)
    dates = sorted(panel["date"].unique())
    rebal = dates[::REBALANCE_EVERY]
    print(f"\n[v29] {len(panel):,} eligible name-months; {len(dates)} month-ends; "
          f"{len(rebal)} quarterly rebalances {rebal[0]}..{rebal[-1]}")

    members_by_date = {d: dict(zip(g["ticker"].astype(str).str.upper(), g["adv"]))
                       for d, g in panel.groupby("date") if d in set(rebal)}
    fcols = ["shares", "cash", "debt_lt", "op_income", "equity", "assets"]
    funds["ticker"] = funds["ticker"].astype(str).str.upper()
    funds_by_date = {}
    for d, g in funds[funds["asof"].isin(set(rebal))].groupby("asof"):
        funds_by_date[d] = {row["ticker"]: {c: row.get(c) for c in fcols}
                            for _, row in g.iterrows()}

    names = sorted({t for m in members_by_date.values() for t in m})
    print(f"[v29] pricing {len(names):,} names + {BENCHMARK}")
    prices = _load_prices(names, start="2015-01-01")
    if BENCHMARK not in prices:
        print(f"[v29] no {BENCHMARK} history — the benchmark is a declared "
              f"criterion and cannot be substituted"); sys.exit(1)

    # ── selection ──────────────────────────────────────────────────────
    schedule, port_rows, tier_counts, gate_counts = [], [], [], collections.Counter()
    for d in rebal:
        members = members_by_date.get(d, {})
        fat = funds_by_date.get(d, {})
        rows = _score_one_date(d, members, fat, prices)
        if not rows:
            continue
        for x in rows:
            gate_counts[x["gate_reason"]] += 1
        eligible_in_tier = sum(1 for x in rows
                               if x["quality_ok"] and x["risk_tier"] == TIER)
        tier_counts.append(eligible_in_tier)
        picked = sc.select_portfolio(rows, tier=TIER, n=N_HOLD)
        if not picked:
            continue
        eff = pd.Timestamp(d) + pd.Timedelta(days=1)
        schedule.append((eff, [p["ticker"] for p in picked]))
        for p in picked:
            port_rows.append({"rebalance": d, "ticker": p["ticker"],
                              "weight": round(p["weight"], 6),
                              "ebit_ev": round(float(p["ebit_ev"]), 6),
                              "risk_score": round(float(p["risk_score"]), 4),
                              "risk_tier": p["risk_tier"],
                              "price": round(float(p["price"]), 4)})

    if not port_rows:
        print("[v29] no portfolio could be formed at any rebalance"); sys.exit(1)
    pf = pd.DataFrame(port_rows)
    os.makedirs(os.path.dirname(OUT_PORT) or ".", exist_ok=True)
    pf.to_csv(OUT_PORT, index=False)

    print("\n" + "=" * 74); print("SELECTION"); print("=" * 74)
    print(f"  quality gate over all candidate name-quarters: "
          f"{dict(gate_counts.most_common())}")
    tc = pd.Series(tier_counts, dtype="float64")
    print(f"\n  eligible names in the {TIER} tier per rebalance: "
          f"min {int(tc.min())}, median {int(tc.median())}, max {int(tc.max())}")
    if tc.median() >= MIN_PER_TIER:
        print(f"  ✅ CRITERION 2 CLEARS: median {int(tc.median())} >= {MIN_PER_TIER}. "
              f"Holding {N_HOLD} is a selection, not a headcount.")
    else:
        print(f"  🔴 CRITERION 2 MISSES: median {int(tc.median())} < {MIN_PER_TIER}. "
              f"The specification MISSES rather than being quietly resized.")
    print(f"\n  {len(schedule)} portfolios formed; {pf['ticker'].nunique()} distinct names")
    print(f"  wrote {OUT_PORT} ({len(pf)} rows)")

    if MODE == "select":
        print("\n[v29] SELECTION ONLY — no return computed, no criterion evaluated,")
        print("      K untouched. Re-run with QT_V29_MODE=read and")
        print("      QT_V29_CONFIRM_READ=1 to spend a slot.")
        return

    # ── the read ───────────────────────────────────────────────────────
    idx = prices[BENCHMARK].index
    cols = sorted({t for _, names_ in schedule for t in names_})
    rets = pd.DataFrame({t: prices[t].reindex(idx).pct_change() for t in cols
                         if t in prices}, index=idx)
    bench = prices[BENCHMARK].pct_change().reindex(idx).fillna(0.0)

    turn = {}
    prev = []
    for eff, names_ in schedule:
        turn[pd.Timestamp(eff)] = ct.turnover(prev, names_)
        prev = names_
    gross = ct.portfolio_daily_returns(schedule, rets)
    net = ct.apply_costs(gross, turn, COST_BPS)
    start = pd.Timestamp(schedule[0][0])
    net, bench = net[net.index >= start], bench[bench.index >= start]

    res = ct.evaluate(net, bench, min_days=1000, min_t=2.0, min_ir=0.5,
                      min_stability=0.5, lag=NW_LAG)
    print("\n" + "=" * 74); print("THE READ"); print("=" * 74)
    for k in ("n_days", "ann_return", "ann_bench", "ann_excess", "t_excess",
              "information_ratio", "max_dd", "max_dd_bench", "stability"):
        print(f"  {k:<20} {res[k]}")
    print(f"\n  CLEARED: {res['cleared']}")
    print(f"  MISSED:  {res['missed']}")
    print(f"\n  VERDICT: {res['verdict']}")
    print("\n[v29] This read is now recorded and CANNOT be repeated. Anti-deferral:")
    print("      no bug found afterwards reopens it.")
    print(json.dumps({"spec": SPEC_ID, "mode": "read", "result": {
        k: (None if isinstance(v, float) and not np.isfinite(v) else v)
        for k, v in res.items() if k != "cleared" and k != "missed"}}, indent=2))


if __name__ == "__main__":
    main()
