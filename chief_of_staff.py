#!/usr/bin/env python3
"""CHIEF OF STAFF -- module 5 of the collective agent. The daily brief.

See docs/COLLECTIVE_AGENT_BRIEF.md §3.4 and the header of qt/chief.py, which
states the design: facts are computed once with no stance in sight, and the
stance then picks an order and a cap. Nothing else.

    OBSERVER (filings, macro)  ┐
    LIBRARIAN (brief)          ├─>  chief/<day>.json + .md + latest.json
    ACTUARY (analysis)         │    -> Discord, the dashboard
    daily snapshot             ┘    -> a reply on each operator message

🔑 EVERY INPUT IS CHECKED FOR FRESHNESS BEFORE IT IS READ. A stale or
missing upstream becomes a dark section and a line in the attention block.
It is never skipped and never replaced by yesterday's content.

🔑 THE OPERATOR IS THE REPOSITORY OWNER, AS GITHUB REPORTS IT. Messages
arrive as issues on a public repository. Only an issue whose
`author_association` is OWNER is read as the operator's, and what is taken
from it is one enumerated word -- the stance -- plus a quoted excerpt. Its
text is never executed, interpreted as an instruction, or acted on.

This agent places no order, makes no recommendation and writes nothing to
the thesis ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import agentio as io
from qt import chief
from qt import notify

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time,
# qt.prices, and any broker SDK. validate_chief_of_staff.py asserts this.

OUT_DIR = os.environ.get("QT_CHIEF_DIR", "chief")
OBS_DIR = os.environ.get("QT_OBS_DIR", "observations")
MACRO_DIR = os.environ.get("QT_MACRO_DIR", "macro")
BRIEF_DIR = os.environ.get("QT_LIB_DIR", "briefs")
ACT_DIR = os.environ.get("QT_ACT_DIR", "analysis")
DAILY = os.environ.get("QT_DAILY_JSON", "daily.json")
STANCE = os.environ.get("QT_STANCE_FILE", "data/operator/stance.json")
REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
HOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
POST = os.environ.get("QT_CHIEF_POST", "") == "1"
REPLY = os.environ.get("QT_CHIEF_REPLY", "") == "1"
PUBLISH_BRANCH = os.environ.get("QT_AGENT_BRANCH", "agent")
API = "https://api.github.com"

# How old each input may be before its section goes dark. Friday's filings
# read on Monday are three days old, so the EDGAR-derived inputs get four.
MAX_AGE = {"observations": 4.0, "macro": 2.0, "librarian": 4.0,
           "analysis": 2.0, "daily snapshot": 4.0}


def load_inputs(now: str) -> dict:
    """Read every upstream artifact WITH its freshness verdict."""
    day, path = io.latest_day_file(OBS_DIR, ".jsonl")
    meta = io.read_json(os.path.join(OBS_DIR, f"{day}.meta.json")) if day else None
    _, mpath = io.latest_day_file(MACRO_DIR, ".json")
    macro = io.read_json(mpath) if mpath else None
    _, bpath = io.latest_day_file(BRIEF_DIR, ".json")
    brief = io.read_json(bpath) if bpath else None
    _, apath = io.latest_day_file(ACT_DIR, ".json")
    analysis = io.read_json(apath) if apath else None
    daily = io.read_json(DAILY)

    def stamp(obj):
        return (obj or {}).get("generated_at") or ""

    statuses = {
        "observations": io.artifact_status(
            "OBSERVER filings", meta, stamp(meta), MAX_AGE["observations"], now),
        "macro": io.artifact_status(
            "OBSERVER macro", macro, stamp(macro), MAX_AGE["macro"], now),
        "librarian": io.artifact_status(
            "LIBRARIAN brief", brief, stamp(brief), MAX_AGE["librarian"], now),
        "analysis": io.artifact_status(
            "ACTUARY analysis", analysis, stamp(analysis),
            MAX_AGE["analysis"], now),
        "daily snapshot": io.artifact_status(
            "daily snapshot", daily, stamp(daily),
            MAX_AGE["daily snapshot"], now),
    }
    return {"day": day, "meta": meta or {},
            "records": io.read_jsonl(path)
            if day and statuses["observations"]["usable"] else [],
            "macro": macro, "brief": brief, "analysis": analysis,
            "daily": daily, "statuses": statuses}


# ── the operator channel (GitHub issues) ─────────────────────────────────────

def _gh(method: str, path: str, payload: dict = None) -> dict:
    """-> {"ok", "json", "reason"}. Never raises."""
    import requests
    try:
        r = requests.request(
            method, f"{API}{path}", timeout=30, json=payload,
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "research-agent/1.0"})
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "json": None, "reason": type(e).__name__}
    if r.status_code >= 300:
        return {"ok": False, "json": None,
                "reason": f"github answered {r.status_code}"}
    try:
        return {"ok": True, "json": r.json(), "reason": "ok"}
    except ValueError:
        return {"ok": False, "json": None, "reason": "github: not JSON"}


def operator_messages() -> dict:
    """Open issues written by the repository owner. See the module header."""
    if not (REPO and TOKEN):
        return {"status": "not_read", "open": [],
                "reason": "no GitHub credentials in this environment — the "
                          "operator channel was not read"}
    got = _gh("GET", f"/repos/{REPO}/issues?state=open&per_page=50"
                     f"&sort=created&direction=desc")
    if not got["ok"]:
        return {"status": "not_read", "open": [], "reason": got["reason"]}
    msgs = [m for m in (chief.parse_issue(i) for i in got["json"] or []) if m]
    return {"status": "ok", "open": msgs, "reason": "ok"}


def acknowledge(messages: list, day: str, stance: str) -> dict:
    """Reply once on each operator message that has no reply yet."""
    out = {"replied": 0, "already": 0, "failed": 0}
    url = (f"https://github.com/{REPO}/blob/{PUBLISH_BRANCH}/"
           f"{OUT_DIR}/{day}.md")
    for m in messages:
        got = _gh("GET", f"/repos/{REPO}/issues/{m['number']}/comments"
                         f"?per_page=100")
        if not got["ok"]:
            out["failed"] += 1
            continue
        if any(chief.ACK_MARKER in str((c or {}).get("body") or "")
               for c in got["json"] or []):
            out["already"] += 1
            continue
        sent = _gh("POST", f"/repos/{REPO}/issues/{m['number']}/comments",
                   {"body": chief.ack_comment(m, day, stance, url)})
        out["replied" if sent["ok"] else "failed"] += 1
    return out


def facts_digest(fact_doc: dict) -> str:
    return hashlib.sha256(json.dumps(fact_doc, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def build(now: str) -> dict:
    inputs = load_inputs(now)
    msgs = operator_messages()
    stance = chief.stance_from_messages(msgs["open"], io.load_stance(STANCE))
    fact_doc = chief.facts(inputs)
    composed = chief.compose(fact_doc, stance["stance"])
    day = inputs["day"] or io.today_utc()
    return {
        "generated_at": now,
        "day": day,
        "describes": (f"filings published {inputs['day']}" if inputs["day"]
                      else "no filing day — OBSERVER produced nothing"),
        "stance": composed["stance"],
        "stance_info": stance,
        "is_not_a_recommendation": (
            "This brief relays what was collected, structured and "
            "described. It contains no buy, sell or hold, and the stance "
            "changes only the order and length of sections."),
        "inputs": inputs["statuses"],
        "attention": composed["attention"],
        "order": composed["order"],
        "sections": composed["sections"],
        "operator_messages": msgs,
        # The complete, stance-free facts and the three layouts, so the
        # dashboard can re-order for a different stance without a new run —
        # and so anyone can check that a stance only reorders.
        "facts": fact_doc,
        "facts_sha256": facts_digest(fact_doc),
        "layouts": {s: {"order": list(chief.ORDER[s]), "caps": chief.CAPS[s]}
                    for s in chief.STANCES},
    }


def deliver() -> None:
    """Send the brief that is ALREADY WRITTEN, and answer the operator.

    🔑 A separate step, run after the publish. The Discord message and the
    issue reply both link to the brief on the published branch; sending
    them before the push would hand the operator a link to a page that does
    not exist yet -- or, if the push then failed, never will. A green run is
    not a delivered artifact, so delivery comes last and reports what it
    actually managed.
    """
    doc = io.read_json(os.path.join(OUT_DIR, "latest.json"))
    if not doc:
        print("::error::chief: nothing to deliver — no brief was written")
        sys.exit(7)
    full = (f"https://github.com/{REPO}/blob/{PUBLISH_BRANCH}/{OUT_DIR}/"
            f"{doc['day']}.md") if REPO else ""

    if REPLY and doc["operator_messages"]["status"] == "ok":
        a = acknowledge(doc["operator_messages"]["open"], doc["day"],
                        doc["stance"])
        print(f"[chief] operator replies: {a['replied']} sent, "
              f"{a['already']} already answered, {a['failed']} failed")
        if a["failed"]:
            print(f"::warning::chief: {a['failed']} operator message(s) "
                  f"could not be answered")
    else:
        print(f"[chief] operator replies: not attempted "
              f"({'replies are off' if not REPLY else doc['operator_messages']['reason']})")

    if not POST:
        print("[chief] discord: posting is off for this run (dry run)")
        return
    res = notify.post(HOOK, chief.render_md(doc, compact=True, full_url=full))
    print(f"[chief] discord: {'sent' if res['ok'] else 'NOT SENT'} "
          f"{res['sent']}/{res['of']} — {res['reason']}")
    if not res["ok"]:
        # Visible, and deliberately not fatal: the brief is published and on
        # the dashboard whether or not a webhook answered.
        print(f"::warning::chief: the brief is published but was not "
              f"delivered to Discord — {res['reason']}")


def main() -> None:
    if "--deliver" in sys.argv[1:]:
        deliver()
        return
    now = io.now_utc()
    doc = build(now)
    text = chief.render_md(doc)

    io.write_json(os.path.join(OUT_DIR, f"{doc['day']}.json"), doc)
    io.write_text(os.path.join(OUT_DIR, f"{doc['day']}.md"), text)
    io.write_json(os.path.join(OUT_DIR, "latest.json"), doc)

    dark = [n for n, s in doc["inputs"].items() if not s["usable"]]
    print(f"[chief] brief for {doc['day']} — stance {doc['stance']} "
          f"({doc['stance_info']['source']})")
    for n, s in doc["inputs"].items():
        print(f"  {n:<16} {s['status']:<8} "
              f"{'' if s['age_days'] is None else str(s['age_days']) + 'd'}")
    print(f"  attention lines  {len(doc['attention'])}")
    print(f"  operator channel {doc['operator_messages']['status']} — "
          f"{len(doc['operator_messages']['open'])} open")
    print(f"  wrote {OUT_DIR}/{doc['day']}.json, .md, latest.json "
          f"({len(text)} chars)")

    print("\n" + text[:2600])
    print("\n[chief] relayed only. No order, no recommendation, nothing "
          "written to the thesis ledger.")
    if dark:
        print(f"::warning::chief: {len(dark)} input(s) dark — "
              f"{', '.join(dark)}. The brief says so in its first block.")


if __name__ == "__main__":
    main()
