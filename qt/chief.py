"""CHIEF OF STAFF -- pure logic for the daily brief. Stdlib only.

See docs/COLLECTIVE_AGENT_BRIEF.md §3.4. One page a day, composed from what
the other three agents produced, ordered by the operator's stance.

-----------------------------------------------------------------------------
🔑 STANCE ORDERS AND CAPS. IT NEVER CHANGES A FACT.

The brief is built in two steps and the split is the whole design:

    facts(inputs)            every section, every line, no stance in sight
    compose(facts, stance)   picks an ORDER and a CAP per section

`compose` receives finished lines and can only reorder sections and show
fewer lines of one. It has no access to the inputs, so it cannot compute a
different number for an aggressive operator than for a conservative one --
not because it is careful, but because the numbers are already strings by
the time it runs. validate_chief_of_staff.py proves every composed line is
one of the fact lines, under every stance.

-----------------------------------------------------------------------------
🔑 THE ATTENTION BLOCK IS NOT SUBJECT TO STANCE

Three things are pinned to the top for everyone, uncapped: an upstream feed
that is dark, a thesis that is DRIFTING (a falsifier fired, nothing
recorded), and a review that is due. Those are not opinions about the
market that a risk appetite should reorder. They are the record-keeping the
operator asked this system to hold them to.

-----------------------------------------------------------------------------
🔑 A DARK INPUT IS A SECTION THAT SAYS SO

Every section carries the freshness verdict of the artifact it came from.
A missing or stale input produces a section whose only line is that it is
dark and why -- never a section quietly left out, and never yesterday's
lines. The operator must be able to tell "nothing happened" from "we could
not look" on every line of this page.

-----------------------------------------------------------------------------
THIS AGENT RECORDS AND RELAYS. It places no order, moves no money, makes no
recommendation, and appends nothing to the thesis ledger: an entry there is
the operator's own act.
"""

from __future__ import annotations

import re

STANCES = ("conservative", "balanced", "aggressive")

ORDER = {
    "conservative": ("theses", "exposure", "risk", "language", "macro",
                     "filings", "insiders", "base_rates", "universe"),
    "balanced":     ("theses", "filings", "language", "insiders",
                     "base_rates", "exposure", "risk", "macro", "universe"),
    "aggressive":   ("insiders", "base_rates", "filings", "language",
                     "theses", "exposure", "risk", "macro", "universe"),
}
CAPS = {
    "conservative": {"theses": 60, "exposure": 40, "risk": 30,
                     "language": 25, "macro": 22,
                     "filings": 14, "insiders": 8, "base_rates": 6,
                     "universe": 6},
    "balanced":     {"theses": 60, "exposure": 30, "risk": 20,
                     "language": 15, "macro": 18,
                     "filings": 20, "insiders": 14, "base_rates": 6,
                     "universe": 6},
    "aggressive":   {"theses": 40, "exposure": 20, "risk": 14,
                     "language": 10, "macro": 14,
                     "filings": 30, "insiders": 30, "base_rates": 6,
                     "universe": 6},
}
TITLES = {"theses": "Your theses", "exposure": "Exposure",
          "risk": "Risk — volatility and loss bounds",
          "language": "Filing language", "macro": "Macro and headlines",
          "filings": "Filings in the watched universe",
          "insiders": "Insider activity", "base_rates": "Base rates",
          "universe": "Universe and benchmarks"}

ACK_MARKER = "<!-- chief-of-staff:ack -->"
STANCE_RE = re.compile(r"\*\*Stance:\*\*\s*`(conservative|balanced|aggressive)`")
TITLE_RE = re.compile(r"^\s*\[(conservative|balanced|aggressive)\]")
FROM_DASHBOARD = "the Quant Terminal dashboard"


def line(text: str, url: str = None) -> dict:
    return {"text": str(text), "url": url or None}


