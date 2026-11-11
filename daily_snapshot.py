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
import re
import sys
import time

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

# ⚠️ NOT www.sec.gov. That host answers 403 from CI and from the operator's
# machine alike, which rules out both company_tickers.json and the daily
# form index -- extract_8k_deficiency.resolve_cik carries the same note and
# routes around it the same way. EDGAR full-text search answers from both,
# and it carries the ticker in `display_names`, so no separate ticker->CIK
# map is needed at all.
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
EFTS_PAGE = 100                                  # the API's maximum
EVIDENCE_PAGES = int(os.environ.get("QT_DAILY_EVIDENCE_PAGES", "12"))
EFTS_SLEEP = 0.15


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


_PAREN = re.compile(r"\(([^)]*)\)")


def tickers_from_display_names(names) -> set:
    """Pull the ticker symbols out of an EFTS `display_names` entry. Pure.

    The field looks like:

        'BIOVIE INC.  (BIVI, BIVIW)  (CIK 0001580149)'

    so there are two traps and both are live:

      1. the LAST parenthetical is the CIK, not a ticker. Taking "the thing
         in brackets" would file every company under a ticker of "CIK".
      2. one company can list SEVERAL tickers in one parenthetical -- share
         classes and warrants -- and they are comma-separated.

    A company with no listed ticker yields nothing rather than a placeholder,
    because an empty set is a true statement and "" is a ticker that would
    later match a blank field somewhere.
    """
    out = set()
    for n in names or []:
        for grp in _PAREN.findall(str(n)):
            g = grp.strip()
            if g.upper().startswith("CIK"):
                continue
            for part in g.split(","):
                t = part.strip().upper()
                if t and len(t) <= 6 and t.replace(".", "").replace("-", "").isalnum():
                    out.add(t)
    return out


def summarise_hits(hits, eligible: set, max_names: int = 40) -> dict:
    """Fold EFTS hits into one form's counts. Pure: no network, no clock.

    🔑 DEDUPED BY ACCESSION NUMBER. EFTS indexes DOCUMENTS, not filings: one
    8-K with an exhibit and a press release is three hits. Counting hits
    would report roughly triple the filings that actually happened, and the
    number would look plausible enough never to be questioned.
    """
    filings, in_univ, names = set(), set(), []
    for h in hits or []:
        acc = str(h.get("_id") or "").split(":")[0]
        if not acc:
            continue
        filings.add(acc)
        src = h.get("_source") or {}
        for t in tickers_from_display_names(src.get("display_names")):
            if t in eligible and t not in in_univ:
                in_univ.add(t)
                if len(names) < max_names:
                    names.append(t)
    return {"filings": len(filings), "in_universe": len(in_univ),
            "names": sorted(names)}


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

def _efts(op, form: str, day, frm: int) -> dict:
    """One page of EDGAR full-text search for a form on a date.

    The query term is EMPTY on purpose. EFTS accepts forms + dates with no
    `q`, and that matters: any keyword would silently select the subset of
    filings containing that word, turning a census of the day into a biased
    sample that still looks like a census.
    """
    r = op.get(EFTS_URL, timeout=60, params={
        "q": "", "forms": form,
        "startdt": str(day), "enddt": str(day),
        "from": frm, "size": EFTS_PAGE})
    r.raise_for_status()
    body = r.json() or {}
    time.sleep(EFTS_SLEEP)
    hits = (body.get("hits") or {})
    return {"_total": ((hits.get("total") or {}).get("value") or 0),
            "_hits": hits.get("hits") or []}


def evidence_block(eligible: set) -> dict:
    """What NEW filings landed for names in today's universe.

    A handful of requests, not three thousand. Per-ticker full-text queries
    were rejected because the spec #4 diagnostic measured 23.8% of tickers
    unresolvable that way -- that feed would under-report by a quarter and
    never say so.
    """
    out = {"index_date": None, "forms": {}, "note": None}
    try:
        op = _session()

        # Walk back to the most recent day that has filings. A weekend, a
        # holiday and "the index has not been published yet" are all the same
        # observation from here: zero hits.
        day = dt.date.today()
        for _ in range(7):
            probe = _efts(op, EVIDENCE_FORMS[0], day, 0)
            if probe.get("_total", 0) > 0:
                out["index_date"] = str(day)
                break
            day -= dt.timedelta(days=1)
        if out["index_date"] is None:
            out["note"] = "no EDGAR filings found in the last 7 days"
            return out

        for form in EVIDENCE_FORMS:
            hits, total, pages = [], None, 0
            while pages < EVIDENCE_PAGES:
                page = _efts(op, form, day, pages * EFTS_PAGE)
                total = page.get("_total", 0) if total is None else total
                got = page.get("_hits") or []
                hits.extend(got)
                pages += 1
                if len(got) < EFTS_PAGE or pages * EFTS_PAGE >= (total or 0):
                    break
            d = summarise_hits(hits, eligible)
            # `documents` is what EFTS counts, `filings` is what happened.
            # Both are reported because a reader comparing this page to
            # EDGAR's own hit count deserves to see the same number.
            d["documents"] = total or 0
            d["truncated"] = (total or 0) > pages * EFTS_PAGE
            out["forms"][form] = d
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
        print(f"  form {form:<4}   {d['in_universe']} in universe of "
              f"{d['filings']} filings ({d['documents']} docs) on "
              f"{ev.get('index_date')}"
              f"{'  [TRUNCATED]' if d.get('truncated') else ''}")
    if ev.get("note"):
        # Printed, not swallowed. The first run lost the evidence feed to a
        # 403 and the log said nothing, because only the forms loop reported.
        print(f"  evidence   ⚠️  {ev['note']}")
    for pr in rec["programmes"]:
        print(f"  {pr['id']}          K {pr['k_used']}/{pr['k_budget']}, "
              f"{len(pr['reads'])} read")
    print(f"  any criteria met: {rec['any_criteria_met']}")
    print("\n[daily] no criterion evaluated, no return measured against a "
          "bar, K unchanged.")


if __name__ == "__main__":
    main()
