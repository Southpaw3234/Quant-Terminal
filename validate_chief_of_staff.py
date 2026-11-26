#!/usr/bin/env python3
"""Pure-logic validation for CHIEF OF STAFF and the notifier. No network.

The class of wrongness this file exists to prevent: **a brief that tells an
aggressive operator something different from what it tells a conservative
one.** Stance is supposed to change the order of the page and its length.
If it could change a number, the brief would be an argument tailored to its
reader -- the "recommendation that looks authoritative" the build brief
names as the failure mode with the highest cost.

Three more, each cheap to ship by accident:

  * a stale or missing input silently dropping out of the page
  * a stranger setting the operator's stance by opening an issue
  * a delivery failure turning a published brief into a failed run
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import chief
from qt import notify
import chief_of_staff as agent

PASS, FAIL = [], []
VERDICT_WORDS = ("buy_", "sell", "hold", "rating", "target", "confidence",
                 "recommendation", "score", "forecast", "signal", "conviction")


def check(name: str, ok: bool, why: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  — {why}" if why else ""))


def _imports(mod) -> set:
    out = set()
    for n in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            out.update(f"{n.module}.{a.name}" for a in n.names)
    return out


def _identifiers(mod) -> set:
    out = set()
    for n in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            out.add(n.name.lower())
        elif isinstance(n, ast.Name):
            out.add(n.id.lower())
        elif isinstance(n, ast.arg):
            out.add(n.arg.lower())
    return out


def ok_status(name="x"):
    return {"name": name, "status": "fresh", "age_days": 0.1,
            "stamp": "t", "usable": True, "why": "ok"}


def dark_status(name="x"):
    return {"name": name, "status": "missing", "age_days": None,
            "stamp": None, "usable": False,
            "why": f"{name} was not produced or could not be read"}


def _filing(tk, form, acc, items=(), direction=None, **extra):
    r = {"id": f"edgar:{acc}", "kind": "filing", "form": form,
         "root_form": form, "ticker": tk, "accession": acc,
         "items": list(items), "url": f"https://x/{acc}",
         "direction": direction}
    r.update(extra)
    return r


def inputs(dark=()):
    names = ("observations", "macro", "librarian", "analysis",
             "daily snapshot")
    st = {n: (dark_status(n) if n in dark else ok_status(n)) for n in names}
    records = ([_filing(f"T{i:02d}", "8-K", f"a{i}", ["2.02", "9.01"])
                for i in range(40)]
               + [_filing("BUYR", "4", "f1", direction="purchase",
                          purchase_value=52000.0, insiders=["Doe J"],
                          first_purchase=[{"insider": "Doe J",
                                           "history_from": "2023-11-20"}]),
                  _filing("SELR", "4", "f2", direction="sale"),
                  _filing("UNKN", "4", "f3"),
                  {"id": "derived:c1", "kind": "derived_event",
                   "event_type": "form4_cluster_buy", "ticker": "CLST",
                   "n_insiders": 3, "total_value": 412000.0}])
    rate = {"event_type": "form4_cluster_buy", "horizon_bars": 63,
            "control": "SPY", "registry": {"verdict": "NOT MET"},
            "abnormal": {"n": 406, "sufficient": True, "median": -0.0449,
                         "q25": -0.2053, "q75": 0.0984, "worst": -0.772,
                         "best": 2.1, "share_positive": 0.40}}
    thesis = {"thesis_id": "grpn-1", "ticker": "GRPN", "claim": "below cash",
              "review_date": "2026-11-01", "review_due": True,
              "drifting": True,
              "falsifiers": [{"falsifier": "customers decline",
                              "ledger_status": "fired_unaddressed"},
                             {"falsifier": "cash < price",
                              "ledger_status": "not_recorded_as_fired"}],
              "evidence_arrived": {"observations": 2,
                                   "days_searched": ["2026-11-13"]}}
    return {
        "statuses": st, "records": records,
        "meta": {"filing_day": "2026-11-13",
                 "totals": {"8-K": {"filings": 313, "in_universe": 40},
                            "4": {"filings": 675, "in_universe": 3}},
                 "direction": {"note": "Finnhub lags EDGAR"}},
        "macro": {"series": [
            {"status": "fresh", "title": "10y", "value": 5.1, "units": "%",
             "as_of": "2026-11-12", "change": 0.12,
             "previous_as_of": "2026-11-10", "url": "https://fred/x"},
            {"status": "stale", "title": "CPI", "reason": "90 days old",
             "url": "https://fred/y"}],
            "news": {"status": "ok", "by_thesis": {},
                     "market": {"items": [
                         {"headline": f"headline {i}", "url": f"https://n/{i}"}
                         for i in range(30)]}}},
        "brief": {"pairs_compared": 55, "issuers_read": 60,
                  "issuers_eligible": 73, "names_truncated": True,
                  "max_names": 60, "selection_rule": "thesis names first",
                  "summary": {"removed": 2, "added": 1, "retained": 9,
                              "indeterminate": 1},
                  "refused_lookahead": [],
                  "claims": [
                      {"notable": True, "transition": "added", "ticker": "AAA",
                       "form": "10-Q", "phrase": "going concern",
                       "baseline": {"filed": "2026-08-14"},
                       "current": {"filed": "2026-11-13"}},
                      {"notable": True, "transition": "removed",
                       "ticker": "ZZZ", "form": "10-Q", "phrase": "covenant",
                       "baseline": {"filed": "2026-08-14"},
                       "current": {"filed": "2026-11-13"}}]},
        "analysis": {"ledger": {"ok": True}, "falsifiers": [thesis],
                     "base_rates": [rate],
                     "observed_today": [
                         {"event_type": "8k_item_2.02", "count": 27,
                          "base_rates": [{"sufficient": False}]}],
                     "exposure": {"status": "no_positions",
                                  "reason": "no positions are declared",
                                  "stance": "balanced",
                                  "limits": {"single_name": 0.2},
                                  "theses_without_a_position": ["GRPN"]}},
        "daily": {"asof_session": "2026-11-13", "capital_deployed": 0,
                  "universe": {"eligible": 1067, "entered": ["A"],
                               "exited": ["B", "C"]},
                  "benchmarks": {"SPY": {"close": 754.37, "ret_1d": -0.0036,
                                         "ret_ytd": 0.1128}}},
    }


def _all_lines(sections) -> set:
    return {(s["id"] if isinstance(s, dict) and "id" in s else "", ln["text"])
            for s in sections for ln in s["lines"]}


# ── 1. 🔑 stance orders and caps; it never changes a fact ────────────────────

def test_stance_cannot_change_a_fact():
    print("\nstance orders and caps; it cannot change a fact")
    f = chief.facts(inputs())
    fact_lines = {(sid, ln["text"]) for sid, sec in f["sections"].items()
                  for ln in sec["lines"]}
    composed = {s: chief.compose(f, s) for s in chief.STANCES}

    for s in chief.STANCES:
        shown = _all_lines(composed[s]["sections"])
        check(f"🔑-every-{s}-line-is-a-fact-line", shown <= fact_lines,
              f"{len(shown)} line(s) shown, all drawn from {len(fact_lines)} "
              f"fact lines")
        check(f"{s}-shows-every-section",
              sorted(x["id"] for x in composed[s]["sections"])
              == sorted(f["sections"]))
        check(f"{s}-reports-shown-of-total",
              all(x["shown"] == len(x["lines"])
                  and x["total"] == len(f["sections"][x["id"]]["lines"])
                  for x in composed[s]["sections"]))

    # The structural form of the guarantee: no stance reaches the builders.
    for fn in (chief.facts, chief.theses_section, chief.exposure_section,
               chief.language_section, chief.macro_section,
               chief.filings_section, chief.insiders_section,
               chief.base_rates_section, chief.universe_section,
               chief.attention):
        check(f"{fn.__name__}-takes-no-stance",
              "stance" not in inspect.signature(fn).parameters,
              "the numbers are finished before a stance is consulted")
    check("compose-takes-finished-facts-only",
          list(inspect.signature(chief.compose).parameters)
          == ["fact_doc", "stance"],
          "it has no access to the inputs, so it cannot recompute anything")

    nums = lambda doc: sorted(re.findall(r"-?\d+(?:\.\d+)?%?", json.dumps(
        [ln["text"] for sec in doc["sections"] for ln in sec["lines"]])))
    full = sorted(re.findall(r"-?\d+(?:\.\d+)?%?", json.dumps(
        [ln["text"] for sec in f["sections"].values() for ln in sec["lines"]])))
    for s in chief.STANCES:
        left = list(full)
        ok = True
        for n in nums(composed[s]):
            if n in left:
                left.remove(n)
            else:
                ok = False
        check(f"every-number-{s}-shows-appears-in-the-facts", ok)

    orders = {s: tuple(x["id"] for x in composed[s]["sections"])
              for s in chief.STANCES}
    check("the-three-stances-order-the-page-differently",
          len(set(orders.values())) == 3)
    check("conservative-leads-with-theses-and-exposure",
          orders["conservative"][:2] == ("theses", "exposure"))
    check("aggressive-leads-with-insider-activity",
          orders["aggressive"][0] == "insiders")
    check("each-order-is-a-permutation-of-the-same-sections",
          all(sorted(o) == sorted(chief.ORDER["balanced"])
              for o in chief.ORDER.values()))
    check("no-section-can-be-capped-to-nothing",
          all(c > 0 for caps in chief.CAPS.values() for c in caps.values()))
    cap_c = len(next(x for x in composed["conservative"]["sections"]
                     if x["id"] == "filings")["lines"])
    cap_a = len(next(x for x in composed["aggressive"]["sections"]
                     if x["id"] == "filings")["lines"])
    check("stance-changes-how-much-is-shown", cap_a > cap_c,
          f"filings: {cap_c} lines conservative, {cap_a} aggressive")
    check("unknown-stance-is-balanced",
          chief.compose(f, "yolo")["stance"] == "balanced")

    check("attention-is-identical-under-every-stance",
          len({json.dumps(composed[s]["attention"], sort_keys=True)
               for s in chief.STANCES}) == 1)
    att = " ".join(a["text"] for a in composed["balanced"]["attention"])
    check("🔑-a-drifting-thesis-is-pinned-to-the-top", "DRIFTING" in att,
          "record-keeping is not an opinion a risk appetite should reorder")
    check("drifting-AND-overdue-reports-both",
          "DRIFTING" in att and "review was due 2026-11-01" in att,
          "two obligations, not one — the first version reported only the "
          "drift and the overdue review vanished behind it")
    th_lines = [ln["text"] for ln in f["sections"]["theses"]["lines"]]
    check("a-thesis-line-carries-both-marks",
          th_lines[0].startswith("🔴 DRIFTING ⏰ REVIEW DUE GRPN"))
    check("no-thesis-line-starts-with-a-stray-bullet",
          not any(t.startswith("· ") for t in th_lines))


# ── 2. 🔑 a dark input is a section that says so ─────────────────────────────

def test_dark_inputs():
    print("\na dark input is a section that says so")
    f = chief.facts(inputs(dark=("librarian", "macro")))
    for sid in ("language", "macro"):
        sec = f["sections"][sid]
        check(f"{sid}-is-dark-with-one-line",
              sec["dark"] and len(sec["lines"]) == 1
              and "DARK" in sec["lines"][0]["text"])
        check(f"{sid}-shows-nothing-stale",
              "going concern" not in json.dumps(sec)
              and "headline 3" not in json.dumps(sec))
    att = [a["text"] for a in f["attention"]]
    check("each-dark-input-is-in-the-attention-block",
          sum("DARK" in a for a in att) == 2, f"{att}")
    for s in chief.STANCES:
        c = chief.compose(f, s)
        check(f"dark-sections-survive-the-{s}-caps",
              all(any(x["id"] == sid and x["dark"] and x["lines"]
                      for x in c["sections"]) for sid in ("language", "macro")))

    all_dark = chief.facts(inputs(dark=("observations", "macro", "librarian",
                                        "analysis", "daily snapshot")))
    check("everything-dark-still-renders-every-section",
          all(sec["dark"] for sec in all_dark["sections"].values())
          and len(all_dark["attention"]) == 5,
          "an outage produces a page that says OUTAGE, not an empty page")

    # 🔑 A fresh macro FILE with nothing in it. This was the first live run:
    # FRED refused all ten series and the attention block said nothing.
    hollow = inputs()
    hollow["macro"] = {"series": [
        {"status": "missing", "title": f"s{i}",
         "reason": "fred api answered 400 — FRED says: api_key is not "
                   "registered"} for i in range(10)],
        "news": {"status": "not_collected", "reason": "no key"}}
    ha = [a["text"] for a in chief.facts(hollow)["attention"]]
    check("🔑-an-all-dark-FRED-reaches-the-attention-block",
          any("FRED is DARK — 0 of 10" in a and "not registered" in a
              for a in ha),
          "'macro: fresh' was true of the file and false of its contents")
    check("uncollected-headlines-reach-the-attention-block",
          any("headlines were not collected" in a for a in ha))
    partial = inputs()
    check("one-stale-series-is-not-an-alarm",
          not any("FRED is DARK" in a["text"]
                  for a in chief.facts(partial)["attention"]),
          "a single stale series is normal; all of them is an outage")

    f2 = chief.facts(inputs())
    macro = [ln["text"] for ln in f2["sections"]["macro"]["lines"]]
    check("a-stale-series-is-shown-as-stale-not-as-a-number",
          any("CPI: stale" in t for t in macro))
    ins = " ".join(ln["text"] for ln in f2["sections"]["insiders"]["lines"])
    check("undirected-form4s-are-NOT-called-purchases",
          "1 not observed" in ins and "NOT purchases" in ins)
    check("a-purchase-is-named-as-one-with-its-first-purchase-history",
          "BUYR — open-market purchase $52,000" in ins
          and "since 2023-11-20" in ins)
    check("the-cluster-base-rate-travels-with-its-verdict",
          "n=406" in f2["sections"]["insiders"]["note"]
          and "NOT MET" in f2["sections"]["insiders"]["note"])
    lang = [ln["text"] for ln in f2["sections"]["language"]["lines"]]
    rem = next(i for i, t in enumerate(lang) if "REMOVED" in t)
    add = next(i for i, t in enumerate(lang) if "added AAA" in t)
    check("removed-language-is-listed-before-added", rem < add)
    check("a-capped-librarian-run-says-it-was-capped",
          any("capped at 60" in t for t in lang))
    ex = " ".join(ln["text"] for ln in f2["sections"]["exposure"]["lines"])
    check("no-positions-reads-as-not-measured", "Not measured" in ex)

    refused = inputs()
    refused["brief"] = dict(refused["brief"], claims=[],
                            refused_lookahead=[{}] * 30)
    lr = " ".join(ln["text"] for ln in
                  chief.facts(refused)["sections"]["language"]["lines"])
    check("all-refused-is-not-reported-as-nothing-changed",
          "Nothing was testable" in lr and "not 'nothing changed'" in lr)


# ── 3. 🔑 only the repository owner is the operator ──────────────────────────

def _issue(assoc="OWNER", title="[aggressive] look at GRPN", number=7,
           body=None, created="2026-11-14T12:00:00Z", labels=(), pr=False):
    body = body if body is not None else (
        "**Stance:** `aggressive`\n**Sent from:** the Quant Terminal "
        "dashboard\n\n---\n\nlook at GRPN @everyone")
    out = {"number": number, "title": title, "body": body,
           "author_association": assoc, "created_at": created,
           "html_url": f"https://github.com/x/y/issues/{number}",
           "labels": [{"name": l} for l in labels]}
    if pr:
        out["pull_request"] = {}
    return out


def test_operator_channel():
    print("\nonly the repository owner is the operator")
    m = chief.parse_issue(_issue())
    check("owner-message-is-read", m and m["stance"] == "aggressive"
          and m["number"] == 7)
    for assoc in ("NONE", "CONTRIBUTOR", "MEMBER", "COLLABORATOR",
                  "FIRST_TIME_CONTRIBUTOR"):
        check(f"🔑-{assoc}-cannot-set-the-stance",
              chief.parse_issue(_issue(assoc=assoc)) is None,
              "the repo is public; anyone can type a stance into an issue")
    check("a-pull-request-is-not-a-message",
          chief.parse_issue(_issue(pr=True)) is None)
    check("an-unrelated-owner-issue-is-not-a-message",
          chief.parse_issue(_issue(title="fix CI", body="nothing")) is None)
    check("the-label-alone-marks-a-message",
          chief.parse_issue(_issue(title="hi", body="hello",
                                   labels=("chief-of-staff",)))["stance"] is None)
    check("stance-is-an-enumerated-word-only",
          chief.parse_issue(_issue(
              title="x", body="**Stance:** `sell everything`\n"
                               "the Quant Terminal dashboard"))["stance"] is None,
          "free text in the stance slot is not a stance")
    check("mentions-are-defanged", "@everyone" not in m["excerpt"]
          and "@​everyone" in m["excerpt"])
    check("garbage-is-not-a-message", chief.parse_issue(None) is None
          and chief.parse_issue("x") is None)

    file_st = {"stance": "balanced", "valid": True, "set_at": "2026-11-14",
               "source": "data/operator/stance.json"}
    older = chief.parse_issue(_issue(number=1, created="2026-11-14T08:00:00Z",
                                     title="[conservative] a",
                                     body="**Stance:** `conservative`\n"
                                          "the Quant Terminal dashboard"))
    got = chief.stance_from_messages([older, m], file_st)
    check("the-newest-owner-message-wins",
          got["stance"] == "aggressive" and "#7" in got["source"])
    check("no-message-means-the-file",
          chief.stance_from_messages([], file_st) == file_st)
    stale = chief.parse_issue(_issue(created="2026-10-01T00:00:00Z"))
    check("a-message-older-than-the-file-does-not-override-it",
          chief.stance_from_messages([stale], file_st)["stance"] == "balanced")
    nostance = chief.parse_issue(_issue(title="hi", body="x",
                                        labels=("chief-of-staff",)))
    check("a-message-with-no-stance-leaves-the-file-in-force",
          chief.stance_from_messages([nostance], file_st) == file_st)

    ack = chief.ack_comment(m, "2026-11-13", "aggressive", "https://b")
    check("the-reply-carries-its-marker", chief.ACK_MARKER in ack,
          "so a message is answered once, not on every run")
    check("the-reply-says-what-it-will-not-do",
          "place an order" in ack and "thesis ledger" in ack)
    check("the-reply-confirms-the-stance-in-effect",
          "**aggressive** is in effect" in ack)
    src = inspect.getsource(agent.acknowledge)
    check("a-message-is-acknowledged-once", "ACK_MARKER" in src)
    check("no-credentials-is-not_read-not-zero-messages",
          "not_read" in inspect.getsource(agent.operator_messages),
          "'the channel was not read' and 'you sent nothing' are different")


# ── 4. rendering, delivery, capabilities ─────────────────────────────────────

def _doc(stance="balanced"):
    f = chief.facts(inputs())
    c = chief.compose(f, stance)
    return {"day": "2026-11-13", "generated_at": "t", "stance": c["stance"],
            "stance_info": {"source": "data/operator/stance.json"},
            "attention": c["attention"], "sections": c["sections"],
            "operator_messages": {"status": "ok", "open": [
                chief.parse_issue(_issue())]}}


def test_render_and_deliver():
    print("\nrendering and delivery")
    d = _doc()
    full = chief.render_md(d)
    comp = chief.render_md(d, compact=True, full_url="https://full")
    check("the-brief-says-it-makes-no-recommendation",
          "no recommendation" in full.lower())
    check("compact-is-shorter-and-links-to-the-full-brief",
          len(comp) < len(full) and "https://full" in comp)
    check("compact-says-how-much-it-cut", " of " in comp)
    full_lines = set(full.split("\n"))
    stripped = lambda l: re.sub(r" — <https?://[^>]+>$", "", l)
    full_stripped = {stripped(l) for l in full_lines}
    check("🔑-every-compact-line-is-a-line-of-the-full-brief",
          all(stripped(l) in full_stripped or l.startswith("**")
              or l.startswith("Full brief") or not l.strip()
              for l in comp.split("\n")),
          "compact is a shorter view; it computes nothing")
    check("operator-messages-are-listed", "#7" in full)

    long = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
    parts = notify.chunk(long)
    check("chunks-fit-the-limit", all(len(p) <= notify.DISCORD_LIMIT for p in parts)
          and len(parts) > 1)
    check("🔑-chunking-loses-nothing", "\n".join(parts) == long,
          "the old snippet cut a digest at 1,900 characters and threw the "
          "rest away")
    check("an-over-long-single-line-is-split-not-dropped",
          "".join(notify.chunk("y" * 5000)) == "y" * 5000)
    check("empty-yields-no-chunks", notify.chunk("") == [] and notify.chunk(None) == [])

    r = notify.post("", "hello")
    check("no-webhook-is-reported-not-raised",
          r["ok"] is False and "no webhook" in r["reason"])
    check("nothing-to-send-is-reported",
          notify.post("https://x", "")["ok"] is False)
    lits = [n.value for n in ast.walk(ast.parse(inspect.getsource(notify)))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    check("requests-carry-a-user-agent", "User-Agent" in lits,
          "the 403 that killed the first scheduled run was Python's default "
          "User-Agent being refused")
    check("mentions-are-disabled-in-the-payload", "allowed_mentions" in lits)
    try:
        bad = notify.post("http://127.0.0.1:9/nope", "hello", pause=0)
        raised = False
    except Exception:                                 # noqa: BLE001
        raised, bad = True, {}
    check("🔑-a-dead-webhook-returns-a-result-and-never-raises",
          not raised and bad.get("ok") is False,
          "by the time a message is posted the artifact is published; a "
          "webhook outage must not turn the run red")


def test_capabilities_absent():
    print("\nCHIEF OF STAFF relays; it cannot decide, trade or write the ledger")
    imported = _imports(agent) | _imports(chief)
    for m in ("qt.referee", "qt.wrc", "qt.calendar_time", "qt.prices",
              "yfinance", "alpaca", "alpaca_trade_api", "qt.thesis"):
        check(f"does-not-import-{m}", m not in imported,
              "an entry in the thesis ledger is the operator's own act"
              if m == "qt.thesis" else "")
    roots = {m.split(".")[0] for m in _imports(chief)}
    check("pure-module-is-stdlib-only", roots <= {"__future__", "re"},
          f"{sorted(roots)}")
    ids = _identifiers(chief) | _identifiers(agent)
    for w in VERDICT_WORDS:
        hit = sorted(i for i in ids if w in i)
        check(f"no-identifier-contains-{w.strip('_')}", not hit,
              f"found {hit}" if hit else "")
    src = inspect.getsource(agent)
    check("the-ledger-path-is-never-opened-for-writing",
          "ledger.jsonl" not in src and "th.append" not in src)
    check("delivery-and-replies-are-opt-in",
          'QT_CHIEF_POST' in src and 'QT_CHIEF_REPLY' in src)
    check("latest-json-is-written-for-the-dashboard",
          '"latest.json"' in inspect.getsource(agent.main))
    check("every-input-has-a-maximum-age",
          set(agent.MAX_AGE) == {"observations", "macro", "librarian",
                                 "analysis", "daily snapshot"}
          and all(v > 0 for v in agent.MAX_AGE.values()))



def test_brief_reads_what_resolved():
    print("\nthe brief shows what resolved in the run, not only the newest day")
    inp = inputs()
    newest = [_filing(f"N{i}", "4", f"n{i}") for i in range(5)]  # all undirected
    inp["records"] = newest
    inp["meta"] = dict(inp["meta"], filing_day="2026-11-25")
    resolved = [dict(_filing("PAL", "4", "p1", direction="purchase",
                             purchase_value=60000.0, insiders=["A"]),
                     day="2026-11-23"),
                dict(_filing("SELL", "4", "s1", direction="sale"), day="2026-11-24"),
                {"id": "derived:c", "kind": "derived_event", "ticker": "PAL",
                 "event_type": "form4_cluster_buy", "n_insiders": 3,
                 "total_value": 434000.0, "day": "2026-11-23"}]
    inp["resolved"] = resolved
    txt = [l["text"] for l in chief.facts(inp)["sections"]["insiders"]["lines"]]
    joined = " | ".join(txt)
    check("🔑-a-purchase-resolved-for-an-older-day-reaches-the-brief",
          any("PAL — open-market purchase $60,000" in t and "(filed 11-23)" in t
              for t in txt),
          "nine briefs in a row showed no purchase while 185 had resolved")
    check("a-cluster-resolved-for-an-older-day-reaches-the-brief",
          any("CLUSTER PAL" in t and "(filed 11-23)" in t for t in txt))
    check("the-resolved-line-counts-and-dates-them",
          any(t.startswith("Resolved since the last brief: 2 Form 4") and
              "11-23, 11-24" in t and "1 purchase" in t and "1 sale" in t
              for t in txt))
    check("the-newest-day-is-still-shown-as-pending",
          "5 Form 4 filing(s) on 2026-11-25" in joined
          and "no observed direction YET" in joined)
    check("an-undirected-newest-day-is-still-NOT-called-purchases",
          "NOT purchases" in joined)
    inp["resolved"] = []
    quiet = " | ".join(l["text"] for l in
                       chief.facts(inp)["sections"]["insiders"]["lines"])
    check("nothing-resolved-says-so", "Resolved since the last brief: 0" in quiet
          and "CLUSTER" not in quiet)
    src = inspect.getsource(agent.load_inputs)
    check("the-agent-passes-resolved-records",
          "resolved_in_run" in src and "RECENT_FILES" in src)


def main():
    print("=" * 70)
    print("CHIEF OF STAFF — pure logic (no network)")
    print("=" * 70)
    test_stance_cannot_change_a_fact()
    test_dark_inputs()
    test_brief_reads_what_resolved()
    test_operator_channel()
    test_render_and_deliver()
    test_capabilities_absent()
    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED  {f}")
        sys.exit(1)
    print("All checks passed. No network touched, nothing recommended.")


if __name__ == "__main__":
    main()
