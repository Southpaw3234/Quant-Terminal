#!/usr/bin/env python3
"""OBSERVER — module 1 of the collective agent. Filings only.

See docs/COLLECTIVE_AGENT_BRIEF.md §3.1. This agent COLLECTS. It does not
score, rank, recommend, or drop a record because it looks unimportant.

    collect  ->  observations/YYYY-MM-DD.jsonl  ->  digest -> Discord

Every record carries `observed_at`, its source, and a link to the filing.

─────────────────────────────────────────────────────────────────────────────
🔑 WHAT THIS AGENT CANNOT SEE, AND WHY IT SAYS SO

The brief asks module 1 for "Form 4 cluster buys" and "first open-market
purchase in N years". **Both require the Form 4 BODY, and the body is
unreachable.** Transaction codes live in the ownership XML under
`www.sec.gov/Archives/`, and that host answers 403 to this client — the same
403 that cost specification #4's evidence feed a whole run.

So the direction of a Form 4 -- bought or sold -- is NOT KNOWN here.

That is reported as `direction: null` with a reason, never guessed and never
omitted. Insider SALES vastly outnumber purchases, so presenting undirected
Form 4 activity as if it were buying would be wrong in the most flattering
possible direction, every single day.

If `FINNHUB_API_KEY` is configured, direction is attempted from Finnhub's
insider-transactions endpoint. When that is unavailable the field stays
null and `direction_source` says why. **A zero that cannot be non-zero is
not a measurement**, and neither is a direction nobody could observe.

─────────────────────────────────────────────────────────────────────────────
WHAT IT DOES SEE

  8-K   the ticker is in the EFTS display_names, and the `items` field says
        which item was reported. Fully resolvable.
  4     the ticker is NOT in display_names. The issuer is identified by
        asking data.sec.gov which of the filing's CIKs has tickers -- an
        operating company does, a person does not.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import edgar
from qt import tickers as qtk

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time, and any
# broker SDK. OBSERVER collects; it cannot spend a read, evaluate a criterion
# or place an order. validate_observer_filings.py asserts this with `ast`.

OUT_DIR = os.environ.get("QT_OBS_DIR", "observations")
UNIVERSE = os.environ.get("QT_OBS_UNIVERSE", "")      # daily.json, optional
PANEL = os.environ.get("QT_OBS_PANEL", "data/universe/v29_universe_pit.csv")
CACHE = os.environ.get("QT_OBS_CACHE", "observations/cik_cache.json")
FORMS = ("8-K", "4")
MAX_PAGES = int(os.environ.get("QT_OBS_PAGES", "12"))
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"


# ── the watched universe ─────────────────────────────────────────────────────

def watched() -> set:
    """Names currently in the universe.

    Prefers the daily snapshot's eligible list (today's screen) and falls
    back to every ticker the frozen panel ever contained. The fallback is
    WIDER, not narrower: an observer that silently watches fewer names than
    it claims is worse than one that watches too many.
    """
    if UNIVERSE and os.path.exists(UNIVERSE):
        try:
            d = json.loads(open(UNIVERSE, encoding="utf-8").read())
            elig = (d.get("universe") or {}).get("eligible_tickers") or []
            if elig:
                return {str(t).strip().upper() for t in elig if str(t).strip()}
        except (OSError, ValueError):
            pass
    if os.path.exists(PANEL):
        df = qtk.read_csv(PANEL, usecols=["ticker"], dtype={"ticker": str})
        return set(qtk.normalise(df["ticker"]).tolist())
    return set()


# ── pure shaping ─────────────────────────────────────────────────────────────

def filing_url(cik, accession: str) -> str:
    """A link a human can click. Dashes stripped: EDGAR's path form."""
    return FILING_URL.format(cik=int(cik), acc=str(accession).replace("-", ""))


def observation(form: str, hit: dict, ticker: str, cik, observed_at: str,
                direction=None, direction_source: str = "") -> dict:
    """One record. Append-only, source-linked, and honest about direction."""
    src = hit.get("_source") or {}
    acc = edgar.accession_of(hit)
    return {
        "observed_at": observed_at,
        "source": "sec-edgar-fts",
        "form": form,
        "accession": acc,
        "filed": src.get("file_date"),
        "ticker": ticker,
        "cik": int(cik) if cik is not None else None,
        "entity": (src.get("display_names") or [None])[0],
        "items": src.get("items") or [],
        "url": filing_url(cik, acc) if cik is not None else None,
        # null means NOT OBSERVED. It never means "no purchase".
        "direction": direction,
        "direction_source": direction_source or "unavailable: Form 4 bodies "
                            "are on www.sec.gov, which answers 403",
    }