def _section(sid: str, lines: list, status: dict = None, note: str = "") -> dict:
    return {"id": sid, "title": TITLES[sid], "lines": lines, "note": note,
            "dark": False, "source": status}


def _dark(sid: str, status: dict) -> dict:
    """A section whose input is unusable. One line, and it says why."""
    return {"id": sid, "title": TITLES[sid], "dark": True, "note": "",
            "source": status,
            "lines": [line(f"⚫ DARK — {status['why']}. Nothing is shown "
                           f"rather than something stale.")]}


def pct(x, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:+.{digits}%}"


# ── facts: one builder per section, no stance anywhere ───────────────────────

def theses_section(analysis: dict, status: dict) -> dict:
    if not status["usable"]:
        return _dark("theses", status)
    led = analysis.get("ledger") or {}
    if led.get("ok") is False:
        return _section("theses", [line(
            f"⚠️ The thesis ledger is unusable — {led.get('why')}. No thesis "
            f"is reported from a record that does not verify.")], status)
    rows = analysis.get("falsifiers") or []
    if not rows:
        return _section("theses", [line(
            "No thesis is open. Record one with `python thesis_ledger.py "
            "open` — it needs a falsifier and a review date.")], status)
    out = []
    for t in rows:
        # Both can be true at once, and both are shown: a thesis that is
        # drifting AND overdue is two obligations, not one.
        mark = (("🔴 DRIFTING " if t["drifting"] else "")
                + ("⏰ REVIEW DUE " if t["review_due"] else ""))
        out.append(line(f"{mark}{t['ticker'] or '—'} · {t['thesis_id']} — "
                        f"{(t.get('claim') or '')[:110]} (review "
                        f"{t['review_date']})"))
        for f in t["falsifiers"]:
            tag = {"fired_unaddressed": "🔴 FIRED, nothing recorded",
                   "fired_overridden": "↪ fired, overridden in writing",
                   "not_recorded_as_fired": "○"}[f["ledger_status"]]
            out.append(line(f"    {tag}  {f['falsifier'][:120]}"))
        ev = t["evidence_arrived"]
        out.append(line(f"    {ev['observations']} filing(s) naming "
                        f"{t['ticker']} arrived in the "
                        f"{len(ev['days_searched'])} day(s) searched"
                        + (" — read them against the falsifiers above"
                           if ev["observations"] else "")))
    return _section("theses", out, status,
                    "Falsifiers are judged by you at review. This page "
                    "reports what the ledger records and what has arrived.")


def exposure_section(analysis: dict, status: dict) -> dict:
    if not status["usable"]:
        return _dark("exposure", status)
    ex = analysis.get("exposure") or {}
    out = []
    if ex.get("status") != "ok":
        out.append(line(f"Not measured — {ex.get('reason')}."))
    else:
        out.append(line(f"${ex['total_dollars']:,.0f} across {ex['n_names']} "
                        f"name(s) · largest {ex['single_name_max']:.0%} · "
                        f"top three {ex['top3']:.0%} · effective names "
                        f"{ex['effective_names']}"))
        for f in ex.get("flags") or []:
            out.append(line(f"⚠️ {f}"))
        for g in ex.get("by_sector") or []:
            out.append(line(f"sector {g['group']}: {g['weight']:.0%} "
                            f"({', '.join(g['names'])})"))
        liq = ex.get("liquidity") or {}
        if liq.get("slowest_days_to_exit") is not None:
            out.append(line(f"slowest exit {liq['slowest_days_to_exit']:g} "
                            f"day(s), {liq['assumes']} (observed for "
                            f"{liq['observed_for']} of {liq['of']})"))
        b = ex.get("beta") or {}
        if b.get("weighted") is not None:
            out.append(line(f"book beta {b['weighted']} over "
                            f"{b['coverage']:.0%} of the book"))
        for n in ex.get("names") or []:
            out.append(line(f"{n['ticker']}: {n['weight']:.0%} "
                            f"(${n['dollars']:,.0f})"
                            + (f", {n['days_to_exit']:g} day(s) to exit"
                               if n["days_to_exit"] is not None else "")))
        for r in ex.get("rejected_rows") or []:
            out.append(line(f"⚠️ positions.csv row {r['row']} rejected: "
                            f"{r['reason']}"))
    if ex.get("theses_without_a_position"):
        out.append(line("open theses with no declared position: "
                        + ", ".join(ex["theses_without_a_position"])))
    if ex.get("positions_without_a_thesis"):
        out.append(line("positions with no thesis on record: "
                        + ", ".join(ex["positions_without_a_thesis"])))
    lim = ex.get("limits") or {}
    return _section("exposure", out, status,
                    f"Lines are flagged against the {ex.get('stance')} "
                    f"limits (single name {lim.get('single_name', 0):.0%}); "
                    f"the figures are the same under every stance.")


