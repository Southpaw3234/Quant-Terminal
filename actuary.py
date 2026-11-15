#!/usr/bin/env python3
"""ACTUARY -- modules 4 and 7 of the collective agent.

See docs/COLLECTIVE_AGENT_BRIEF.md §1 and §3.3, and the header of
qt/actuary.py, which states the three rules this agent enforces in code.

    frozen outcome ledgers (reads already spent)  ->  base rates
    OBSERVER's newest day                         ->  what happened today,
                                                      each with its base
                                                      rate or "insufficient
                                                      history"
    data/operator/positions.csv + stance          ->  exposure
    the thesis ledger + LIBRARIAN's brief         ->  falsifier status
                                                  ->  analysis/<day>.json

🔑 THIS AGENT MEASURES NOTHING NEW. Every base rate is a description of a
ledger a pre-registered read already consumed. It does not import the
referee, it does not compute a statistic against a criterion, and it does
not touch the K budget. It reads data/ and never writes it.

🔑 IT NEVER EMITS A VERDICT. Distributions with `n`, concentration figures,
ledger status -- or `null` with the reason. Nothing here says what to do.
"""

from __future__ import annotations

import csv
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import actuary as act
from qt import agentio as io

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time,
# qt.prices, and any broker SDK. validate_actuary.py asserts this with `ast`.

OUT_DIR = os.environ.get("QT_ACT_DIR", "analysis")
OBS_DIR = os.environ.get("QT_OBS_DIR", "observations")
BRIEF_DIR = os.environ.get("QT_LIB_DIR", "briefs")
SOURCES = os.environ.get("QT_ACT_SOURCES", "data/actuary/sources.json")
POSITIONS = os.environ.get("QT_ACT_POSITIONS", "data/operator/positions.csv")
STANCE = os.environ.get("QT_STANCE_FILE", "data/operator/stance.json")
LEDGER = os.environ.get("QT_THESIS_LEDGER", "data/theses/ledger.jsonl")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
FINNHUB = "https://finnhub.io/api/v1"
OBS_MAX_AGE_DAYS = float(os.environ.get("QT_ACT_OBS_MAX_AGE", "4"))
BRIEF_MAX_AGE_DAYS = float(os.environ.get("QT_ACT_BRIEF_MAX_AGE", "4"))


def read_csv_rows(path: str) -> list:
    """Rows as dicts, via the csv module.

    Not pandas: a ticker column read by pandas turns `NA` (Nano Labs) into
    NaN, and a position that parses as missing is a position that vanishes.
    """
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def load_base_rates() -> tuple:
    """-> (rates, sources_report). Read-only over data/."""
    cfg = io.read_json(SOURCES) or {}
    rates, report = [], []
    registries = {}
    for s in cfg.get("sources") or []:
        path = s.get("ledger") or ""
        rows = read_csv_rows(path)
        if not rows:
            # A listed ledger that is missing is reported, not skipped: a
            # base rate that quietly disappears reads as "no history".
            report.append({"ledger": path, "status": "missing", "rows": 0,
                           "reason": "ledger not found or empty"})
            continue
        reg_path = s.get("registry") or ""
        if reg_path not in registries:
            registries[reg_path] = io.read_json(reg_path)
        ctx = act.registry_context(registries[reg_path], s.get("spec_id"))
        src = {"path": path, "rows": len(rows),
               "sha256": io.sha256_normalised(path)}
        got = act.base_rates(rows, src, ctx)
        rates.extend(got)
        report.append({"ledger": path, "status": "ok", "rows": len(rows),
                       "sha256": src["sha256"], "spec_id": s.get("spec_id"),
                       "verdict": ctx.get("verdict"),
                       "event_types": sorted({r["event_type"] for r in got})})
    return rates, report


