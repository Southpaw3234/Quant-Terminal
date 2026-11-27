"""Where Bitcoin sits relative to its halving cycle. Pure logic, no network.

A DESCRIPTION OF A HYPOTHESIS, NOT A MODEL. Every ~210,000 blocks (about
four years) the reward for mining a Bitcoin block halves. Bitcoin's large
past run-ups and crashes have so far fallen at similar distances after each
halving, which is why people talk about a "four-year cycle". It has repeated
a handful of times, it has never been tested here, and nothing guarantees it
repeats. This module only measures: how far into the current cycle we are,
where price is against the halving and the cycle's high, and where the past
cycles in the price data stood at the same distance.

Inputs are daily closes in USD (pandas Series, DatetimeIndex). Output is a
JSON-ready dict. Nothing here says buy, sell or hold.
"""

from __future__ import annotations

import math

import pandas as pd

# Block-height facts, not estimates: blocks 210,000 / 420,000 / 630,000 /
# 840,000. Dates are UTC.
HALVINGS = ("2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20")
CYCLE_DAYS_EST = 1461           # ~4 years; the next date is an ESTIMATE
WINDOW_PAD = 60                 # days either side of a past peak/bottom date
# The cycle PEAK is the highest close in the first two years after a
# halving. The whole halving-to-halving maximum is the wrong thing: for the
# 2020 cycle it is March 2024's run-up into the NEXT halving, not the
# November 2021 top. This rule was set after seeing that, and is stated
# here rather than hidden; validate_btc_cycle.py pins it.
PEAK_SEARCH_DAYS = 730


def _ts(d):
    return pd.Timestamp(d).tz_localize(None).normalize()


def next_halving_estimate() -> str:
    return (_ts(HALVINGS[-1]) + pd.Timedelta(days=CYCLE_DAYS_EST)).date().isoformat()


def cycle_stats(px: pd.Series, start: str, end) -> dict:
    """One cycle, halving `start` to `end` (next halving or the data's end).

    `complete_data` is False when the price data begins after the halving:
    the cycle's start (and possibly its peak) are then outside what we have,
    and its figures are not comparable, so the caller reports it as partial.
    """
    s0, e0 = _ts(start), _ts(end)
    seg = px[(px.index >= s0) & (px.index < e0)].dropna()
    out = {"halving": s0.date().isoformat(), "ends": e0.date().isoformat(),
           "complete_data": bool(len(px) and px.index[0] <= s0 + pd.Timedelta(days=3)),
           "n_days": int(len(seg))}
    if seg.empty:
        return dict(out, status="no_data")
    start_px = float(seg.iloc[0])
    early = seg[seg.index < s0 + pd.Timedelta(days=PEAK_SEARCH_DAYS)]
    pk_d = early.idxmax()
    pk = float(seg.loc[pk_d])
    after = seg[seg.index >= pk_d]
    bt_d = after.idxmin()
    bt = float(after.loc[bt_d])
    return dict(out, status="ok",
                price_at_halving=round(start_px, 2),
                peak=round(pk, 2), peak_date=pk_d.date().isoformat(),
                peak_days_after=int((pk_d - s0).days),
                peak_multiple=round(pk / start_px, 2),
                low_after_peak=round(bt, 2), low_date=bt_d.date().isoformat(),
                low_days_after=int((bt_d - s0).days),
                drop_peak_to_low=round(bt / pk - 1, 4))


def overlay(px: pd.Series, start: str, end, step: int = 7) -> list:
    """[[days_after_halving, price / price_at_halving], ...] every `step` days."""
    s0, e0 = _ts(start), _ts(end)
    seg = px[(px.index >= s0) & (px.index < e0)].dropna()
    if seg.empty:
        return []
    base = float(seg.iloc[0])
    pts = [[int((d - s0).days), round(float(v) / base, 4)]
           for i, (d, v) in enumerate(seg.items()) if i % step == 0]
    last = seg.index[-1]
    if pts[-1][0] != int((last - s0).days):
        pts.append([int((last - s0).days), round(float(seg.iloc[-1]) / base, 4)])
    return pts


