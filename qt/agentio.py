"""Shared plumbing for the four collective agents. No network.

Every agent needs the same five things: the watched universe, the open
theses, a fingerprint of what it read, a freshness verdict on an upstream
artifact, and a way to write its own. The first three modules each restated
them, slightly differently. They live here once.

🔑 THE FRESHNESS CONTRACT (docs/COLLECTIVE_AGENT_BRIEF.md §5): a feed that
stops must look stopped. `artifact_status` never returns a bare dict of
data -- it returns the data WITH a verdict, and an agent that consumes an
upstream artifact must carry that verdict into its own output. A missing
or stale input becomes a section that says so, never a section quietly
left out.
"""

from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os

DAY_FMT = "%Y-%m-%d"


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def sha256_normalised(path: str) -> str:
    """sha256 with CRLF folded to LF, so a Windows checkout and a Linux
    runner fingerprint the same bytes."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def read_json(path: str):
    """-> parsed JSON, or None if the file is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip("\n") + "\n")


def read_jsonl(path: str) -> list:
    """Every parseable line. A malformed line is skipped AND counted by the
    caller via `read_jsonl_counted` when that matters."""
    return read_jsonl_counted(path)[0]


def read_jsonl_counted(path: str) -> tuple:
    """-> (rows, n_bad). Missing file is ([], 0)."""
    rows, bad = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        return [], 0
    return rows, bad


def write_jsonl(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")


def day_files(directory: str, suffix: str) -> list:
    """[(day, path)] for `YYYY-MM-DD<suffix>` files, oldest first.

    Matches the DATE shape, not the glob, so `cik_cache.json` or a
    `latest.json` beside the day files is never mistaken for a day.
    """
    out = []
    for p in glob.glob(os.path.join(directory, f"*{suffix}")):
        name = os.path.basename(p)[: -len(suffix)] if suffix else \
            os.path.basename(p)
        try:
            dt.datetime.strptime(name, DAY_FMT)
        except ValueError:
            continue
        out.append((name, p))
    return sorted(out)


def latest_day_file(directory: str, suffix: str):
    """-> (day, path) of the newest day file, or (None, None)."""
    files = day_files(directory, suffix)
    return files[-1] if files else (None, None)


def age_days(day_or_iso: str, now: str = "") -> "float | None":
    """Age of a date or timestamp in days. None when it cannot be parsed —
    and an undated record is to be treated exactly like a stale one."""
    s = str(day_or_iso or "").strip()
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    ref = dt.datetime.fromisoformat((now or now_utc()).replace("Z", "+00:00"))
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=dt.timezone.utc)
    return (ref - t).total_seconds() / 86400.0


def artifact_status(name: str, obj, stamp: str, max_age_days: float,
                    now: str = "") -> dict:
    """The freshness verdict for one upstream artifact.

    -> {"name", "status": fresh|stale|missing|undated, "age_days", "stamp",
        "usable": bool, "why"}

    🔑 `usable` is False for anything but `fresh`. An agent shows a dark
    line for an unusable input; it does not fall back to the old numbers.
    """
    if obj is None:
        return {"name": name, "status": "missing", "age_days": None,
                "stamp": None, "usable": False,
                "why": f"{name} was not produced or could not be read"}
    age = age_days(stamp, now)
    if age is None:
        return {"name": name, "status": "undated", "age_days": None,
                "stamp": stamp or None, "usable": False,
                "why": f"{name} carries no readable timestamp, so its age "
                       f"cannot be established — treated as stale"}
    if age > max_age_days:
        return {"name": name, "status": "stale", "age_days": round(age, 2),
                "stamp": stamp, "usable": False,
                "why": f"{name} is {age:.1f} days old (limit "
                       f"{max_age_days:g}) — not shown"}
    return {"name": name, "status": "fresh", "age_days": round(age, 2),
            "stamp": stamp, "usable": True, "why": "ok"}


# -- the watched universe and the operator's theses --------------------------

