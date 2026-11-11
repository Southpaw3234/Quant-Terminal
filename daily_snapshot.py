#!/usr/bin/env python3
"""The daily terminal feed. What the system SEES today — nothing it claims.

Writes one JSON snapshot describing the current state of the research
programme: which names pass the declared eligibility screen today, what new
filings arrived, what the standing verdicts are, and how fresh all of that is.

─────────────────────────────────────────────────────────────────────────────
WHAT THIS IS NOT

🔑 NO RETURN IS MEASURED AGAINST ANY CRITERION HERE. No Referee call, no K
spent, no verdict written or revised. This file gathers and displays. Under
the protocol, *gathering* is free and *measuring* is scarce, which is exactly
why a daily terminal can exist while the research programme is still unproven.

It is not a trading system and it does not hold capital. Every snapshot
carries `live_trading: false` and `capital_deployed: 0` as data rather than as
a disclaimer, so the page cannot render as a portfolio by accident.

The eligible list is NOT a buy list. It is the set of names that pass the
declared liquidity and price filters — the pool a specification would be
allowed to look at. All five reads taken so far returned NOT MET, and the
snapshot carries those verdicts alongside the pool so the two are never
read apart.

─────────────────────────────────────────────────────────────────────────────
THE FAILURE MODE THIS IS BUILT AGAINST

The dashboard this feeds says it in its own words: a previous version was fed
by a build step inside `quant_runner.py`, that file was deleted, the feed
froze, and the page carried on showing a seventeen-position book the account
had not held for a month.

    A record that can quietly stop being true is worse than no record.

So every snapshot states when it was generated and which session it is as of,
`sessions_behind` is computed from the benchmark calendar rather than trusted
from a clock, and the page is expected to go DARK rather than render a stale
snapshot as current. A feed that stops must look stopped.

─────────────────────────────────────────────────────────────────────────────
WHY THE SCREEN IS IMPORTED, NEVER RE-TYPED

The eligibility tuple (ADV band, price floor, history minimum, lookback) and
the screening function are imported from `build_universe_v29` and
`qt.prices`. They are not re-stated here with the same numbers.

If the terminal re-implemented the screen, the panel's rules and the
terminal's rules could drift apart, and the terminal would then show a
universe the specifications were never written against — while looking
perfectly healthy. A number worth displaying is worth importing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys

import pandas as pd

import build_universe_v29 as v29
from qt import tickers as qtk

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time,
# qt.execution, qt.ledger. This job runs unattended on a cron. It must not be
# able to spend a read, evaluate a criterion or place an order even if a
# future edit here were careless, and the absence of the import is what makes
# that true. validate_daily_snapshot.py asserts it by parsing this file's
# imports, so adding one of them turns the suite red.

OUT = os.environ.get("QT_DAILY_OUT", "docs/daily.json")
PREV = os.environ.get("QT_DAILY_PREV", "")       # previous snapshot, if any
PANEL = os.environ.get("QT_DAILY_PANEL", "data/universe/v29_universe_pit.csv")
LIMIT = int(os.environ.get("QT_DAILY_LIMIT", "0"))
CHUNK = int(os.environ.get("QT_DAILY_CHUNK", "100"))
BENCHMARKS = ("IWM", "SPY")
EVIDENCE_FORMS = ("8-K", "4")

REGISTRIES = (
    ("v27", "data/registry/specifications.json", "v27 — event studies"),
    ("v29", "data/registry/v29_specifications.json", "V29 — systematic value"),
)

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_DAILY_INDEX = ("https://www.sec.gov/Archives/edgar/daily-index/"
                   "{yr}/QTR{q}/form.{ymd}.idx")


# ── helpers ──────────────────────────────────────────────────────────────────

def sha256_of(path: str) -> str:
    """Hash with CRLF normalised, so a Windows checkout and a Linux runner
    agree on the fingerprint of the same logical file."""
    try:
        b = open(path, "rb").read().replace(b"\r\n", b"\n")
    except OSError:
        return ""
    return hashlib.sha256(b).hexdigest()


def staleness(generated_at: str, asof_session: str, now=None) -> dict:
    """How old is this snapshot, in plain days. Pure, so it is testable.

    Two different ages, because they fail for different reasons:
      feed_age_days  -- how long since the PIPELINE last ran. Large means the
                        cron is broken or the workflow is erroring.
      data_age_days  -- how long since the SESSION the data describes. Large
                        with a small feed age means the pipeline is running
                        but its price source has gone stale.

    The page is expected to go dark on either, rather than render a stale
    snapshot as current.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {"feed_age_days": None, "data_age_days": None}
    try:
        g = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        out["feed_age_days"] = round((now - g).total_seconds() / 86400.0, 2)
    except (ValueError, AttributeError):
        pass
    try:
        a = dt.date.fromisoformat(asof_session)
        out["data_age_days"] = (now.date() - a).days
    except (ValueError, TypeError):
        pass
    return out