def finnhub_facts(tickers: list) -> dict:
    """{ticker: {sector, market_cap_musd, adv_dollars, beta, reason}}.

    Any field Finnhub does not return stays None. Without a key every field
    is None and the reason says so -- name concentration is still computed,
    because it needs nothing but the operator's own dollars.
    """
    if not tickers:
        return {}
    if not FINNHUB_KEY:
        return {t: {"sector": None, "market_cap_musd": None,
                    "adv_dollars": None, "beta": None,
                    "reason": "FINNHUB_API_KEY not configured"}
                for t in tickers}
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "research-agent/1.0"})

    def get(path, **params):
        try:
            r = sess.get(f"{FINNHUB}{path}", timeout=20,
                         params=dict(params, token=FINNHUB_KEY))
            time.sleep(1.1)
            return r.json() if r.status_code == 200 else None
        except Exception:                             # noqa: BLE001
            return None

    out = {}
    for t in tickers:
        prof = get("/stock/profile2", symbol=t) or {}
        met = (get("/stock/metric", symbol=t, metric="all") or {}).get(
            "metric") or {}
        quote = get("/quote", symbol=t) or {}
        price = quote.get("c") or None
        vol_m = met.get("3MonthAverageTradingVolume")       # millions of shares
        adv = (vol_m * 1e6 * price) if vol_m and price else None
        missing = [k for k, v in (("sector", prof.get("finnhubIndustry")),
                                  ("market cap", prof.get("marketCapitalization")),
                                  ("ADV", adv), ("beta", met.get("beta")))
                   if not v]
        out[t] = {"sector": prof.get("finnhubIndustry") or None,
                  "market_cap_musd": prof.get("marketCapitalization") or None,
                  "adv_dollars": round(adv, 0) if adv else None,
                  "beta": met.get("beta"),
                  "reason": ("finnhub returned no " + ", ".join(missing))
                  if missing else None}
    return out


def digest(doc: dict) -> str:
    lines = [f"**Analysis — {doc['day']}**  _(distributions and status; "
             f"no recommendation is made anywhere in this document)_"]

    lines.append("")
    lines.append("__Base rates__ — from reads already spent")
    for r in doc["base_rates"]:
        a = r["abnormal"]
        head = (f"· `{r['event_type']}` over {r['horizon_bars']} bars vs "
                f"{r['control']}, n={a['n']}")
        if not a["sufficient"]:
            lines.append(f"{head}: {a['reason']}")
            continue
        lines.append(f"{head}: median {a['median']:+.1%}, middle half "
                     f"{a['q25']:+.1%} to {a['q75']:+.1%}, worst "
                     f"{a['worst']:+.1%}, {a['share_positive']:.0%} positive "
                     f"— registry verdict **{r['registry'].get('verdict') or 'none recorded'}**")
    if not doc["base_rates"]:
        lines.append("_none available_")

    today = doc.get("observed_today") or []
    with_rate = [e for e in today
                 if any("abnormal" in b and b["abnormal"]["sufficient"]
                        for b in e["base_rates"])]
    lines.append("")
    lines.append(f"__Observed {doc['day']}__ — {len(today)} event type(s); "
                 f"{len(with_rate)} with a usable base rate, "
                 f"{len(today) - len(with_rate)} with insufficient history")
    for e in with_rate:
        lines.append(f"· `{e['event_type']}` × {e['count']} — base rate above")

    ex = doc["exposure"]
    lines.append("")
    if ex["status"] != "ok":
        lines.append(f"__Exposure__ — not measured: {ex['reason']}")
    else:
        lines.append(f"__Exposure__ — ${ex['total_dollars']:,.0f} across "
                     f"{ex['n_names']} name(s); largest "
                     f"{ex['single_name_max']:.0%}, top three {ex['top3']:.0%}, "
                     f"effective names {ex['effective_names']}")
        for f in ex["flags"]:
            lines.append(f"· ⚠️ {f}")
    if ex.get("theses_without_a_position"):
        lines.append("· open theses with no declared position: "
                     + ", ".join(f"`{t}`" for t in ex["theses_without_a_position"]))

    fs = doc["falsifiers"]
    lines.append("")
    if doc["ledger"]["ok"] is False:
        lines.append(f"__Theses__ — ⚠️ ledger unusable: {doc['ledger']['why']}")
    elif not fs:
        lines.append("__Theses__ — none open")
    else:
        lines.append(f"__Theses__ — {len(fs)} open")
        for t in fs:
            mark = ("🔴 DRIFTING" if t["drifting"]
                    else "⏰ review due" if t["review_due"] else "·")
            lines.append(f"{mark} `{t['ticker']}` **{t['thesis_id']}** — "
                         f"review {t['review_date']}; "
                         f"{t['evidence_arrived']['observations']} filing(s) "
                         f"arrived")
    return "\n".join(lines)