def risk_section(analysis: dict, status: dict) -> dict:
    """Volatility and loss bounds: how far, never which way."""
    if not status["usable"]:
        return _dark("risk", status)
    vb = analysis.get("volatility") or {}
    if vb.get("status") != "ok":
        src = vb.get("source") or {}
        return _section("risk", [line(
            f"⚫ Not computed — prices are {src.get('status', 'missing')}: "
            f"{src.get('why', 'no price file')}")], status)
    out = []
    rd = analysis.get("risk_dollars") or {}
    if rd.get("status") == "ok":
        out.append(line(
            f"If every position had its 1-in-100 day at once: "
            f"${rd['sum_usd']:,.0f} on ${rd['covered_usd']:,.0f} covered "
            f"({rd['coverage']:.0%} of the book). An upper bound, not a "
            f"portfolio estimate."))
        for n in rd["names"]:
            out.append(line(
                f"{n['ticker']}: 1-day 99% loss bound "
                + (f"{n['loss_1d_99_pct']:+.1f}% (${n['loss_1d_99_usd']:,.0f})"
                   if n["loss_1d_99_usd"] is not None else
                   f"not available — {n['reason']}")))
    order = {"position": 0, "thesis": 1, "cluster buy": 2, "benchmark": 3}
    for t, d in sorted((vb.get("names") or {}).items(),
                       key=lambda kv: (min(order.get(w, 9) for w in kv[1]["why"]),
                                       kv[0])):
        tag = "/".join(d["why"])
        if d["status"] != "ok":
            out.append(line(f"{t} ({tag}): {d.get('fallback_reason')}"))
            continue
        lo, hi = d["band_21d_pct"]
        out.append(line(
            f"{t} ({tag}): {d['vol_ann_pct']:.0f}%/yr now vs "
            f"{d['realized_63d_ann_pct']:.0f}% over the last 3 months · "
            f"1-day 99% loss bound {d['loss_1d_99_pct']:+.1f}% · "
            f"1-month 90% band {lo:+.0f}% to {hi:+.0f}% · {d['model']}"
            + (f" (fallback: {d['fallback_reason']})"
               if d.get("fallback_reason") else "")))
    for t, why in sorted((vb.get("missing") or {}).items()):
        out.append(line(f"⚫ {t}: no price history — {why}"))
    if not out:
        out.append(line("No name to describe."))
    return _section("risk", out, status,
                    "How far each name has tended to move, not which way. The "
                    "1-day figure is breached one day in a hundred by "
                    "construction; the 1-month band is not a target. Cluster-"
                    "buy names are shown for context, not as suggestions.")