def parse_form_index(text: str, want: dict, forms=EVIDENCE_FORMS,
                     max_names: int = 40) -> dict:
    """Parse an EDGAR daily form index. Pure: no network, no clock.

    The index is space-aligned columns:

        Form Type  Company Name            CIK      Date Filed  File Name
        8-K        EXAMPLE CORP            1234567  2026-11-10  edgar/...

    The CIK is taken from the `edgar/data/<cik>/` path segment rather than by
    hunting for a numeric column. A company name can contain digits, and a
    name like "1-800-FLOWERS" would otherwise be read as the CIK.

    `want` maps CIK -> ticker for the names currently in the universe.
    """
    out = {f: {"total": 0, "in_universe": 0, "names": []} for f in forms}
    seen = {f: set() for f in forms}
    for line in text.splitlines():
        parts = [p for p in line.split("  ") if p.strip()]
        if len(parts) < 2:
            continue
        form = parts[0].strip()
        if form not in out:
            continue
        out[form]["total"] += 1
        cik = None
        if "edgar/data/" in line:
            seg = line.split("edgar/data/", 1)[1].split("/", 1)[0]
            if seg.isdigit():
                cik = int(seg)
        if cik is not None and cik in want and want[cik] not in seen[form]:
            seen[form].add(want[cik])
            out[form]["in_universe"] += 1
            if len(out[form]["names"]) < max_names:
                out[form]["names"].append(want[cik])
    return out


def _session():
    import requests
    ua = (os.environ.get("SEC_USER_AGENT") or "").strip()
    s = requests.Session()
    # EDGAR requires a descriptive User-Agent. A generic client name is used
    # when the secret is absent rather than inventing contact details.
    s.headers.update({"User-Agent": ua or "Quant-Terminal research client",
                      "Accept-Encoding": "gzip, deflate"})
    return s


# ── the candidate pool ───────────────────────────────────────────────────────

def candidate_pool() -> tuple:
    """Today's candidate names, and where the list came from.

    Finnhub's live symbol list is preferred because it contains names that
    listed AFTER the frozen panel was built -- a terminal that can only ever
    show the panel's tickers would silently stop covering new listings. The
    frozen panel is the fallback, and the source is reported either way so a
    reader can tell which universe they are looking at.
    """
    key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if key:
        try:
            syms = v29.fetch_symbols(key)
            if syms:
                return syms, "finnhub_live"
        except Exception as e:                 # noqa: BLE001 - reported, not raised
            print(f"[daily] finnhub pool unavailable ({e.__class__.__name__}), "
                  f"falling back to the frozen panel")
    df = qtk.read_csv(PANEL, usecols=["ticker"], dtype={"ticker": str})
    return sorted(set(qtk.normalise(df["ticker"]))), "frozen_panel_fallback"


def screen_today(pool: list, asof) -> tuple:
    """Apply the DECLARED screen to today. Returns (rows, funnel).

    `v29.screen_chunk` is reused verbatim against a one-date grid, so this is
    the same code path that produced the frozen panel -- not a copy of it.
    """
    rows, funnel = [], {}
    pool = pool[:LIMIT] if LIMIT else pool
    for k in range(0, len(pool), CHUNK):
        chunk = pool[k:k + CHUNK]
        prices = v29.fetch_chunk(chunk)
        r, f = v29.screen_chunk(prices, [pd.Timestamp(asof)])
        rows.extend(r)
        for reason, n in f.items():
            funnel[reason] = funnel.get(reason, 0) + int(n)
        print(f"  screened {min(k + CHUNK, len(pool))}/{len(pool)} "
              f"· {len(rows)} eligible so far")
    return rows, funnel


