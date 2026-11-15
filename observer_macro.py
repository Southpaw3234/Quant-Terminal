#!/usr/bin/env python3
"""OBSERVER — module 6 of the collective agent. Macro series and headlines.

See docs/COLLECTIVE_AGENT_BRIEF.md §3.1 and §6. Same contract as the filings
collector: it COLLECTS. It does not score, rank, forecast or interpret.

    FRED                 ->  ten declared series, latest value and change
    Finnhub news         ->  market headlines, and headlines for every name
                             carrying an open thesis
                         ->  macro/YYYY-MM-DD.json + .md

-----------------------------------------------------------------------------
🔑 EVERY SERIES STATES ITS OWN AGE

A macro series is the easiest thing in this system to show stale. CPI is
published monthly; if the feed stopped in March, "CPI 3.1%" would still
look entirely reasonable in July. So each series carries the date its
latest value DESCRIBES and a verdict against the cadence that series is
actually published on, and a series past its limit is reported as stale
with its value withheld -- never shown as current.

🔑 A VALUE THAT COULD NOT BE FETCHED IS null WITH A REASON, NOT ABSENT.
The brief lists what was asked for and what came back, so a missing series
reads as "FRED did not answer" rather than as a shorter list.

🔑 HEADLINES ARE COLLECTED, NOT CURATED. They are kept newest-first up to a
stated cap, and the cap is reported with the true total. Nothing is dropped
for looking unimportant: deciding what matters is not this agent's job, and
an agent that filters by importance is a model wearing a collector's badge.
"""

from __future__ import annotations

import csv
import datetime as dt
import io as _stdio
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import agentio as io

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time, and any
# broker SDK. validate_observer_macro.py asserts this with `ast`.

OUT_DIR = os.environ.get("QT_MACRO_DIR", "macro")
LEDGER = os.environ.get("QT_THESIS_LEDGER", "data/theses/ledger.jsonl")
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_PAGE = "https://fred.stlouisfed.org/series/{id}"
FINNHUB_NEWS = "https://finnhub.io/api/v1/news"
FINNHUB_COMPANY_NEWS = "https://finnhub.io/api/v1/company-news"
NEWS_CAP = int(os.environ.get("QT_MACRO_NEWS_CAP", "25"))
COMPANY_NEWS_CAP = int(os.environ.get("QT_MACRO_COMPANY_NEWS_CAP", "10"))
COMPANY_NEWS_DAYS = 7
# A plain product token. FRED's public CSV endpoint stalls on some free-form
# agent strings and answers this shape at once; the keyed API is the primary
# path and does not care.
USER_AGENT = "research-agent/1.0"
# After this many series fail in a row the rest are not attempted. Ten
# sixty-second timeouts is a ten-minute job that reports nothing new.
FRED_BREAKER = 2

# The declared series. `max_age_days` is the cadence the series is PUBLISHED
# on plus slack, so a daily series two weeks old is stale and a monthly one
# is not. Declared here, in one place, rather than inferred from the data.
SERIES = (
    {"id": "DGS10", "title": "10-year Treasury yield", "units": "%", "max_age_days": 7},
    {"id": "DGS2", "title": "2-year Treasury yield", "units": "%", "max_age_days": 7},
    {"id": "T10Y2Y", "title": "10y minus 2y spread", "units": "pp", "max_age_days": 7},
    {"id": "DFF", "title": "Effective fed funds rate", "units": "%", "max_age_days": 7},
    {"id": "BAMLH0A0HYM2", "title": "High-yield credit spread (OAS)", "units": "pp", "max_age_days": 7},
    {"id": "VIXCLS", "title": "VIX", "units": "index", "max_age_days": 7},
    {"id": "DTWEXBGS", "title": "Broad dollar index", "units": "index", "max_age_days": 10},
    {"id": "ICSA", "title": "Initial jobless claims", "units": "claims", "max_age_days": 16},
    {"id": "UNRATE", "title": "Unemployment rate", "units": "%", "max_age_days": 75},
    {"id": "CPIAUCSL", "title": "CPI (all urban, SA)", "units": "index", "max_age_days": 75},
)