def language_section(brief: dict, status: dict) -> dict:
    if not status["usable"]:
        return _dark("language", status)
    s = brief.get("summary") or {}
    refused = len(brief.get("refused_lookahead") or [])
    out = [line(f"{brief.get('pairs_compared', 0)} filing pair(s) compared "
                f"across {brief.get('issuers_read', 0)} issuer(s): "
                f"{s.get('removed', 0)} removed, {s.get('added', 0)} added, "
                f"{s.get('retained', 0)} retained, "
                f"{s.get('indeterminate', 0)} indeterminate")]
    if not brief.get("claims") and refused:
        out.append(line(f"🔑 Nothing was testable — all {refused} phrase "
                        f"test(s) were refused because the phrase was "
                        f"declared on or after the filing. That is not "
                        f"'nothing changed'."))
    order = {"removed": 0, "added": 1}
    notable = sorted((c for c in brief.get("claims") or [] if c.get("notable")),
                     key=lambda c: (order.get(c["transition"], 9),
                                    c["ticker"], c["phrase"]))
    for c in notable:
        mark = "🔻 REMOVED" if c["transition"] == "removed" else "🔹 added"
        out.append(line(f"{mark} {c['ticker']} {c['form']} — "
                        f"\"{c['phrase']}\" ({c['baseline']['filed']} → "
                        f"{c['current']['filed']})"))
    if s.get("indeterminate"):
        out.append(line(f"⚠️ {s['indeterminate']} claim(s) indeterminate — "
                        f"the filing was not in the index, so zero hits is "
                        f"not a removal"))
    if brief.get("names_truncated"):
        out.append(line(f"⚠️ capped at {brief.get('max_names')} issuers of "
                        f"{brief.get('issuers_eligible')} — "
                        f"{brief.get('selection_rule')}"))
    for r in (brief.get("baseline_unavailable") or [])[:3]:
        out.append(line(f"⚠️ {r['ticker']} {r['form']}: {r['reason']}"))
    return _section("language", out, status,
                    "A phrase-presence diff over pre-declared phrases, not a "
                    "section diff: language nobody declared is invisible "
                    "here, and Item 1A vs Item 7 is not observed.")


def macro_section(macro: dict, status: dict) -> dict:
    if not status["usable"]:
        return _dark("macro", status)
    out = []
    for s in macro.get("series") or []:
        if s["status"] == "fresh":
            chg = ("" if s.get("change") is None
                   else f" ({s['change']:+g} vs {s['previous_as_of']})")
            out.append(line(f"{s['title']}: {s['value']:g} {s['units']} as "
                            f"of {s['as_of']}{chg}", s.get("url")))
        else:
            out.append(line(f"⚫ {s['title']}: {s['status']} — {s['reason']}",
                            s.get("url")))
    news = macro.get("news") or {}
    if news.get("status") != "ok":
        out.append(line(f"⚫ Headlines not collected — {news.get('reason')}"))
    else:
        for tk, c in sorted((news.get("by_thesis") or {}).items()):
            if c.get("status") != "ok":
                out.append(line(f"⚫ {tk} headlines not collected — "
                                f"{c.get('reason')}"))
            for it in c.get("items") or []:
                out.append(line(f"{tk}: {it['headline']}", it["url"]))
        m = news.get("market") or {}
        for it in m.get("items") or []:
            out.append(line(it["headline"], it["url"]))
    return _section("macro", out, status,
                    "Collected, not curated: series state their own age and "
                    "headlines are newest-first, not most-important-first.")


def filings_section(records: list, meta: dict, status: dict) -> dict:
    if not status["usable"]:
        return _dark("filings", status)
    totals = meta.get("totals") or {}
    out = []
    for form, t in totals.items():
        out.append(line(f"{form}: {t.get('in_universe', '?')} in the watched "
                        f"universe of {t.get('filings', '?')} market-wide"
                        + (" — SAMPLED, not a full census"
                           if t.get("truncated") else "")))
    items = {}
    for r in records:
        if r.get("kind", "filing") == "filing" and r.get("root_form") == "8-K":
            for it in r.get("items") or []:
                items[it] = items.get(it, 0) + 1
    if items:
        out.append(line("8-K items: " + ", ".join(
            f"{k} × {v}" for k, v in sorted(items.items()))))
    for r in sorted((r for r in records
                     if r.get("kind", "filing") == "filing"
                     and r.get("root_form") in ("8-K", "424B5", "10-K")),
                    key=lambda r: (r["ticker"], r["accession"])):
        detail = ("item " + ", ".join(str(i) for i in r["items"][:4])
                  if r.get("items") else r["form"])
        out.append(line(f"{r['ticker']} — {r['form']}"
                        + ("" if detail == r["form"] else f" {detail}"),
                        r.get("url")))
    return _section("filings", out, status,
                    f"Filing day {meta.get('filing_day')}. Listed "
                    f"alphabetically — nothing here is ordered by importance.")