def phase(days: int, past: list) -> dict:
    """Which stretch of past cycles today's distance from the halving matches.

    Uses ONLY completed cycles whose data covers them. Labels say what
    happened in the past at this distance, never what will happen.
    """
    full = [c for c in past if c.get("status") == "ok" and c.get("complete_data")]
    if not full:
        return {"label": "unknown", "text": "No complete past cycle in the price data."}
    pk = [c["peak_days_after"] for c in full]
    lo = [c["low_days_after"] for c in full]
    pk_lo, pk_hi = min(pk) - WINDOW_PAD, max(pk) + WINDOW_PAD
    lo_lo, lo_hi = min(lo) - WINDOW_PAD, max(lo) + WINDOW_PAD
    if days < pk_lo:
        lab, txt = "rising", "before the point where past cycles peaked"
    elif days <= pk_hi:
        lab, txt = "peak window", "inside the stretch where past cycles peaked"
    elif days < lo_lo:
        lab, txt = "falling", "between past peaks and past lows, where earlier cycles were falling"
    elif days <= lo_hi:
        lab, txt = "low window", "inside the stretch where past cycles hit their lows"
    else:
        lab, txt = "recovering", "after past lows, where earlier cycles were recovering toward the next halving"
    return {"label": lab, "text": txt,
            "past_peak_days": pk, "past_low_days": lo,
            "peak_window": [pk_lo, pk_hi], "low_window": [lo_lo, lo_hi]}


def describe(px: pd.Series, today=None) -> dict:
    """The whole picture for the dashboard."""
    px = pd.Series(px, dtype="float64").dropna()
    px.index = pd.DatetimeIndex(px.index).tz_localize(None).normalize()
    px = px[px > 0].sort_index()
    if px.empty:
        return {"status": "no_data"}
    last_d = px.index[-1]
    bounds = list(HALVINGS) + [next_halving_estimate()]
    cycles = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        end = min(_ts(b), last_d + pd.Timedelta(days=1))
        if _ts(a) > last_d:
            break
        c = cycle_stats(px, a, end)
        c["current"] = _ts(b) > last_d
        c["overlay"] = overlay(px, a, end) if c.get("complete_data") else []
        cycles.append(c)
    cur = cycles[-1]
    past = [c for c in cycles[:-1]]
    h0 = _ts(HALVINGS[-1])
    days = int((last_d - h0).days)
    price = float(px.iloc[-1])
    seg = px[(px.index >= h0) & (px.index < h0 + pd.Timedelta(days=PEAK_SEARCH_DAYS))]
    hi_d = seg.idxmax()
    same_stage = []
    for c in past:
        if not (c.get("status") == "ok" and c.get("complete_data")):
            continue
        s0 = _ts(c["halving"])
        seg_p = px[(px.index >= s0) & (px.index <= s0 + pd.Timedelta(days=days))]
        if seg_p.empty:
            continue
        same_stage.append({
            "halving": c["halving"],
            "multiple_vs_halving": round(float(seg_p.iloc[-1]) / float(seg_p.iloc[0]), 2),
            "off_cycle_high": round(float(seg_p.iloc[-1]) / float(
                seg_p[seg_p.index < s0 + pd.Timedelta(days=PEAK_SEARCH_DAYS)].max()) - 1, 4)})
    total = (_ts(next_halving_estimate()) - h0).days
    return {
        "status": "ok", "last_session": last_d.date().isoformat(),
        "data_from": px.index[0].date().isoformat(),
        "price": round(price, 2),
        "halvings": list(HALVINGS), "next_halving_estimate": next_halving_estimate(),
        "days_since_halving": days, "cycle_length_days_est": total,
        "share_of_cycle": round(days / total, 4),
        "price_at_halving": cur.get("price_at_halving"),
        "multiple_vs_halving": round(price / cur["price_at_halving"], 2)
        if cur.get("price_at_halving") else None,
        "cycle_high": round(float(seg.loc[hi_d]), 2),
        "cycle_high_date": hi_d.date().isoformat(),
        "cycle_high_days_after": int((hi_d - h0).days),
        "off_cycle_high": round(price / float(seg.loc[hi_d]) - 1, 4),
        "phase": phase(days, past),
        "same_stage_past": same_stage,
        "cycles": cycles,
        "how_to_read": (
            "A pattern that has repeated a few times, not a law. Past cycles "
            "peaked and bottomed at similar distances after each halving; "
            "that is the whole basis of the 'four-year cycle'. It has never "
            "been tested here, the sample is three or four cycles, and "
            "Bitcoin has fallen 70-85% from past peaks. Nothing here says "
            "which way the price goes next."),
    }