# ── pure shaping ─────────────────────────────────────────────────────────────

def _num(v) -> "float | None":
    """FRED writes a missing value as '.', and a CSV may leave it blank."""
    try:
        s = str(v).strip()
        return None if s in ("", ".") else float(s)
    except (TypeError, ValueError):
        return None


def parse_fred_json(payload: dict) -> list:
    """-> [(date, value)] oldest first, missing values dropped."""
    out = [(str(o.get("date", ""))[:10], _num(o.get("value")))
           for o in (payload or {}).get("observations") or []]
    return sorted((d, v) for d, v in out if d and v is not None)


def parse_fred_csv(text: str) -> list:
    """-> [(date, value)] oldest first, from the keyless graph CSV."""
    rows = list(csv.reader(_stdio.StringIO(text or "")))
    out = []
    for r in rows[1:]:
        if len(r) >= 2 and _num(r[1]) is not None:
            out.append((r[0].strip()[:10], _num(r[1])))
    return sorted(out)


def value_on_or_before(obs: list, day: str):
    """The last observation dated on or before `day`, or None."""
    prior = [(d, v) for d, v in obs if d <= day]
    return prior[-1] if prior else None


def series_summary(spec: dict, obs: list, now: str, source: str) -> dict:
    """One series, with its own freshness verdict.

    🔑 A stale series keeps its dates and LOSES its value. Showing an old
    number beside a warning is still showing the old number.
    """
    base = {"id": spec["id"], "title": spec["title"], "units": spec["units"],
            "url": FRED_PAGE.format(id=spec["id"]), "source": source,
            "max_age_days": spec["max_age_days"]}
    if not obs:
        return dict(base, status="missing", value=None, as_of=None,
                    reason="no observations returned")
    as_of, value = obs[-1]
    age = io.age_days(as_of, now)
    if age is None:
        # An undated value is treated exactly like a stale one.
        return dict(base, status="undated", value=None, as_of=as_of,
                    reason=f"latest observation date {as_of!r} is unreadable")
    prev = obs[-2] if len(obs) > 1 else None
    year_ago_day = (dt.date.fromisoformat(as_of)
                    - dt.timedelta(days=365)).isoformat()
    ya = value_on_or_before(obs, year_ago_day)
    out = dict(base, as_of=as_of, age_days=round(age, 1),
               previous=prev[1] if prev else None,
               previous_as_of=prev[0] if prev else None,
               year_ago=ya[1] if ya else None,
               year_ago_as_of=ya[0] if ya else None)
    if age > spec["max_age_days"]:
        return dict(out, status="stale", value=None, previous=None,
                    year_ago=None,
                    reason=f"latest observation is {age:.0f} days old; this "
                           f"series is published often enough that "
                           f"{spec['max_age_days']} days means it stopped")
    return dict(out, status="fresh", value=value,
                change=None if prev is None else round(value - prev[1], 6),
                change_1y=None if ya is None else round(value - ya[1], 6),
                reason="ok")


