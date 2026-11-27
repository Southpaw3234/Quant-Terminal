#!/usr/bin/env python3
"""OBSERVER — daily closes for the names whose RISK the brief describes.

Collects only. ACTUARY turns these into volatility and loss bounds
(qt/volatility.py); this file computes nothing from them.

    which names   the operator's declared positions, every open thesis,
                  every cluster buy OBSERVER resolved in its latest run,
                  and the benchmarks SPY and IWM
    how far back  from 2017, so GJR-GARCH has its four years and the tail
                  estimate its thousand standardized days
    written to    prices/latest.csv + prices/latest.meta.json

🔑 NOT PUBLISHED. These are inputs to tonight's ACTUARY run, a few thousand
rows per name. Publishing them every night would grow the agent-output
branch by megabytes a day to store prices anyone can re-download. What is
published is what ACTUARY derives, and it names the source and the last
session it describes.

A name the price source does not return is LISTED in the meta file with the
reason, never dropped: ACTUARY reports it as "no price history", which is a
different fact from "no risk".
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import agentio as io

# 🔑 DELIBERATELY NOT IMPORTED: qt.referee, qt.wrc, qt.calendar_time,
# qt.prices and any broker SDK.

OUT_DIR = os.environ.get("QT_PRICES_DIR", "prices")
OBS_DIR = os.environ.get("QT_OBS_DIR", "observations")
POSITIONS = os.environ.get("QT_ACT_POSITIONS", "data/operator/positions.csv")
LEDGER = os.environ.get("QT_THESIS_LEDGER", "data/theses/ledger.jsonl")
START = os.environ.get("QT_PRICES_START", "2017-01-01")
BENCHMARKS = ("SPY", "IWM")
MAX_NAMES = int(os.environ.get("QT_PRICES_MAX", "60"))


def wanted(positions_rows: list, thesis_tickers: set, resolved: list) -> dict:
    """{ticker: [why it is watched]} — positions first, benchmarks always."""
    out = {}

    def add(t, why):
        t = str(t or "").strip().upper()
        if t:
            out.setdefault(t, [])
            if why not in out[t]:
                out[t].append(why)

    for r in positions_rows:
        add(r.get("ticker"), "position")
    for t in sorted(thesis_tickers):
        add(t, "thesis")
    for r in resolved:
        if r.get("kind") == "derived_event":
            add(r.get("ticker"), "cluster buy")
    for b in BENCHMARKS:
        add(b, "benchmark")
    return out


def read_rows(path: str) -> list:
    """csv module, not pandas: pandas turns the ticker NA into NaN."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def main() -> None:
    now = io.now_utc()
    files = io.day_files(OBS_DIR, ".jsonl")[-7:]
    recent = {d: io.read_jsonl(p) for d, p in files}
    meta_obs = io.read_json(os.path.join(OBS_DIR, f"{files[-1][0]}.meta.json")) \
        if files else None
    resolved = io.resolved_in_run(recent, (meta_obs or {}).get("generated_at", ""))
    want = wanted(read_rows(POSITIONS), io.thesis_tickers(LEDGER), resolved)
    names = sorted(want, key=lambda t: (
        0 if "position" in want[t] else 1 if "thesis" in want[t]
        else 3 if "benchmark" in want[t] else 2, t))
    capped = len(names) > MAX_NAMES
    names = names[:MAX_NAMES] if capped else names
    for b in BENCHMARKS:
        if b not in names:
            names.append(b)
    print(f"[prices] {len(names)} name(s): "
          + ", ".join(f"{t} ({'/'.join(want[t])})" for t in names))

    import yfinance as yf
    raw = yf.download(names, start=START, end=None, auto_adjust=True,
                      progress=False, threads=True, group_by="column")
    close = raw["Close"] if raw is not None and len(raw) else None
    rows, missing = [], {}
    for t in names:
        if close is None or t not in getattr(close, "columns", []):
            missing[t] = "the price source returned nothing for this ticker"
            continue
        s = close[t].dropna()
        if s.empty:
            missing[t] = "the price source returned an empty series"
            continue
        rows.extend((d.date().isoformat(), t, f"{v:.6f}") for d, v in s.items())

    if not rows:
        print("::error::prices: nothing came back for any name — refusing to "
              "write a file that would read as 'no risk'")
        sys.exit(5)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "latest.csv"), "w", encoding="utf-8",
              newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date", "ticker", "close"])
        w.writerows(rows)
    last = max(r[0] for r in rows)
    io.write_json(os.path.join(OUT_DIR, "latest.meta.json"), {
        "generated_at": now, "last_session": last, "source": "yfinance",
        "start": START, "names": {t: want[t] for t in names},
        "missing": missing, "capped": capped, "max_names": MAX_NAMES})
    print(f"[prices] wrote {len(rows)} closes through {last}; missing "
          f"{len(missing)}{': ' + ', '.join(missing) if missing else ''}")
    print("[prices] collected only. Nothing computed, K unchanged.")


if __name__ == "__main__":
    main()
