#!/usr/bin/env python3
"""OBSERVER — Bitcoin's daily close, and where it sits in its halving cycle.

Collects BTC-USD closes from 2014 (the price source's first day) and hands
them to qt/btc_cycle.py, which only measures distances and ratios. Writes
crypto/latest.json, which is published and read by the dashboard.

A description of a popular hypothesis, never a forecast: see the header of
qt/btc_cycle.py. Nothing here touches the K budget or the equity research.
"""

from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from qt import agentio as io
from qt import btc_cycle

OUT_DIR = os.environ.get("QT_CRYPTO_DIR", "crypto")
START = "2014-01-01"


def main() -> None:
    now = io.now_utc()
    import yfinance as yf
    raw = yf.download("BTC-USD", start=START, progress=False, auto_adjust=True)
    close = None
    if raw is not None and len(raw):
        c = raw["Close"]
        close = c["BTC-USD"] if hasattr(c, "columns") else c
    if close is None or close.dropna().empty:
        print("::error::crypto: the price source returned nothing for BTC-USD "
              "— refusing to write a file that would read as current")
        sys.exit(5)
    doc = btc_cycle.describe(close)
    doc.update({"generated_at": now, "source": "yfinance BTC-USD daily close (UTC)",
                "is_not_a_recommendation": True})
    os.makedirs(OUT_DIR, exist_ok=True)
    io.write_json(os.path.join(OUT_DIR, "latest.json"), doc)
    print(f"[crypto] BTC {doc['price']:,.0f} on {doc['last_session']}; day "
          f"{doc['days_since_halving']} of ~{doc['cycle_length_days_est']} since "
          f"the {doc['halvings'][-1]} halving; {doc['off_cycle_high']:+.0%} from "
          f"the cycle high; past-cycle stretch: {doc['phase']['label']}")
    print("[crypto] described only. Nothing tested, K unchanged.")


if __name__ == "__main__":
    main()