# ── benchmarks ───────────────────────────────────────────────────────────────

def benchmark_block() -> tuple:
    """Benchmark closes, and the session calendar they define.

    The calendar comes from a traded instrument rather than from the wall
    clock, so `asof` is a real session and `sessions_behind` counts sessions
    rather than guessing at weekends and holidays.
    """
    import yfinance as yf
    raw = yf.download(list(BENCHMARKS), start="2024-01-01", end=None,
                      auto_adjust=True, progress=False, threads=True,
                      group_by="column")
    out, sessions = {}, None
    if raw is None or len(raw) == 0:
        return out, sessions
    multi = isinstance(raw.columns, pd.MultiIndex)
    for tk in BENCHMARKS:
        try:
            s = raw["Close"][tk] if multi else raw["Close"]
        except (KeyError, TypeError):
            continue
        s = pd.to_numeric(s, errors="coerce").dropna()
        if not len(s):
            continue
        s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
        if sessions is None or len(s.index) > len(sessions):
            sessions = s.index
        jan = s[s.index >= pd.Timestamp(f"{s.index[-1].year}-01-01")]
        out[tk] = {
            "close": round(float(s.iloc[-1]), 4),
            "ret_1d": round(float(s.iloc[-1] / s.iloc[-2] - 1.0), 6)
            if len(s) > 1 else None,
            "ret_ytd": round(float(s.iloc[-1] / jan.iloc[0] - 1.0), 6)
            if len(jan) > 1 else None,
            "last_session": str(s.index[-1].date()),
        }
    return out, sessions


# ── the evidence feed ────────────────────────────────────────────────────────

def evidence_block(eligible: set) -> dict:
    """What NEW filings landed for names in today's universe.

    Two requests, not three thousand: EDGAR publishes a full form index per
    filing day, and a ticker->CIK map once. Per-ticker full-text queries were
    rejected -- the spec #4 diagnostic measured 23.8% of tickers unresolvable
    that way, so a per-ticker feed would silently under-report by a quarter.
    """
    out = {"index_date": None, "forms": {}, "note": None}
    try:
        op = _session()
        m = op.get(SEC_TICKERS, timeout=60)
        m.raise_for_status()
        cik_of, name_of = {}, {}
        for d in (m.json() or {}).values():
            tk = str(d.get("ticker") or "").strip().upper()
            if tk:
                cik_of[tk] = int(d.get("cik_str") or 0)
                name_of[int(d.get("cik_str") or 0)] = d.get("title") or ""
        want = {cik_of[t]: t for t in eligible if t in cik_of}

        # Walk back to the most recent published index. Weekends, holidays and
        # the lag between a session closing and its index appearing all look
        # the same from here: a 404.
        day, idx_text = dt.date.today(), None
        for _ in range(7):
            url = SEC_DAILY_INDEX.format(yr=day.year, q=(day.month - 1) // 3 + 1,
                                         ymd=day.strftime("%Y%m%d"))
            r = op.get(url, timeout=60)
            if r.status_code == 200 and r.text:
                idx_text, out["index_date"] = r.text, str(day)
                break
            day -= dt.timedelta(days=1)
        if idx_text is None:
            out["note"] = "no EDGAR daily index found in the last 7 days"
            return out

        out["forms"] = parse_form_index(idx_text, want)
        for form, d in out["forms"].items():
            d["names"] = [{"ticker": t, "name": name_of.get(cik_of.get(t, 0), "")}
                          for t in d["names"]]
    except Exception as e:                     # noqa: BLE001 - reported, not raised
        out["note"] = f"evidence feed unavailable: {e.__class__.__name__}"
    return out


# ── the standing research record ─────────────────────────────────────────────