def open_theses(ledger_path: str) -> dict:
    """-> {"ok": bool, "why": str, "theses": [state dicts], "entries": n}

    Reads the thesis ledger and REFUSES to use one whose chain does not
    verify: widening a watch list, or reporting falsifiers, from an altered
    ledger would bless the alteration.
    """
    from qt import thesis as th
    try:
        entries = th.load(ledger_path)
    except ValueError as e:
        return {"ok": False, "why": str(e), "theses": [], "entries": 0}
    v = th.verify(entries)
    if not v["ok"]:
        return {"ok": False, "why": f"ledger chain broken — {v['why']}",
                "theses": [], "entries": len(entries)}
    st = th.state(entries)
    return {"ok": True, "why": "chain intact", "entries": len(entries),
            "theses": [s for s in st.values() if s["status"] == "open"],
            "all": list(st.values()),
            "drifting": th.unreviewed_fired(entries),
            "summary": th.summary(entries, today_utc())}


def thesis_tickers(ledger_path: str) -> set:
    t = open_theses(ledger_path)
    return {s["ticker"].strip().upper() for s in t["theses"]
            if s.get("ticker")} if t["ok"] else set()


def watched_universe(daily_json: str, panel_csv: str,
                     ledger_path: str = "") -> dict:
    """-> {"tickers": set, "source": str, "thesis_added": int}

    Today's eligible list when the daily snapshot is on hand, otherwise
    every ticker the frozen panel ever contained. The fallback is WIDER on
    purpose: watching too many names is a nuisance, silently watching fewer
    than claimed is a lie. Open-thesis names are added on top either way —
    a position does not stop mattering because a liquidity filter moved.
    """
    out, source = set(), "none"
    d = read_json(daily_json) if daily_json else None
    if d:
        out = {str(t).strip().upper()
               for t in ((d.get("universe") or {}).get("eligible_tickers")
                         or []) if str(t).strip()}
        if out:
            source = f"daily snapshot {d.get('asof_session', '?')}"
    if not out and panel_csv and os.path.exists(panel_csv):
        from qt import tickers as qtk
        df = qtk.read_csv(panel_csv, usecols=["ticker"], dtype={"ticker": str})
        out = set(qtk.normalise(df["ticker"]).tolist())
        source = "frozen panel (wider; no daily snapshot on hand)"
    mine = thesis_tickers(ledger_path) if ledger_path else set()
    added = len(mine - out)
    return {"tickers": {t for t in (out | mine) if t}, "source": source,
            "thesis_added": added, "thesis_tickers": mine}


# -- the operator's stance ---------------------------------------------------

STANCES = ("conservative", "balanced", "aggressive")


def load_stance(path: str) -> dict:
    """-> {"stance", "source", "set_at", "valid"}

    A missing or unreadable file is `balanced`, and SAYS it is a default.
    A stance nobody set must not be presented as the operator's choice.
    """
    d = read_json(path) if path else None
    s = str((d or {}).get("stance") or "").strip().lower()
    if s in STANCES:
        return {"stance": s, "valid": True, "set_at": (d or {}).get("set_at"),
                "source": f"{path}"}
    return {"stance": "balanced", "valid": False, "set_at": None,
            "source": "default — no valid stance is set in " + str(path)}


# -- what resolved in the latest run -----------------------------------------

def is_form4(record: dict) -> bool:
    return (record.get("kind", "filing") == "filing"
            and str(record.get("root_form") or record.get("form") or "")
            .startswith("4"))


def resolved_in_run(records_by_day: dict, run_at: str) -> list:
    """Form 4 directions observed, and events derived, IN THE RUN at `run_at`.

    -> [record + {"day": filing day}], oldest filing day first.

    🔑 Why this exists. Finnhub reports a Form 4's direction about two days
    after EDGAR lists it, so OBSERVER fills it into an OLDER day file. A
    consumer that reads only the newest file sees a day that is almost
    entirely undirected, every night: the brief showed no purchase and no
    cluster buy from 11-16 to 11-24 while 185 and 29 had resolved. This is
    what a consumer reads instead. Matching is on the exact run timestamp
    OBSERVER stamps on each fill, so a record is reported on the one night
    it resolved and never again.
    """
    out = []
    if not run_at:
        return out
    for day in sorted(records_by_day):
        for r in records_by_day[day] or []:
            hit = ((is_form4(r) and r.get("direction")
                    and r.get("direction_observed_at") == run_at)
                   or (r.get("kind") == "derived_event"
                       and r.get("observed_at") == run_at))
            if hit:
                out.append(dict(r, day=day))
    return out