def shape_news(items: list, cap: int, now_ts: float, max_age_days: int = 7,
               ticker: str = "") -> dict:
    """-> {"total", "shown", "truncated", "items"} newest first.

    Kept by TIME, up to a cap that is reported with the true total. Not by
    importance: nothing here decides what matters.
    """
    seen, rows = set(), []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        head = str(it.get("headline") or "").strip()
        url = str(it.get("url") or "").strip()
        try:
            ts = float(it.get("datetime") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if not head or not url or ts <= 0:
            continue
        if now_ts - ts > max_age_days * 86400 or url in seen:
            continue
        seen.add(url)
        rows.append({"id": f"news:{it.get('id') or url}",
                     "headline": head[:240], "url": url,
                     "publisher": str(it.get("source") or "").strip(),
                     "published_at": dt.datetime.fromtimestamp(
                         ts, dt.timezone.utc).isoformat(timespec="seconds"),
                     "ticker": ticker or None})
    rows.sort(key=lambda r: r["published_at"], reverse=True)
    return {"total": len(rows), "shown": min(len(rows), cap),
            "truncated": len(rows) > cap, "items": rows[:cap]}


def digest(doc: dict) -> str:
    lines = [f"**Macro and headlines — {doc['day']}**"]
    fresh = [s for s in doc["series"] if s["status"] == "fresh"]
    dark = [s for s in doc["series"] if s["status"] != "fresh"]
    if fresh:
        lines.append("")
        for s in fresh:
            chg = "" if s.get("change") is None else f"  ({s['change']:+g} vs {s['previous_as_of']})"
            lines.append(f"· {s['title']}: **{s['value']:g}** {s['units']} "
                         f"as of {s['as_of']}{chg}")
    for s in dark:
        lines.append(f"· ⚠️ {s['title']}: **{s['status']}** — {s['reason']}")
    news = doc.get("news") or {}
    if news.get("status") != "ok":
        lines.append("")
        lines.append(f"⚠️ Headlines: **not collected** — {news.get('reason')}")
    else:
        m = news["market"]
        lines.append("")
        lines.append(f"__Market headlines__ — {m['shown']} of {m['total']}, "
                     f"newest first (collected, not curated)")
        for it in m["items"][:8]:
            lines.append(f"· {it['headline'][:110]} — <{it['url']}>")
        for tk, c in sorted((news.get("by_thesis") or {}).items()):
            if c.get("status") != "ok":
                lines.append(f"· `{tk}`: not collected — {c.get('reason')}")
                continue
            lines.append(f"__{tk}__ — {c['shown']} of {c['total']} headline(s) "
                         f"in {COMPANY_NEWS_DAYS} days")
            for it in c["items"][:4]:
                lines.append(f"· {it['headline'][:110]} — <{it['url']}>")
    return "\n".join(lines)


# ── network ──────────────────────────────────────────────────────────────────

def fetch_fred(sess, series_id: str, key: str) -> dict:
    """-> {"ok", "obs", "source", "reason"}. Keyed API first, keyless CSV as
    the fallback; a failure of both is a failure, never an empty series."""
    start = (dt.datetime.now(dt.timezone.utc).date()
             - dt.timedelta(days=800)).isoformat()
    if key:
        try:
            r = sess.get(FRED_API, timeout=40, params={
                "series_id": series_id, "api_key": key, "file_type": "json",
                "observation_start": start})
            if r.status_code == 200:
                obs = parse_fred_json(r.json())
                if obs:
                    return {"ok": True, "obs": obs, "source": "fred-api",
                            "reason": "ok"}
            why = f"fred api answered {r.status_code}"
        except Exception as e:                       # noqa: BLE001
            why = f"fred api: {type(e).__name__}"
    else:
        why = "FRED_API_KEY not configured"
    try:
        r = sess.get(FRED_CSV, timeout=20,
                     params={"id": series_id, "cosd": start})
        if r.status_code == 200:
            obs = parse_fred_csv(r.text)
            if obs:
                return {"ok": True, "obs": obs, "source": "fred-graph-csv",
                        "reason": "ok"}
        why += f"; keyless csv answered {r.status_code}"
    except Exception as e:                           # noqa: BLE001
        why += f"; keyless csv: {type(e).__name__}"
    return {"ok": False, "obs": [], "source": None, "reason": why}


def fetch_news(sess, url: str, params: dict) -> dict:
    try:
        r = sess.get(url, params=params, timeout=30)
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "items": [], "reason": type(e).__name__}
    if r.status_code != 200:
        return {"ok": False, "items": [],
                "reason": f"finnhub answered {r.status_code}"}
    try:
        data = r.json()
    except ValueError:
        return {"ok": False, "items": [], "reason": "finnhub: not JSON"}
    time.sleep(1.1)
    return {"ok": True, "items": data if isinstance(data, list) else [],
            "reason": "ok"}