def insiders_section(records: list, meta: dict, rates: list,
                     status: dict, resolved: list = None) -> dict:
    """Insider activity.

    `records` is the newest filing day; `resolved` is what OBSERVER resolved
    in its latest run across the recheck window (agentio.resolved_in_run).
    🔑 Purchases and clusters come from `resolved` when it is given: the
    newest day's directions mostly arrive two nights later, so reading them
    from `records` showed nothing at all for nine nights running.
    """
    if not status["usable"]:
        return _dark("insiders", status)
    f4 = [r for r in records if r.get("kind", "filing") == "filing"
          and r.get("root_form") == "4"]
    src = records if resolved is None else resolved
    clusters = [r for r in src if r.get("kind") == "derived_event"]
    out = []
    counts = {}
    for r in f4:
        k = r.get("direction") or "not observed"
        counts[k] = counts.get(k, 0) + 1
    day = meta.get("filing_day") or "the newest day"
    out.append(line(f"{len(f4)} Form 4 filing(s) on {day} in the watched "
                    f"universe: "
                    + (", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
                       or "none")))
    if counts.get("not observed"):
        out.append(line(f"⚠️ {counts['not observed']} have no observed "
                        f"direction YET — Finnhub reports direction about two "
                        f"days after EDGAR, so they resolve in a later brief. "
                        f"Until then they are filings, NOT purchases; insider "
                        f"sales outnumber buys."))
    if resolved is not None:
        rf = [r for r in resolved if r.get("kind", "filing") == "filing"]
        rc = {}
        for r in rf:
            rc[r["direction"]] = rc.get(r["direction"], 0) + 1
        days = sorted({r["day"] for r in resolved})
        out.append(line(
            f"Resolved since the last brief: {len(rf)} Form 4 direction(s)"
            + (f" for filings from {', '.join(d[5:] for d in days)}"
               if days else "") + ": "
            + (", ".join(f"{v} {k}" for k, v in sorted(rc.items())) or "none")
            + (f"; {len(clusters)} cluster buy(s)" if clusters else "")))
    for c in sorted(clusters, key=lambda r: r["ticker"]):
        when = f" (filed {c['day'][5:]})" if c.get("day") else ""
        out.append(line(f"◆ CLUSTER {c['ticker']} — {c['n_insiders']} "
                        f"insiders, ${c['total_value']:,.0f}{when} (frozen "
                        f"definition)"))
    pf4 = [r for r in src if r.get("kind", "filing") == "filing"
           and r.get("root_form") == "4"]
    for r in sorted((r for r in pf4 if r.get("direction")
                     in ("purchase", "mixed")),
                    key=lambda r: (r["ticker"], r["accession"])):
        val = (f" ${r['purchase_value']:,.0f}"
               if r.get("purchase_value") else "")
        who = ", ".join((r.get("insiders") or [])[:2])
        first = ""
        if r.get("first_purchase"):
            fp = r["first_purchase"][0]
            first = (f" — first open-market purchase by {fp['insider']} in "
                     f"the history available (since {fp['history_from']})")
        when = f" (filed {r['day'][5:]})" if r.get("day") else ""
        out.append(line(f"{r['ticker']} — open-market {r['direction']}{val}"
                        + (f" by {who}" if who else "") + first + when,
                        r.get("url")))
    anchor = ""
    for r in rates or []:
        a = r.get("abnormal") or {}
        if r.get("event_type") == "form4_cluster_buy" and a.get("sufficient"):
            anchor += (f" Past cluster buys over {r['horizon_bars']} bars "
                       f"(n={a['n']}): median {pct(a['median'])} vs "
                       f"{r['control']}, middle half {pct(a['q25'])} to "
                       f"{pct(a['q75'])}; registry verdict "
                       f"{(r.get('registry') or {}).get('verdict')}.")
    return _section("insiders", out, status,
                    "A purchase is a code-P open-market transaction read "
                    "from Finnhub by accession." + anchor)


