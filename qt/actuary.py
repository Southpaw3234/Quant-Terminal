"""ACTUARY -- pure logic. Base rates, exposure, falsifier status. Stdlib only.

See docs/COLLECTIVE_AGENT_BRIEF.md §1 and §3.3. Read §1 first; it is the
finding this repository cost a year to produce:

    ACTUARY reports: "of the 47 historical cases resembling this, the median
    90-day outcome was -2%, the interquartile range was -14% to +9%, and 3
    of 47 delisted." It never reports: "BUY."

-----------------------------------------------------------------------------
🔑 THREE RULES THIS MODULE ENFORCES IN CODE

1. A DISTRIBUTION OR NOTHING. `distribution` returns quartiles, the range,
   the hit rate and `n` -- or, below MIN_N cases, a record saying
   "insufficient history" that carries NO statistics at all. Not a median
   with a caveat: a median from four cases is a number that will be read as
   information, and the caveat will not be.

2. BASE RATES COME FROM READS ALREADY SPENT. Every base rate is computed
   from a frozen outcome ledger under data/events/ that a pre-registered
   read has already consumed. This module measures nothing new, so it can
   run nightly without touching the K budget. And each base rate travels
   with that read's VERDICT, because a median abnormal return of +3% beside
   the words "NOT MET" is a very different sentence from the same median
   alone -- the registry already established the average is not
   distinguishable from zero at the declared threshold.

3. NO VERDICT, EVER. There is no function here that returns buy, sell,
   hold, a rating, a target or a confidence. `validate_actuary.py` asserts
   that over the module's identifiers and over the keys of everything it
   emits. Stance changes which concentration lines are FLAGGED; it never
   changes a number.

-----------------------------------------------------------------------------
🔑 A FALSIFIER IS EVALUATED BY THE OPERATOR, AND THIS SAYS SO

Falsifiers are free text ("two consecutive quarters of declining active
customers"). This module cannot test one and does not pretend to. It
reports what the ledger records -- fired, overridden, unaddressed -- and
what evidence has ARRIVED for the name since, so the operator has the
filing in front of them at review. `arrived` is reported as evidence
counts; whether a falsifier is met is left null with the reason.
"""

from __future__ import annotations

import math

MIN_N = 30

# Concentration lines a stance FLAGS. Presentation only: every number below
# is computed identically for every stance, and validate_actuary.py proves
# it. A stance decides what is called out, not what is true.
STANCE_LIMITS = {
    "conservative": {"single_name": 0.10, "top3": 0.30, "sector": 0.25,
                     "days_to_exit": 3.0},
    "balanced":     {"single_name": 0.20, "top3": 0.50, "sector": 0.40,
                     "days_to_exit": 5.0},
    "aggressive":   {"single_name": 0.33, "top3": 0.70, "sector": 0.60,
                     "days_to_exit": 10.0},
}
STANCES = tuple(STANCE_LIMITS)
ADV_PARTICIPATION = 0.10       # exit assumed at 10% of average daily volume
SIZE_BUCKETS = ((300.0, "micro (<$300M)"), (2000.0, "small ($300M–$2B)"),
                (10000.0, "mid ($2B–$10B)"), (float("inf"), "large (>$10B)"))


# ── 1. distributions ─────────────────────────────────────────────────────────

def _clean(values) -> list:
    out = []
    for v in values or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return sorted(out)