def collect_news(sess, tickers: list, now_ts: float) -> dict:
    if not FINNHUB_KEY:
        return {"status": "not_collected", "market": None, "by_thesis": {},
                "reason": "FINNHUB_API_KEY not configured"}
    g = fetch_news(sess, FINNHUB_NEWS,
                   {"category": "general", "token": FINNHUB_KEY})
    if not g["ok"]:
        return {"status": "not_collected", "market": None, "by_thesis": {},
                "reason": g["reason"]}
    out = {"status": "ok", "reason": "ok", "source": "finnhub",
           "market": shape_news(g["items"], NEWS_CAP, now_ts), "by_thesis": {}}
    today = dt.datetime.now(dt.timezone.utc).date()
    for tk in tickers:
        c = fetch_news(sess, FINNHUB_COMPANY_NEWS, {
            "symbol": tk, "token": FINNHUB_KEY,
            "from": (today - dt.timedelta(days=COMPANY_NEWS_DAYS)).isoformat(),
            "to": today.isoformat()})
        if c["ok"]:
            shaped = shape_news(c["items"], COMPANY_NEWS_CAP, now_ts,
                                COMPANY_NEWS_DAYS, tk)
            out["by_thesis"][tk] = dict(shaped, status="ok", reason="ok")
        else:
            out["by_thesis"][tk] = {"status": "not_collected",
                                    "reason": c["reason"], "items": []}
    return out


def main() -> None:
    now = io.now_utc()
    day = io.today_utc()
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})
    series, streak = [], 0
    for spec in SERIES:
        if streak >= FRED_BREAKER:
            got = {"ok": False, "obs": [], "source": None,
                   "reason": f"not attempted: FRED failed {streak} series in "
                             f"a row, so the source is treated as down"}
        else:
            got = fetch_fred(sess, spec["id"], FRED_KEY)
            streak = 0 if got["ok"] else streak + 1
        if got["ok"]:
            series.append(series_summary(spec, got["obs"], now, got["source"]))
        else:
            series.append({"id": spec["id"], "title": spec["title"],
                           "units": spec["units"], "status": "missing",
                           "value": None, "as_of": None,
                           "url": FRED_PAGE.format(id=spec["id"]),
                           "source": None, "reason": got["reason"]})
        s = series[-1]
        print(f"[macro] {s['id']:<14} {s['status']:<8} "
              f"{'' if s['value'] is None else s['value']} "
              f"{s.get('as_of') or ''}  {'' if s['status'] == 'fresh' else s['reason']}")

    tickers = sorted(io.thesis_tickers(LEDGER))
    news = collect_news(sess, tickers,
                        dt.datetime.now(dt.timezone.utc).timestamp())
    print(f"[macro] headlines: {news['status']}"
          + (f" — {news['reason']}" if news["status"] != "ok" else
             f" — market {news['market']['shown']}/{news['market']['total']}, "
             f"{len(news['by_thesis'])} thesis name(s)"))

    n_fresh = sum(1 for s in series if s["status"] == "fresh")
    doc = {"generated_at": now, "day": day, "series": series, "news": news,
           "series_requested": len(SERIES), "series_fresh": n_fresh,
           "thesis_tickers": tickers}

    # 🔑 Refuse to publish a macro file with NOTHING in it. Ten missing
    # series and no headlines is an outage, and writing it as today's macro
    # picture would let it be read as one.
    if n_fresh == 0 and news["status"] != "ok":
        print("\n[macro] REFUSING TO PUBLISH — not one of "
              f"{len(SERIES)} series is fresh and no headlines were "
              "collected. That is an outage, not a quiet day.")
        print(f"::error::macro: 0 of {len(SERIES)} series fresh and no news "
              f"— {series[0]['reason']}")
        sys.exit(5)

    io.write_json(os.path.join(OUT_DIR, f"{day}.json"), doc)
    text = digest(doc)
    io.write_text(os.path.join(OUT_DIR, f"{day}.md"), text)
    print(f"\n[macro] wrote {OUT_DIR}/{day}.json — {n_fresh} of "
          f"{len(SERIES)} series fresh")
    print("\n" + text[:1200])
    print("\n[macro] collected only. Nothing forecast, nothing interpreted.")


if __name__ == "__main__":
    main()