def base_rates_section(analysis: dict, status: dict) -> dict:
    if not status["usable"]:
        return _dark("base_rates", status)
    out = []
    for r in analysis.get("base_rates") or []:
        a = r["abnormal"]
        head = (f"{r['event_type']} over {r['horizon_bars']} bars vs "
                f"{r['control']}, n={a['n']}")
        if not a["sufficient"]:
            out.append(line(f"{head}: {a['reason']}"))
            continue
        out.append(line(
            f"{head}: median {pct(a['median'])}, middle half "
            f"{pct(a['q25'])} to {pct(a['q75'])}, worst {pct(a['worst'])}, "
            f"best {pct(a['best'])}, {a['share_positive']:.0%} positive — "
            f"registry verdict {(r.get('registry') or {}).get('verdict')}"))
    today = analysis.get("observed_today") or []
    thin = [e for e in today
            if not any("abnormal" in b and b["abnormal"]["sufficient"]
                       for b in e["base_rates"])]
    if today:
        out.append(line(f"{len(today)} event type(s) observed; {len(thin)} "
                        f"have insufficient history for a base rate "
                        f"(no frozen outcome ledger)"))
    if not out:
        out.append(line("No base rate is available."))
    return _section("base_rates", out, status,
                    "What followed past events of a type — a description, "
                    "not a forecast. Computed only from reads already spent.")


def universe_section(daily: dict, status: dict) -> dict:
    if not status["usable"]:
        return _dark("universe", status)
    u = daily.get("universe") or {}
    out = [line(f"{u.get('eligible')} names pass the declared screen for "
                f"session {daily.get('asof_session')} "
                f"(+{len(u.get('entered') or [])} entered, "
                f"−{len(u.get('exited') or [])} left)")]
    for k, b in sorted((daily.get("benchmarks") or {}).items()):
        out.append(line(f"{k} {b.get('close')} · 1d {pct(b.get('ret_1d'), 2)}"
                        f" · ytd {pct(b.get('ret_ytd'))}"))
    out.append(line("The eligible list is not a buy list. Capital deployed: "
                    f"${daily.get('capital_deployed', 0)}."))
    return _section("universe", out, status)


def attention(statuses: dict, analysis: dict, analysis_status: dict,
              macro: dict = None) -> list:
    """🔑 Pinned for every stance, uncapped. See the module header."""
    out = []
    for name, st in statuses.items():
        if not st["usable"]:
            out.append(line(f"⚫ {name} is DARK — {st['why']}"))
    # A macro file can be perfectly fresh and still carry nothing: on the
    # first live run it held headlines and ten series that FRED refused, and
    # "macro: fresh" was all the attention block had to say about it.
    if macro and (statuses.get("macro") or {}).get("usable"):
        series = macro.get("series") or []
        dead = [s for s in series if s.get("status") != "fresh"]
        if series and len(dead) == len(series):
            out.append(line(f"⚫ FRED is DARK — 0 of {len(series)} macro "
                            f"series could be fetched: "
                            f"{dead[0].get('reason')}"))
        news = macro.get("news") or {}
        if news.get("status") != "ok":
            out.append(line(f"⚫ headlines were not collected — "
                            f"{news.get('reason')}"))
    if analysis_status["usable"]:
        led = analysis.get("ledger") or {}
        if led.get("ok") is False:
            out.append(line(f"⚠️ thesis ledger unusable — {led.get('why')}"))
        for t in analysis.get("falsifiers") or []:
            if t["drifting"]:
                out.append(line(
                    f"🔴 {t['ticker']} · {t['thesis_id']} is DRIFTING — a "
                    f"falsifier fired and nothing was recorded. Override it "
                    f"in writing or close the thesis."))
            if t["review_due"]:
                out.append(line(f"⏰ {t['ticker']} · {t['thesis_id']} — "
                                f"review was due {t['review_date']}"))
    return out