def digest(records: list, day: str, totals: dict) -> str:
    """The human-readable summary. States what was not observable."""
    by_form = {}
    for r in records:
        by_form.setdefault(r["form"], []).append(r)

    lines = [f"**Filings for {day}** — {len(records)} in the watched universe"]
    for form in FORMS:
        rs = by_form.get(form, [])
        t = totals.get(form, {})
        lines.append("")
        lines.append(f"__{form}__ — {len(rs)} of {t.get('filings', 0)} "
                     f"filings market-wide"
                     + ("  _(sampled, not a full census)_"
                        if t.get("truncated") else ""))
        if not rs:
            lines.append("_none_")
            continue
        for r in sorted(rs, key=lambda x: x["ticker"])[:25]:
            items = (", ".join(str(i) for i in r["items"][:3])
                     if r["items"] else "")
            bits = [f"`{r['ticker']}`"]
            if items:
                bits.append(f"item {items}")
            if r["direction"]:
                bits.append(f"**{r['direction']}**")
            bits.append(f"<{r['url']}>")
            lines.append("· " + " — ".join(bits))
        if len(rs) > 25:
            lines.append(f"_…and {len(rs) - 25} more_")

    if any(r["form"] == "4" and r["direction"] is None for r in records):
        lines.append("")
        lines.append("⚠️ **Form 4 direction is not observed.** Transaction "
                     "codes are in the filing body, which EDGAR serves from "
                     "a host that refuses this client. These are filings, "
                     "**not** purchases — insider sales outnumber buys, so "
                     "do not read them as buying.")
    return "\n".join(lines)


# ── network ──────────────────────────────────────────────────────────────────

def last_filing_day(sess, probe_form: str = "8-K") -> "str | None":
    """Walk back to the most recent day that has filings.

    A weekend, a holiday and "not published yet" are the same observation
    from here: zero hits.
    """
    day = dt.date.today()
    for _ in range(7):
        if edgar.efts_page(sess, probe_form, day, 0)["total"] > 0:
            return str(day)
        day -= dt.timedelta(days=1)
    return None


def load_cache() -> dict:
    try:
        return {int(k): v for k, v in
                json.loads(open(CACHE, encoding="utf-8").read()).items()}
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in cache.items()}, f, indent=0,
                  sort_keys=True)
        f.write("\n")


def main() -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    univ = watched()
    print(f"[observer] watching {len(univ)} tickers")
    if not univ:
        print("[observer] no universe — refusing to report an empty watch "
              "list as 'nothing happened'")
        sys.exit(2)

    sess = edgar.session(os.environ.get("SEC_USER_AGENT", ""))
    day = last_filing_day(sess)
    if day is None:
        print("[observer] no EDGAR filings found in the last 7 days")
        sys.exit(3)
    print(f"[observer] filing day {day}")

    cache = load_cache()
    records, totals = [], {}

    for form in FORMS:
        res = edgar.efts_day(sess, form, day, MAX_PAGES)
        filings = edgar.dedupe_filings(res["hits"])
        totals[form] = {"documents": res["total_documents"],
                        "filings": len(filings),
                        "truncated": res["truncated"]}
        print(f"[observer] {form}: {len(filings)} filings "
              f"({res['total_documents']} documents)"
              f"{'  [TRUNCATED]' if res['truncated'] else ''}")

        for acc, hit in filings.items():
            src = hit.get("_source") or {}
            ciks = src.get("ciks") or []
            if form == "8-K":
                tks = edgar.tickers_from_display_names(src.get("display_names"))
                hit_tk = next((t for t in sorted(tks) if t in univ), None)
                cik = int(ciks[0]) if ciks else None
                if hit_tk:
                    records.append(observation(form, hit, hit_tk, cik, now,
                                               direction_source="n/a"))
            else:
                # Form 4 carries no ticker. Ask data.sec.gov which CIK is the
                # issuer: an operating company has tickers, a person does not.
                for c in ciks:
                    ci = int(c)
                    if ci not in cache:
                        cache[ci] = edgar.submissions(sess, ci)
                cik, tk = edgar.pick_issuer(ciks, cache)
                if tk and tk in univ:
                    records.append(observation(form, hit, tk, cik, now))

    save_cache(cache)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{day}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")

    text = digest(records, day, totals)
    with open(os.path.join(OUT_DIR, f"{day}.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")

    print(f"\n[observer] wrote {path} — {len(records)} observations")
    for form in FORMS:
        n = sum(1 for r in records if r["form"] == form)
        print(f"  {form:<5} {n} in universe of {totals[form]['filings']} filings")
    print(f"  cik cache {len(cache)} entries")
    print("\n" + text[:1200])
    print("\n[observer] collected only. Nothing scored, nothing recommended, "
          "no order placed, K unchanged.")


if __name__ == "__main__":
    main()