def main() -> None:
    now = io.now_utc()
    rates, sources = load_base_rates()
    print(f"[actuary] {len(rates)} base rate(s) from "
          f"{sum(1 for s in sources if s['status'] == 'ok')} of "
          f"{len(sources)} frozen ledger(s)")
    for r in rates:
        a = r["abnormal"]
        print(f"  {r['event_type']:<22} {str(r['horizon_bars']) + ' bars':<9} "
              f"n={a['n']:<4} "
              + (f"median {a['median']:+.4f}  IQR {a['q25']:+.4f}..{a['q75']:+.4f}  "
                 f"verdict {r['registry'].get('verdict')}"
                 if a["sufficient"] else a["reason"]))

    # -- upstream: OBSERVER ---------------------------------------------------
    day, path = io.latest_day_file(OBS_DIR, ".jsonl")
    meta = io.read_json(os.path.join(OBS_DIR, f"{day}.meta.json")) if day else None
    obs_status = io.artifact_status(
        "observations", meta if day else None,
        (meta or {}).get("generated_at") or (day or ""), OBS_MAX_AGE_DAYS, now)
    records = io.read_jsonl(path) if day and obs_status["usable"] else []
    print(f"[actuary] observations: {obs_status['status']}"
          + (f" ({day}, {len(records)} records)" if day else ""))

    observed = []
    if obs_status["usable"]:
        for et, count in act.observed_event_types(records).items():
            observed.append({"event_type": et, "count": count,
                             "base_rates": act.lookup(et, rates)})
    clusters = [{"ticker": r.get("ticker"), "id": r.get("id"),
                 "n_insiders": r.get("n_insiders"),
                 "total_value": r.get("total_value"),
                 "base_rates": act.lookup(r.get("event_type"), rates)}
                for r in records if r.get("kind") == "derived_event"]

    # -- upstream: LIBRARIAN --------------------------------------------------
    bday, bpath = io.latest_day_file(BRIEF_DIR, ".json")
    brief = io.read_json(bpath) if bpath else None
    brief_status = io.artifact_status(
        "librarian brief", brief, (brief or {}).get("generated_at") or "",
        BRIEF_MAX_AGE_DAYS, now)
    bearing = (brief or {}).get("bearing_on_theses") if brief_status["usable"] else []
    print(f"[actuary] librarian brief: {brief_status['status']}")

    # -- the operator ---------------------------------------------------------
    stance = io.load_stance(STANCE)
    th = io.open_theses(LEDGER)
    thesis_tickers = sorted({(s.get("ticker") or "").strip().upper()
                             for s in th["theses"]} - {""})
    parsed = act.parse_positions(read_csv_rows(POSITIONS))
    facts = finnhub_facts([p["ticker"] for p in parsed["positions"]])
    ex = act.exposure(parsed, facts, stance["stance"], thesis_tickers)
    print(f"[actuary] stance {stance['stance']} ({stance['source']}); "
          f"exposure {ex['status']}; theses open {len(th['theses'])}")

    doc = {
        "generated_at": now,
        "day": day or io.today_utc(),
        "describes": f"filings published {day}" if day else "no filing day",
        "is_not_a_recommendation": (
            "This document reports distributions with n, concentration "
            "figures and ledger status. It contains no buy, sell, hold, "
            "rating, target or confidence, and nothing in it should be read "
            "as one."),
        "upstream": {"observations": obs_status, "librarian": brief_status},
        "stance": stance,
        "sources": sources,
        "min_n": act.MIN_N,
        "base_rates": rates,
        "observed_today": observed,
        "cluster_events": clusters,
        "exposure": ex,
        "ledger": {"ok": th["ok"], "why": th["why"]},
        "falsifiers": act.falsifier_report(th["theses"], bearing,
                                           io.today_utc()),
    }

    # 🔑 An analysis with no base rates AND no upstream is an outage. It is
    # still written -- the dark state is the finding -- but it must not exit
    # zero looking like a normal night.
    io.write_json(os.path.join(OUT_DIR, f"{doc['day']}.json"), doc)
    text = digest(doc)
    io.write_text(os.path.join(OUT_DIR, f"{doc['day']}.md"), text)
    print(f"\n[actuary] wrote {OUT_DIR}/{doc['day']}.json")
    print("\n" + text[:1800])
    print("\n[actuary] distributions and status only. No verdict, no order, "
          "no read spent, K unchanged.")
    if not rates:
        print("::error::actuary: no base rate could be computed — every "
              "listed ledger was missing")
        sys.exit(5)


if __name__ == "__main__":
    main()