def facts(inputs: dict) -> dict:
    """Every section, complete. No stance is consulted here or below.

    `inputs` = {"records", "meta", "macro", "brief", "analysis", "daily",
                "statuses": {name: artifact_status}}
    """
    st = inputs["statuses"]
    an = inputs.get("analysis") or {}
    sections = {
        "theses": theses_section(an, st["analysis"]),
        "exposure": exposure_section(an, st["analysis"]),
        "risk": risk_section(an, st["analysis"]),
        "language": language_section(inputs.get("brief") or {}, st["librarian"]),
        "macro": macro_section(inputs.get("macro") or {}, st["macro"]),
        "filings": filings_section(inputs.get("records") or [],
                                   inputs.get("meta") or {},
                                   st["observations"]),
        "insiders": insiders_section(
            inputs.get("records") or [], inputs.get("meta") or {},
            an.get("base_rates") if st["analysis"]["usable"] else [],
            st["observations"], inputs.get("resolved")),
        "base_rates": base_rates_section(an, st["analysis"]),
        "universe": universe_section(inputs.get("daily") or {},
                                     st["daily snapshot"]),
    }
    return {"attention": attention(st, an, st["analysis"],
                                   inputs.get("macro")),
            "sections": sections}


# ── compose: order and cap. Nothing else. ────────────────────────────────────

def compose(fact_doc: dict, stance: str) -> dict:
    """Pick an order and a cap per section. Receives finished lines only.

    🔑 A dark section is never capped away and is never reordered to the
    bottom by a cap of zero: it keeps its single line.
    """
    s = stance if stance in ORDER else "balanced"
    out = []
    for sid in ORDER[s]:
        sec = fact_doc["sections"][sid]
        cap = CAPS[s][sid]
        shown = sec["lines"] if sec["dark"] else sec["lines"][:cap]
        out.append({"id": sid, "title": sec["title"], "dark": sec["dark"],
                    "note": sec["note"], "source": sec["source"],
                    "lines": shown, "shown": len(shown),
                    "total": len(sec["lines"])})
    return {"stance": s, "order": list(ORDER[s]),
            "attention": list(fact_doc["attention"]), "sections": out}


def render_md(doc: dict, compact: bool = False, full_url: str = "") -> str:
    """The file form of a composed brief, or -- `compact` -- the Discord one.

    Compact shows fewer lines per section and no links, and says so on each
    section it shortened. It is a shorter VIEW of the same composed brief:
    it selects from `doc["sections"]` and computes nothing.
    """
    per = 7 if compact else 10 ** 6
    st = doc["stance_info"]
    out = [f"# Daily brief — {doc['day']}",
           f"_stance **{doc['stance']}** ({st['source']}) · generated "
           f"{doc['generated_at']} · no recommendation is made anywhere in "
           f"this brief_"]
    if doc["attention"]:
        out.append("")
        out.append("**Needs your attention**")
        out += [f"> {a['text']}" for a in doc["attention"]]
    for sec in doc["sections"]:
        out.append("")
        lines = sec["lines"] if sec["dark"] else sec["lines"][:per]
        out.append(f"**{sec['title']}**"
                   + (f"  _({len(lines)} of {sec['total']})_"
                      if sec["total"] > len(lines) else ""))
        for ln in lines:
            t = ln["text"]
            out.append((t if t.startswith("    ") else f"· {t}")
                       + (f" — <{ln['url']}>"
                          if ln.get("url") and not compact else ""))
        if sec["note"] and not compact:
            out.append(f"_{sec['note']}_")
    msgs = doc.get("operator_messages") or {}
    if msgs.get("status") == "ok" and msgs.get("open"):
        out.append("")
        out.append(f"**Your messages** — {len(msgs['open'])} open")
        for m in msgs["open"]:
            out.append(f"· #{m['number']} [{m['stance'] or 'no stance'}] "
                       f"{m['title'][:90]} — <{m['url']}>")
    if compact and full_url:
        out.append("")
        out.append(f"Full brief, with every line and link: <{full_url}>")
    return "\n".join(out)


