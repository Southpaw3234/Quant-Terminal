"""Measurement — v28 Phase 1.

**The only role permitted to compute a return.** (`docs/V28_AGENT_ARCHITECTURE.md`
§3.2.) Data never sees returns; Research proposes but cannot measure; Referee
authorises but does not compute. This module is where a number becomes evidence.

WHAT IS PORTED HERE
-------------------
`summarize()` is a faithful port of `event_study.py`'s summary, extracted so it
can be tested against the frozen E1 read without a network fetch. The arithmetic
is deliberately identical, down to `ddof=1` and the `max(1, n // 3)` third —
Phase 1's exit gate is that this reproduces `+1.7647%, t 1.16, stability −0.52`
EXACTLY from the frozen ledger, and "exactly" is not a figure of speech.

⚠️ **A REBUILD THAT CHANGES A VERDICT SILENTLY IS THE FAILURE MODE THIS GATE
EXISTS FOR.** v25 spent months discovering that its analyzer rewrote history on
every run. The equivalent error here is subtler: a rebuilt statistic that reads
0.03 higher, on a bar of 2.0, on a series nobody re-derives by hand. If the
reproduction test fails, the rebuild is wrong even when the new number looks
more defensible.

WHAT IS *NOT* HERE
------------------
Price fetching, entry selection, settlement and the independence filter stay in
`event_study.py` for now. They are already covered by its synthetic suite —
look-ahead, unsettled exits, overlap dedup — and moving them is Phase 2 work.
So the pair covers the whole path: `event_study.py` proves the returns are
computed honestly; this proves the verdict is derived honestly from them.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# E1 bars, from docs/V27_PREREGISTRATION.md. THE DOCUMENT IS AUTHORITATIVE --
# if these ever disagree with it, the document wins and this is the bug.
E1_MIN_N = 40
E1_MIN_EFFECT = 0.005      # +0.50% mean abnormal return per event
E1_MIN_T = 2.0
E1_STABILITY = 0.50        # final third >= 50% of first third


@dataclass
class Read:
    """The result of a measurement. A verdict, not a decision."""

    label: str
    n: int
    mean: float
    sd: float
    t: float
    first_third: float
    last_third: float
    stability: float
    passes: bool

    def failures(self) -> list:
        """Which bars were missed. Empty means the point estimate cleared."""
        out = []
        if self.n < E1_MIN_N:
            out.append(f"N {self.n} < {E1_MIN_N}")
        if not (self.mean >= E1_MIN_EFFECT):
            out.append(f"mean {self.mean:+.4%} < {E1_MIN_EFFECT:+.2%}")
        if not (np.isfinite(self.t) and self.t >= E1_MIN_T):
            out.append(f"t {self.t:+.2f} < {E1_MIN_T}")
        if not (np.isfinite(self.stability) and self.stability >= E1_STABILITY):
            out.append(f"stability {self.stability:.2f} < {E1_STABILITY:.2f}")
        return out

    def summary(self) -> str:
        st = "n/a" if not np.isfinite(self.stability) else f"{self.stability:.2f}"
        return (f"[{self.label}] n={self.n} mean={self.mean:+.4%} "
                f"t={self.t:+.2f} stability={st} "
                f"(first {self.first_third:+.4%} -> last {self.last_third:+.4%}) "
                f"-> {'PASS' if self.passes else 'NOT MET'}")


def summarize(df: pd.DataFrame, label: str = "ALL",
              value_col: str = "abnormal_ret",
              order_col: str = "event_ts") -> Read:
    """E1 statistics over a scored event ledger.

    Stability is the final third against the first third, ordered by event
    time. It is undefined (NaN, and therefore a MISS) when the first third is
    not positive — a "ratio" against a negative baseline is not a decay measure,
    it is a sign artifact that can read as a pass.

    v25's S3 produced +0.0254 over folds 1-6 that decayed to -0.0053 by folds
    9-12 and still read as a near-miss. This clause is that lesson.
    """
    x = pd.to_numeric(df[value_col], errors="coerce").dropna().values
    n = int(len(x))
    if n < 2:
        return Read(label, n, float("nan"), float("nan"), float("nan"),
                    float("nan"), float("nan"), float("nan"), False)

    mean = float(np.mean(x))
    sd = float(np.std(x, ddof=1))
    t = mean / (sd / np.sqrt(n)) if sd > 0 else float("nan")

    ordered = df.sort_values(order_col)
    xs = pd.to_numeric(ordered[value_col], errors="coerce").dropna().values
    third = max(1, len(xs) // 3)
    first_m = float(np.mean(xs[:third]))
    last_m = float(np.mean(xs[-third:]))
    stability = (last_m / first_m) if first_m > 0 else float("nan")

    passes = bool(n >= E1_MIN_N and mean >= E1_MIN_EFFECT
                  and np.isfinite(t) and t >= E1_MIN_T
                  and np.isfinite(stability) and stability >= E1_STABILITY)

    return Read(label, n, mean, sd, t, first_m, last_m, stability, passes)


def survivorship_sensitivity(df: pd.DataFrame,
                             status_col: str = "exit_status",
                             value_col: str = "abnormal_ret") -> dict:
    """How much of the result depends on how delistings were handled?

    🔑 **A long-horizon result on a survivor-biased universe is an upper bound,
    not an estimate**, and the only honest way to quote one is beside the number
    that says how far it moves when the delisted names are removed.

    Reports the read three ways:
      all         every settled observation, delistings included at their
                  last traded price
      survivors   delistings EXCLUDED — i.e. what the engine used to report
                  by accident, before forward_return_ex existed
      delisted    the delisted subset alone

    ⚠️ If `survivors` is much better than `all`, the strategy's apparent edge is
    partly an artifact of names that stopped trading, and the TRUE figure is
    worse than `all` — because the universe is missing the delistings that never
    made it into the symbol source at all. `all` is the ceiling of an estimate
    whose floor is unknown.
    """
    if status_col not in df.columns:
        return {"available": False,
                "note": f"no {status_col} column — engine predates "
                        f"delisting classification; ALL results are "
                        f"survivor-only and silently so"}
    delisted_mask = df[status_col].astype(str) == "delisted"
    out = {
        "available": True,
        "n_all": int(len(df)),
        "n_delisted": int(delisted_mask.sum()),
        "all": summarize(df, "all"),
        "survivors": summarize(df[~delisted_mask], "survivors"),
    }
    out["delisted_pct"] = (out["n_delisted"] / out["n_all"]) if out["n_all"] else 0.0
    if out["n_delisted"] >= 2:
        out["delisted"] = summarize(df[delisted_mask], "delisted")
    return out


def format_sensitivity(s: dict) -> str:
    if not s.get("available"):
        return f"[survivorship] {s.get('note', 'unavailable')}"
    lines = [f"[survivorship] {s['n_delisted']}/{s['n_all']} "
             f"({s['delisted_pct']:.1%}) of settled events DELISTED mid-hold"]
    for key in ("all", "survivors", "delisted"):
        r = s.get(key)
        if r is not None:
            lines.append(f"  {key:<10} {r.summary()}")
    a, sv = s.get("all"), s.get("survivors")
    if a is not None and sv is not None and np.isfinite(a.mean) and np.isfinite(sv.mean):
        gap = sv.mean - a.mean
        lines.append(f"  ⚠️  excluding delistings moves the mean {gap:+.4%}. "
                     f"The universe is ALSO missing names that delisted before "
                     f"the symbol source was read, so 'all' is a CEILING.")
    return "\n".join(lines)


def read_ledger(path) -> pd.DataFrame:
    """Load a scored event ledger. Raises rather than returning empty.

    An empty DataFrame from a missing file would flow downstream and summarise
    as "n=0, NOT MET", which is indistinguishable from a genuine null read.
    Those two must never look alike.
    """
    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"{path} is empty — refusing to summarise nothing")
    return df