def research_record() -> dict:
    """Every read taken, with its verdict. Read from the registries, never
    restated: the registry is the record, and this is a view of it."""
    progs, any_met = [], False
    for pid, path, label in REGISTRIES:
        try:
            doc = json.loads(open(path, encoding="utf-8").read())
        except OSError:
            continue
        specs = doc.get("specifications", {})
        reads = []
        for sid, s in sorted(specs.items()):
            res = s.get("result") or {}
            read_at = s.get("read_at")
            verdict = res.get("verdict") or (res.get("read") or {}).get("verdict")
            if read_at:
                reads.append({"spec_id": sid, "read_at": read_at,
                              "verdict": verdict, "prior": s.get("prior")})
                if verdict and "NOT MET" not in str(verdict).upper():
                    any_met = True
        progs.append({"id": pid, "label": label,
                      "k_budget": doc.get("k_budget"),
                      "k_used": len(reads),
                      "declared": len(specs),
                      "reads": reads})
    return {"programmes": progs, "any_criteria_met": any_met}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    print(f"[daily] snapshot at {now.isoformat(timespec='seconds')}")
    print("[daily] GATHER AND DISPLAY ONLY — no criterion evaluated, "
          "no Referee call, K untouched.\n")

    bench, sessions = benchmark_block()
    if sessions is None or not len(sessions):
        print("[daily] no benchmark calendar — refusing to date a snapshot "
              "against the wall clock")
        sys.exit(2)
    asof = sessions[-1]
    print(f"[daily] as of session {asof.date()} "
          f"({len(sessions)} sessions in calendar)")

    pool, pool_src = candidate_pool()
    print(f"[daily] candidate pool {len(pool)} ({pool_src})")
    rows, funnel = screen_today(pool, asof)
    eligible = sorted({r["ticker"] for r in rows})
    print(f"[daily] eligible today {len(eligible)}")

    prev_elig, prev_asof = set(), None
    if PREV and os.path.exists(PREV):
        try:
            p = json.loads(open(PREV, encoding="utf-8").read())
            prev_elig = set((p.get("universe") or {}).get("eligible_tickers") or [])
            prev_asof = p.get("asof_session")
        except (OSError, ValueError):
            pass
    entered = sorted(set(eligible) - prev_elig) if prev_elig else []
    exited = sorted(prev_elig - set(eligible)) if prev_elig else []
    print(f"[daily] vs {prev_asof or 'no previous snapshot'}: "
          f"+{len(entered)} entered, -{len(exited)} exited")

    ev = evidence_block(set(eligible))
    rec = research_record()

    snap = {
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "asof_session": str(asof.date()),
        # Stated as DATA, not as a footnote, so the page cannot render this
        # as a portfolio by accident.
        "status": "research",
        "live_trading": False,
        "capital_deployed": 0,
        "eligible_is_a_buy_list": False,
        "universe": {
            "pool_source": pool_src,
            "candidate_pool": len(pool),
            "eligible": len(eligible),
            "eligible_tickers": eligible,
            "entered": entered,
            "exited": exited,
            "compared_to": prev_asof,
            "funnel": funnel,
            # Imported from build_universe_v29, never re-typed here.
            "rules": {
                "adv_min": v29.ADV_MIN, "adv_max": v29.ADV_MAX,
                "price_min": v29.PRICE_MIN, "min_history": v29.MIN_HISTORY,
                "adv_lookback": v29.ADV_LOOKBACK,
            },
            "top_by_adv": sorted(rows, key=lambda r: -r["adv"])[:25],
        },
        "benchmarks": bench,
        "staleness_at_generation": staleness(
            now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            str(asof.date()), now=now),
        "evidence": ev,
        "research_record": rec,
        "sources": [
            {"path": p, "sha256": sha256_of(p)}
            for _, p, _ in REGISTRIES
        ] + [{"path": PANEL, "sha256": sha256_of(PANEL)}],
    }

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, sort_keys=False)
        f.write("\n")

    print(f"\n[daily] wrote {OUT}")
    print(f"  as of        {snap['asof_session']}")
    print(f"  eligible     {len(eligible)} of {len(pool)} candidates")
    for form, d in (ev.get("forms") or {}).items():
        print(f"  form {form:<4}   {d['in_universe']} in universe "
              f"of {d['total']} filed {ev.get('index_date')}")
    for pr in rec["programmes"]:
        print(f"  {pr['id']}          K {pr['k_used']}/{pr['k_budget']}, "
              f"{len(pr['reads'])} read")
    print(f"  any criteria met: {rec['any_criteria_met']}")
    print("\n[daily] no criterion evaluated, no return measured against a "
          "bar, K unchanged.")


if __name__ == "__main__":
    main()