# ── the operator channel ─────────────────────────────────────────────────────

def defang(text: str) -> str:
    """Neutralise mentions in text that came from outside this repository.

    Issue titles and headlines are untrusted. An `@everyone` in one would
    otherwise ping a channel when the brief is posted.
    """
    return str(text or "").replace("@", "@​")


def parse_issue(issue: dict) -> "dict | None":
    """An operator message, or None if this issue is not one.

    🔑 ONLY THE REPOSITORY OWNER IS THE OPERATOR. The repo is public, so
    anyone can open an issue and anyone can type a stance into it. An issue
    counts only when GitHub itself says its author is the OWNER -- that
    field is set by GitHub, not by the body, so it cannot be typed.
    """
    if not isinstance(issue, dict) or "pull_request" in issue:
        return None
    if issue.get("author_association") != "OWNER":
        return None
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    labels = {str((l or {}).get("name") or "").lower()
              for l in issue.get("labels") or []}
    if not ("chief-of-staff" in labels or FROM_DASHBOARD in body
            or TITLE_RE.search(title)):
        return None
    m = STANCE_RE.search(body) or TITLE_RE.search(title)
    text = body.split("---", 1)[1] if "---" in body else body
    return {"number": issue.get("number"), "title": defang(title),
            "stance": m.group(1) if m else None,
            "created_at": issue.get("created_at"),
            "url": issue.get("html_url"),
            "excerpt": defang(" ".join(text.split())[:280])}


def stance_from_messages(messages: list, file_stance: dict) -> dict:
    """The newest owner message that states a stance wins; else the file.

    The brief reports which source it used, so a stance changed from the
    dashboard is visible as such rather than silently replacing the file.
    """
    stated = [m for m in messages or [] if m.get("stance")]
    file_at = str(file_stance.get("set_at") or "")
    if stated:
        newest = max(stated, key=lambda m: str(m.get("created_at") or ""))
        # A message older than the file's own date does not override it.
        if str(newest.get("created_at") or "")[:10] >= file_at[:10]:
            return {"stance": newest["stance"], "valid": True,
                    "set_at": newest.get("created_at"),
                    "source": f"your message #{newest['number']}"}
    return dict(file_stance)


def ack_comment(message: dict, day: str, stance: str, brief_url: str) -> str:
    """The reply posted on an operator message. Deterministic; it states
    what the agent did with the message and what it will not do."""
    took = (f"Your stance **{message['stance']}** is in effect for the brief"
            if message.get("stance") and message["stance"] == stance
            else f"The brief is using stance **{stance}**")
    return "\n".join([
        ACK_MARKER,
        f"**Chief of Staff — received.** {took} of {day}.",
        "",
        f"Your message is listed under *Your messages* in that brief: "
        f"{brief_url}",
        "",
        "What I do with a message: record your stance, and keep the thread "
        "in the brief until you close it. What I do not do: place an order, "
        "give a recommendation, or write to the thesis ledger for you — an "
        "entry there has to be your own act.",
        "",
        "To put a thesis on the record:",
        "```",
        "python thesis_ledger.py open --id <id> --ticker <TICKER> \\",
        "  --claim \"...\" --falsifier \"what would make this wrong\" \\",
        "  --review YYYY-MM-DD",
        "```",
        "Close this issue when you are done with it and it leaves the brief.",
    ])