def quantile(sorted_vals: list, q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not sorted_vals:
        raise ValueError("quantile of nothing")
    pos = (len(sorted_vals) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def distribution(values, min_n: int = MIN_N) -> dict:
    """The full shape of what happened, with `n` -- or "insufficient history".

    🔑 Below `min_n` the record carries NO statistics. A median from four
    cases is not a small amount of information; it is a number that will be
    read as information.
    """
    vals = _clean(values)
    n = len(vals)
    if n < min_n:
        return {"n": n, "sufficient": False, "min_n": min_n,
                "reason": f"insufficient history: {n} case(s), fewer than "
                          f"the {min_n} needed to report a distribution"}
    return {"n": n, "sufficient": True, "min_n": min_n,
            "median": round(quantile(vals, 0.50), 6),
            "q25": round(quantile(vals, 0.25), 6),
            "q75": round(quantile(vals, 0.75), 6),
            "p05": round(quantile(vals, 0.05), 6),
            "p95": round(quantile(vals, 0.95), 6),
            "worst": round(vals[0], 6), "best": round(vals[-1], 6),
            "mean": round(sum(vals) / n, 6),
            "share_positive": round(sum(1 for v in vals if v > 0) / n, 4)}


def insufficient(event_type: str, why: str) -> dict:
    """The answer for an event type with no usable history."""
    return {"event_type": event_type, "n": 0, "sufficient": False,
            "reason": f"insufficient history: {why}"}


# ── 2. base rates from frozen ledgers ────────────────────────────────────────

def find_verdict(result) -> "str | None":
    """The first `verdict` string anywhere inside a registry result block.

    Registry entries nest it differently (`result.verdict`,
    `result.read.verdict`), so this walks rather than assuming a path -- and
    returns None, not a guess, when there is none.
    """
    if isinstance(result, dict):
        v = result.get("verdict")
        if isinstance(v, str) and v.strip():
            return v.strip()
        for child in result.values():
            got = find_verdict(child)
            if got:
                return got
    elif isinstance(result, list):
        for child in result:
            got = find_verdict(child)
            if got:
                return got
    return None


def registry_context(registry_doc: dict, spec_id: str) -> dict:
    """What the pre-registered read on this ledger concluded."""
    specs = (registry_doc or {}).get("specifications") or {}
    spec = specs.get(spec_id) if isinstance(specs, dict) else next(
        (s for s in specs if isinstance(s, dict)
         and s.get("spec_id") == spec_id), None)
    if not spec:
        return {"spec_id": spec_id, "verdict": None,
                "reason": "specification not found in the registry"}
    verdict = find_verdict(spec.get("result"))
    return {"spec_id": spec_id, "verdict": verdict,
            "read_at": spec.get("read_at"),
            "declared_at": spec.get("declared_at"),
            "reason": "ok" if verdict else "the registry records no verdict "
                                           "for this specification"}


def base_rates(rows: list, source: dict, registry: dict,
               min_n: int = MIN_N) -> list:
    """Distributions per (event type, horizon) from one frozen ledger.

    `rows` are the ledger's rows as dicts. Returns are reported two ways,
    because they answer different questions: `abnormal` is the return over
    the control, `raw` is what a holder actually experienced.
    """
    groups = {}
    for r in rows or []:
        et = str(r.get("event_type") or "").strip()
        if not et:
            continue
        groups.setdefault((et, str(r.get("horizon") or "").strip()),
                          []).append(r)
    out = []
    for (et, horizon), grp in sorted(groups.items()):
        exits = {}
        for r in grp:
            k = str(r.get("exit_status") or "not recorded").strip() \
                or "not recorded"
            exits[k] = exits.get(k, 0) + 1
        dates = sorted(str(r.get("event_ts") or "")[:10] for r in grp
                       if r.get("event_ts"))
        controls = sorted({str(r.get("control") or "").strip()
                           for r in grp} - {""})
        verdict = registry.get("verdict")
        out.append({
            "event_type": et,
            "horizon_bars": int(horizon) if horizon.isdigit() else None,
            "control": controls[0] if len(controls) == 1 else controls,
            "n_events": len(grp),
            "abnormal": distribution([r.get("abnormal_ret") for r in grp],
                                     min_n),
            "raw": distribution([r.get("event_ret") for r in grp], min_n),
            "exits": exits,
            "sample": {"from": dates[0] if dates else None,
                       "to": dates[-1] if dates else None},
            "source": source,
            "registry": registry,
            # 🔑 The sentence that has to travel with every number above.
            "how_to_read": (
                "What followed past events of this type — a description, "
                "not a forecast. "
                + (f"The pre-registered read on this ledger returned "
                   f"{verdict}: the average was not established as different "
                   f"from zero at the declared threshold."
                   if verdict else
                   "No pre-registered verdict is recorded for this ledger.")),
        })
    return out


def lookup(event_type: str, rates: list) -> list:
    """Every base rate for an event type, or ONE insufficient-history record.

    The brief's done-when for this module: "any event type returns a
    distribution with n, or 'insufficient history'". There is no third
    answer and no empty list.
    """
    hits = [r for r in rates or [] if r["event_type"] == event_type]
    return hits or [insufficient(
        event_type, "no frozen outcome ledger exists for this event type")]


def observed_event_types(records: list) -> dict:
    """{event_type: count} for one day's OBSERVER records.

    Names are mechanical -- the form, the item, the direction -- so the same
    filing always maps to the same type and nothing here is a judgment.
    """
    out = {}

    def bump(k):
        out[k] = out.get(k, 0) + 1

    for r in records or []:
        if r.get("kind") == "derived_event":
            bump(str(r.get("event_type")))
            continue
        form = r.get("root_form") or r.get("form") or ""
        if form == "8-K":
            for item in r.get("items") or ["(no item listed)"]:
                bump(f"8k_item_{item}")
        elif str(form).startswith("4"):
            bump(f"form4_{r.get('direction') or 'direction_not_observed'}")
        elif form:
            bump(f"filing_{form}")
    return dict(sorted(out.items()))


# ── 3. exposure ──────────────────────────────────────────────────────────────

def parse_positions(rows: list) -> dict:
    """Operator-declared positions -> {"positions", "rejected", "as_of"}.

    A row is {ticker, dollars[, as_of]}. Dollars must be a positive number;
    anything else is REJECTED and listed, never coerced to zero -- a
    position silently valued at $0 disappears from every concentration
    figure, which is the most dangerous way to be wrong about exposure.
    """
    pos, rejected, dates = {}, [], []
    for i, r in enumerate(rows or [], 1):
        tk = str(r.get("ticker") or "").strip().upper()
        raw = str(r.get("dollars") or "").replace(",", "").replace("$", "").strip()
        if not tk and not raw:
            continue
        try:
            dollars = float(raw)
        except ValueError:
            dollars = float("nan")
        if not tk or not math.isfinite(dollars) or dollars <= 0:
            rejected.append({"row": i, "ticker": tk or None, "dollars": raw,
                             "reason": "needs a ticker and a positive dollar "
                                       "amount"})
            continue
        pos[tk] = pos.get(tk, 0.0) + dollars
        if str(r.get("as_of") or "").strip():
            dates.append(str(r["as_of"]).strip()[:10])
    return {"positions": [{"ticker": t, "dollars": round(d, 2)}
                          for t, d in sorted(pos.items())],
            "rejected": rejected,
            "as_of": max(dates) if dates else None}


def _bucket(market_cap_musd) -> "str | None":
    if market_cap_musd is None:
        return None
    for limit, name in SIZE_BUCKETS:
        if market_cap_musd < limit:
            return name
    return None


def _grouped(positions: list, total: float, key) -> list:
    """Weights summed by a key. A name whose key is unknown goes to an
    explicit `not observed` bucket -- never dropped, never guessed."""
    agg = {}
    for p in positions:
        k = key(p) or "not observed"
        agg.setdefault(k, {"dollars": 0.0, "names": []})
        agg[k]["dollars"] += p["dollars"]
        agg[k]["names"].append(p["ticker"])
    return sorted(({"group": k, "weight": round(v["dollars"] / total, 4),
                    "dollars": round(v["dollars"], 2),
                    "names": sorted(v["names"])} for k, v in agg.items()),
                  key=lambda x: (-x["weight"], x["group"]))


def exposure(parsed: dict, facts: dict, stance: str,
             open_thesis_tickers: list = None) -> dict:
    """Concentration by name, sector, size and liquidity.

    `facts` is {ticker: {"sector", "market_cap_musd", "adv_dollars",
    "beta", "reason"}} -- any field may be None, and None stays None.

    🔑 Every number is computed the same way for every stance. `stance`
    selects only the limits a line is FLAGGED against.
    """
    limits = STANCE_LIMITS.get(stance, STANCE_LIMITS["balanced"])
    positions = parsed.get("positions") or []
    theses = sorted(set(open_thesis_tickers or []))
    held = {p["ticker"] for p in positions}
    base = {"stance": stance if stance in STANCE_LIMITS else "balanced",
            "limits": limits, "as_of": parsed.get("as_of"),
            "rejected_rows": parsed.get("rejected") or [],
            "theses_without_a_position": [t for t in theses if t not in held],
            "positions_without_a_thesis":
                sorted(t for t in held if t not in theses)}
    if not positions:
        # 🔑 Null with a reason. "0% concentrated" would be a measurement of
        # a portfolio nobody declared.
        return dict(base, status="no_positions", total_dollars=None,
                    reason="no positions are declared in "
                           "data/operator/positions.csv, so there is nothing "
                           "to measure concentration of")

    total = sum(p["dollars"] for p in positions)
    rows = []
    for p in sorted(positions, key=lambda x: (-x["dollars"], x["ticker"])):
        f = facts.get(p["ticker"]) or {}
        adv = f.get("adv_dollars")
        days = (round(p["dollars"] / (adv * ADV_PARTICIPATION), 2)
                if adv and adv > 0 else None)
        rows.append({"ticker": p["ticker"], "dollars": p["dollars"],
                     "weight": round(p["dollars"] / total, 4),
                     "sector": f.get("sector"),
                     "size": _bucket(f.get("market_cap_musd")),
                     "beta": f.get("beta"),
                     "adv_dollars": adv,
                     # None = ADV not observed. Never "0 days".
                     "days_to_exit": days,
                     "facts_reason": f.get("reason")})

    w = [r["weight"] for r in rows]
    hhi = sum(x * x for x in w)
    betas = [(r["weight"], r["beta"]) for r in rows if r["beta"] is not None]
    beta_cov = sum(x for x, _ in betas)
    dte = [r["days_to_exit"] for r in rows if r["days_to_exit"] is not None]

    flags = []
    if w[0] > limits["single_name"]:
        flags.append(f"{rows[0]['ticker']} is {w[0]:.0%} of the book — over "
                     f"the {limits['single_name']:.0%} single-name line for "
                     f"a {base['stance']} stance")
    if sum(w[:3]) > limits["top3"] and len(w) > 3:
        flags.append(f"the top three names are {sum(w[:3]):.0%} — over the "
                     f"{limits['top3']:.0%} line")
    by_sector = _grouped(rows, total, lambda r: r["sector"])
    for g in by_sector:
        if g["group"] != "not observed" and g["weight"] > limits["sector"]:
            flags.append(f"{g['group']} is {g['weight']:.0%} — over the "
                         f"{limits['sector']:.0%} sector line")
    for r in rows:
        if r["days_to_exit"] is not None \
                and r["days_to_exit"] > limits["days_to_exit"]:
            flags.append(f"{r['ticker']} would take {r['days_to_exit']:g} "
                         f"days to exit at {ADV_PARTICIPATION:.0%} of average "
                         f"volume — over the {limits['days_to_exit']:g}-day line")

    return dict(
        base, status="ok", total_dollars=round(total, 2), n_names=len(rows),
        names=rows,
        single_name_max=w[0], top3=round(sum(w[:3]), 4),
        top5=round(sum(w[:5]), 4),
        effective_names=round(1.0 / hhi, 2) if hhi > 0 else None,
        by_sector=by_sector,
        by_size=_grouped(rows, total, lambda r: r["size"]),
        # A book beta over only the part whose beta was observed, WITH the
        # coverage, so a number built on 40% of the book cannot pass as one
        # built on all of it.
        beta={"weighted": round(sum(x * b for x, b in betas) / beta_cov, 3)
              if beta_cov > 0 else None,
              "coverage": round(beta_cov, 4),
              "reason": None if beta_cov > 0 else "no beta was observed for "
                                                  "any position"},
        liquidity={"slowest_days_to_exit": max(dte) if dte else None,
                   "observed_for": len(dte), "of": len(rows),
                   "assumes": f"selling at {ADV_PARTICIPATION:.0%} of "
                              f"average daily dollar volume"},
        flags=flags)


# ── 4. falsifiers ────────────────────────────────────────────────────────────

def falsifier_report(theses: list, bearing: list, today: str) -> list:
    """Per open thesis: what the ledger records, and what has arrived.

    `theses` are state dicts from qt.thesis; `bearing` is LIBRARIAN's
    `bearing_on_theses`. Nothing here decides whether a falsifier is met.
    """
    by_id = {b.get("thesis_id"): b for b in bearing or []}
    out = []
    for t in theses or []:
        overridden = set(t.get("overridden") or [])
        fired = list(t.get("fired") or [])
        rows = []
        for f in t.get("falsifiers") or []:
            status = ("fired_overridden" if f in overridden
                      else "fired_unaddressed" if f in fired
                      else "not_recorded_as_fired")
            rows.append({"falsifier": f, "ledger_status": status,
                         # 🔑 null, with the reason. A free-text condition
                         # cannot be tested by this agent.
                         "met": None,
                         "met_reason": "free text; judged by the operator "
                                       "at review, not by this agent"})
        b = by_id.get(t.get("thesis_id")) or {}
        obs = b.get("observations") or []
        review = t.get("review_date") or ""
        out.append({
            "thesis_id": t.get("thesis_id"), "ticker": t.get("ticker"),
            "claim": t.get("claim"), "stance_at_open": t.get("stance"),
            "review_date": review or None,
            "review_due": bool(review and review <= today),
            "falsifiers": rows,
            "drifting": any(r["ledger_status"] == "fired_unaddressed"
                            for r in rows),
            "evidence_arrived": {
                "observations": len(obs),
                "ids": [o.get("id") for o in obs][:20],
                "days_searched": b.get("days_searched") or [],
                "note": "filings naming this ticker in the days searched — "
                        "evidence to read, not a finding that a falsifier "
                        "was met"},
        })
    return sorted(out, key=lambda x: (not x["drifting"], not x["review_due"],
                                      x["review_date"] or "9999",
                                      x["thesis_id"] or ""))
