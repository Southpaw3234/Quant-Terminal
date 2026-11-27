#!/usr/bin/env python3
"""Event files for the types in data/registry/described.json. EVENTS ONLY.

This computes no return. research/describe_events.py turns these into
outcome ledgers; keeping the two apart is the same separation that held
between extract_8k_car.py and event_study.py for specification #3.

    8-K items     SEC EDGAR submissions API, the structured `items` field,
                  one request per universe name (ticker -> CIK via EDGAR
                  full-text search, as extract_8k_car.py does)
    form4_sale    the 1095-day Finnhub pull of run 33927199851, filtered
                  with OBSERVER's own direction_of(), so a historical "sale"
                  is exactly what the nightly brief calls a sale

    python research/extract_described_events.py --form4-raw PATH
        -> data/events/described/events_<type>.csv

Columns match event_study.py's input: event_id, ticker, event_ts,
event_type, plus provenance (filing_date, acceptance_utc or n_filings).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

REGISTRY = "data/registry/described.json"
UNIVERSE = "data/universe/v27_universe.csv"
OUT_DIR = os.environ.get("QT_DESC_DIR", "data/events/described")
WINDOW_START, WINDOW_END = "2023-09-01", "2026-08-31"
ET = "America/New_York"


# ── pure logic ───────────────────────────────────────────────────────────────

def event_ts_8k(acceptance_utc: str) -> "str | None":
    """The acceptance instant in ET -- the timestamp the engine enters after.

    Bars are dated at midnight, and event_study's guard enters on the first
    bar STRICTLY AFTER this, so every 8-K enters at the close of the next
    session after its ET acceptance date: after-close filings exactly when
    they were tradeable, pre-market and intraday filings one session later
    than they could have been. That is conservative on purpose -- the first
    day's reaction is left out rather than risk counting a close the filing
    had not reached. validate_described_events.py pins all three cases.
    """
    try:
        t = pd.Timestamp(acceptance_utc)
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(ET).tz_localize(None).isoformat()


def item_events(filings: list, ticker: str, items: list) -> dict:
    """{item: [event rows]} from one name's submissions rows.

    Originals only (form == "8-K"); one event per ticker per filing date per
    item, the earliest acceptance that day.
    """
    out = {i: {} for i in items}
    for f in filings:
        if f.get("form") != "8-K":
            continue
        d = str(f.get("filing_date") or "")
        if not (WINDOW_START <= d <= WINDOW_END):
            continue
        listed = {x.strip() for x in str(f.get("items") or "").split(",")}
        for i in items:
            if i not in listed:
                continue
            prev = out[i].get(d)
            if prev is None or f["acceptance"] < prev["acceptance_utc"]:
                ts = event_ts_8k(f["acceptance"])
                if ts:
                    out[i][d] = {"event_id": f"{ticker}_{d}_8k{i}",
                                 "ticker": ticker, "event_ts": ts,
                                 "event_type": f"8k_item_{i}",
                                 "filing_date": d,
                                 "acceptance_utc": f["acceptance"]}
    return {i: list(v.values()) for i, v in out.items()}


def form4_sale_events(raw: pd.DataFrame, universe: set) -> pd.DataFrame:
    """One event per ticker per filing date with a filing OBSERVER calls a sale."""
    from observer_filings import direction_of
    raw = raw.copy()
    raw["ticker"] = raw["ticker"].astype(str).str.upper().str.strip()
    raw = raw[raw["ticker"].isin(universe)
              & raw["filing_date"].between(WINDOW_START, WINDOW_END)]
    rows = []
    for (tk, fd, acc), grp in raw.groupby(["ticker", "filing_date", "tx_id"]):
        recs = [{"code": str(c), "is_derivative": str(d).strip().lower() == "true"}
                for c, d in zip(grp["code"], grp["is_derivative"])]
        if direction_of(recs) == "sale":
            rows.append((tk, fd, acc))
    df = pd.DataFrame(rows, columns=["ticker", "filing_date", "tx_id"])
    out = (df.groupby(["ticker", "filing_date"]).size()
           .rename("n_filings").reset_index())
    out["event_id"] = "f4s_" + out["ticker"] + "_" + out["filing_date"]
    out["event_ts"] = out["filing_date"]
    out["event_type"] = "form4_sale"
    return out[["event_id", "ticker", "event_ts", "event_type",
                "filing_date", "n_filings"]]


# ── network ──────────────────────────────────────────────────────────────────

def fetch_8k_rows(sess, cik: int) -> list:
    import extract_8k_car as x8
    st, body = x8._get(sess, x8.SUBS_URL.format(name=f"CIK{cik:010d}.json"))
    time.sleep(x8.SLEEP)
    if st != "ok":
        return []
    chunks = [(body.get("filings", {}) or {}).get("recent", {}) or {}]
    for extra in (body.get("filings", {}) or {}).get("files", []) or []:
        if str(extra.get("filingTo", "")) >= WINDOW_START:
            st2, b2 = x8._get(sess, x8.SUBS_URL.format(name=extra["name"]))
            time.sleep(x8.SLEEP)
            if st2 == "ok":
                chunks.append(b2)
    rows = []
    for ch in chunks:
        n = len(ch.get("form", []) or [])
        col = lambda k: (ch.get(k) or []) + [""] * n   # noqa: E731
        for i in range(n):
            rows.append({"form": col("form")[i], "filing_date": col("filingDate")[i],
                         "items": col("items")[i],
                         "acceptance": str(col("acceptanceDateTime")[i])})
    return rows


def resolve_cik(sess, tk: str):
    """extract_8k_car.resolve_cik, with a second query for names that filed
    no Item 2.02 in the window (they can still file a 3.01 or a 5.02)."""
    import extract_8k_car as x8
    cik = x8.resolve_cik(sess, tk)
    if cik is not None:
        return cik
    st, body = x8._get(sess, x8.FTS_URL, params={
        "q": '"Item"', "forms": "8-K", "entityName": tk,
        "startdt": WINDOW_START, "enddt": WINDOW_END})
    time.sleep(x8.SLEEP)
    if st != "ok":
        return None
    for h in ((body.get("hits", {}) or {}).get("hits", []) or []):
        src = h.get("_source", {}) or {}
        names = " ".join(src.get("display_names", []) or [])
        if f"({tk})" in names or f"({tk}," in names or f", {tk})" in names:
            ciks = src.get("ciks") or []
            if ciks:
                return int(ciks[0])
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--form4-raw", required=True)
    ap.add_argument("--skip-8k", action="store_true")
    a = ap.parse_args()

    reg = json.load(open(REGISTRY, encoding="utf-8"))
    types = sorted(reg["described"])
    items = [t.split("_")[-1] for t in types if t.startswith("8k_item_")]
    universe = set(pd.read_csv(UNIVERSE, keep_default_na=False)["ticker"]
                   .astype(str).str.upper())
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"[describe-events] {len(universe)} names; window {WINDOW_START}.."
          f"{WINDOW_END}; types {types}")

    if "form4_sale" in types:
        raw = pd.read_csv(a.form4_raw, keep_default_na=False,
                          dtype={"ticker": str, "code": str})
        ev = form4_sale_events(raw, universe)
        ev.to_csv(os.path.join(OUT_DIR, "events_form4_sale.csv"), index=False,
                  lineterminator="\n")
        print(f"[describe-events] form4_sale: {len(ev)} events, "
              f"{ev['ticker'].nunique()} names")

    if items and not a.skip_8k:
        import extract_8k_car as x8
        sess = x8._session()
        found = {i: [] for i in items}
        unresolved, t0 = [], time.time()
        for k, tk in enumerate(sorted(universe), 1):
            cik = resolve_cik(sess, tk)
            if cik is None:
                unresolved.append(tk)
                continue
            for i, evs in item_events(fetch_8k_rows(sess, cik), tk, items).items():
                found[i].extend(evs)
            if k % 100 == 0:
                print(f"  ...{k}/{len(universe)} names, {len(unresolved)} "
                      f"unresolved, {sum(map(len, found.values()))} events, "
                      f"{time.time() - t0:.0f}s")
        for i, evs in found.items():
            df = pd.DataFrame(evs).sort_values(["event_ts", "ticker"])
            df.to_csv(os.path.join(OUT_DIR, f"events_8k_item_{i}.csv"),
                      index=False, lineterminator="\n")
            print(f"[describe-events] 8k_item_{i}: {len(df)} events, "
                  f"{df['ticker'].nunique()} names")
        with open(os.path.join(OUT_DIR, "unresolved_ciks.json"), "w",
                  encoding="utf-8") as f:
            json.dump(sorted(unresolved), f, indent=0)
        print(f"[describe-events] {len(unresolved)} names with no CIK found "
              f"(listed in unresolved_ciks.json)")
    print("[describe-events] events only. No return computed.")


if __name__ == "__main__":
    main()
