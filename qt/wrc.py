"""Multiple-testing correction — White's Reality Check and Hansen's SPA.

The E1 criterion in `docs/V27_PREREGISTRATION.md` requires a specification to
clear **WRC and SPA at p < 0.10 against the declared K = 5**. Until this module
existed that clause was printed as a NOTE at the bottom of every read and never
evaluated — a pass could not have been called on the day it happened.

WHAT THE QUESTION ACTUALLY IS
-----------------------------
You are allowed five looks. Pick the best of five null strategies and its naive
t-statistic will look significant far more often than 5% of the time — that is
not a subtle effect, and `validate_qt_wrc.py` demonstrates it directly. WRC and
SPA answer: *given that K things were tried, what is the probability the best
one's performance arose by chance?*

  WRC  (White 2000)   statistic = max_k sqrt(n) * mean_k.  Bootstrap the time
                      index, recenter every series at its own sample mean (the
                      null), take max over k each time; p = P(max* >= observed).
  SPA  (Hansen 2005)  studentized, and does not let terrible alternatives inflate
                      the null: a series whose mean is far below zero is
                      recentred at its mean rather than at zero, so it cannot
                      manufacture a large positive bootstrap statistic. Strictly
                      more powerful than WRC; both are reported.

THE PROBLEM WITH EVENT STUDIES, STATED RATHER THAN HIDDEN
---------------------------------------------------------
Both tests assume K series on ONE shared time index, resampled jointly so their
dependence is preserved. Event studies do not give you that: spec #1's 160
events and spec #2's ~390 fall on different names and different dates. There is
no shared index to bootstrap.

So there are two layers here and the distinction is load-bearing:

  * `wrc_pvalue` / `spa_pvalue` — the CORRECT joint tests, for aligned series.
    Used as-is whenever aligned series exist (e.g. daily strategy returns).
  * `event_study_correction` — the HONEST adapter for unaligned event sets.
    Each read spec gets its own bootstrap p (a single-series WRC is a one-sided
    bootstrap test of mean > 0). Then the declared-K correction is applied as a
    Bonferroni-type bound: a spec passes only if `p_k <= alpha / K_declared`,
    and unread budget slots COUNT in the divisor.

  ⚠️ That last point is the one that matters. With K_declared = 5 and one spec
  read, the bar is p <= 0.02, not p <= 0.10. Correcting only for the specs
  actually tested would let the budget be spent one read at a time with each
  judged as if it were the only one — which is exactly the data-snooping
  the clause exists to prevent. The unread slots are not free.

WHY BONFERRONI AND NOT SOMETHING CLEVERER
-----------------------------------------
Because the series are unaligned, the joint distribution of the max is not
estimable and Bonferroni is the bound that holds without it. It is
conservative. Conservative is the right direction for a project whose four
previous reads all failed and whose universe carries an unquantified upward
bias. If the day comes when a spec clears p <= 0.02 raw, that is a result that
survived the harshest honest treatment available, and it will mean something.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

DEFAULT_B = 2000          # bootstrap replications
DEFAULT_BLOCK = 5.0       # mean block length for the stationary bootstrap


# ───────────────────────────────────────────────────────────── bootstrap

def stationary_bootstrap_indices(n: int, B: int, mean_block: float = DEFAULT_BLOCK,
                                 rng=None) -> np.ndarray:
    """Politis–Romano (1994) stationary bootstrap. Returns a (B, n) index array.

    Blocks have geometric length with mean `mean_block`, wrapping circularly.
    Preserves short-range dependence, which an iid resample would destroy.
    `mean_block=1` degenerates to the iid bootstrap.
    """
    rng = np.random.default_rng(rng)
    n = int(n)
    if n <= 0:
        raise ValueError("n must be positive")
    if mean_block <= 1.0:
        # iid bootstrap: every position is an independent uniform draw. Same
        # distribution as the block loop with p=1, without the Python inner
        # loop -- the calibration test in validate_qt_wrc.py runs hundreds of
        # trials and would otherwise take minutes.
        return rng.integers(0, n, size=(B, n), dtype=np.int64)
    p = 1.0 / float(mean_block)
    out = np.empty((B, n), dtype=np.int64)
    for b in range(B):
        idx = np.empty(n, dtype=np.int64)
        idx[0] = rng.integers(0, n)
        starts = rng.random(n) < p
        for t in range(1, n):
            idx[t] = rng.integers(0, n) if starts[t] else (idx[t - 1] + 1) % n
        out[b] = idx
    return out


# ────────────────────────────────────────────────────────── joint tests

@dataclass
class MTResult:
    """Result of a multiple-testing correction. A verdict input, not a decision."""

    method: str
    k_tested: int
    k_declared: int
    n: int
    statistic: float
    p_value: float
    alpha: float
    adjusted_alpha: float
    passes: bool
    per_series_mean: list = field(default_factory=list)
    per_series_p: list = field(default_factory=list)
    note: str = ""

    def summary(self) -> str:
        return (f"[{self.method}] K_tested={self.k_tested} K_declared={self.k_declared} "
                f"n={self.n} stat={self.statistic:+.3f} p={self.p_value:.4f} "
                f"(bar p<{self.adjusted_alpha:.4f}) -> "
                f"{'PASS' if self.passes else 'NOT MET'}"
                + (f"  {self.note}" if self.note else ""))


def _as_matrix(series) -> np.ndarray:
    d = np.asarray(series, dtype=float)
    if d.ndim == 1:
        d = d[:, None]
    if d.ndim != 2 or d.shape[0] < 2:
        raise ValueError("need an (n, K) matrix with n >= 2")
    if np.isnan(d).any():
        raise ValueError("series contain NaN — drop or align before testing")
    return d


def wrc_pvalue(series, B: int = DEFAULT_B, mean_block: float = DEFAULT_BLOCK,
               rng=None) -> tuple:
    """White's Reality Check on an (n, K) matrix of relative performance.

    Returns (p_value, observed_statistic, per_series_means).
    H0: every series has mean <= 0. Statistic: max_k sqrt(n) * mean_k.
    """
    d = _as_matrix(series)
    n, K = d.shape
    fbar = d.mean(axis=0)
    V = float(np.sqrt(n) * fbar.max())
    idx = stationary_bootstrap_indices(n, B, mean_block, rng)
    # (B, K): bootstrap means, recentred at the sample mean = the null.
    fstar = d[idx].mean(axis=1)                      # (B, K)
    Vstar = np.sqrt(n) * (fstar - fbar).max(axis=1)  # (B,)
    p = float(np.mean(Vstar >= V))
    return p, V, fbar.tolist()


def spa_pvalue(series, B: int = DEFAULT_B, mean_block: float = DEFAULT_BLOCK,
               rng=None) -> tuple:
    """Hansen's (2005) SPA test, 'consistent' p-value, on an (n, K) matrix.

    Returns (p_value, observed_statistic, per_series_means).
    Studentizes each series and recentres poorly-performing ones at their own
    mean so they cannot inflate the null distribution of the max.
    """
    d = _as_matrix(series)
    n, K = d.shape
    fbar = d.mean(axis=0)
    idx = stationary_bootstrap_indices(n, B, mean_block, rng)
    fstar = d[idx].mean(axis=1)                          # (B, K)
    # omega_k: bootstrap sd of sqrt(n) * fbar*_k
    omega = np.sqrt(n) * fstar.std(axis=0, ddof=1)
    omega = np.where(omega <= 1e-12, 1e-12, omega)
    T = float(max(0.0, (np.sqrt(n) * fbar / omega).max()))
    # Recentring: series far below zero stay centred at their mean.
    threshold = -omega * np.sqrt(2.0 * np.log(np.log(max(n, 3)))) / np.sqrt(n)
    mu_c = np.where(fbar <= threshold, fbar, 0.0)
    Z = np.sqrt(n) * (fstar - fbar + mu_c) / omega       # (B, K)
    Tstar = np.maximum(Z.max(axis=1), 0.0)
    p = float(np.mean(Tstar >= T))
    return p, T, fbar.tolist()


def joint_correction(series, k_declared: int, alpha: float = 0.10,
                     B: int = DEFAULT_B, mean_block: float = DEFAULT_BLOCK,
                     rng=None) -> dict:
    """Both joint tests on ALIGNED series, against the declared K.

    If fewer series are supplied than K_declared, the unread slots still count:
    the joint p is compared against alpha * (K_tested / K_declared), which is
    the Bonferroni-consistent way to charge for looks not yet taken.
    """
    d = _as_matrix(series)
    n, K = d.shape
    if K > k_declared:
        raise ValueError(f"{K} series exceeds the declared K={k_declared} — "
                         f"testing beyond K invalidates E1 for every spec")
    adj = alpha * (K / float(k_declared))
    out = {}
    for name, fn in (("WRC", wrc_pvalue), ("SPA", spa_pvalue)):
        p, stat, means = fn(d, B, mean_block, rng)
        out[name] = MTResult(name, K, k_declared, n, stat, p, alpha, adj,
                             passes=bool(p < adj), per_series_mean=means)
    return out


# ────────────────────────────────────────── event-study (unaligned) adapter

def bootstrap_mean_p(x, B: int = DEFAULT_B, mean_block: float = 1.0,
                     rng=None) -> tuple:
    """One-sided bootstrap p for H0: mean <= 0 on a single series.

    This IS the K=1 Reality Check. Default block length 1 (iid) because
    event-level abnormal returns ordered by event date carry little serial
    dependence once overlapping windows have been removed upstream; pass a
    larger block if the caller knows otherwise.
    """
    p, stat, _ = wrc_pvalue(np.asarray(x, dtype=float)[:, None], B, mean_block, rng)
    return p, stat


def event_study_correction(series_by_spec: dict, k_declared: int,
                           alpha: float = 0.10, B: int = DEFAULT_B,
                           rng=None, k_tested=None) -> dict:
    """Declared-K correction for UNALIGNED event-study specifications.

    `series_by_spec` maps spec_id -> 1-D array of per-event abnormal returns.
    Each gets a bootstrap p; each passes only if p <= alpha / K_declared.

    ⚠️ Unread budget slots count in the divisor. With K_declared=5 and one
    spec read, the bar is 0.02. That is deliberate — see the module docstring.

    `k_tested` is how many specifications have been READ including the ones
    supplied here. It defaults to `len(series_by_spec)`, which is only right
    when every read spec's series is passed in. The E1 job passes just the
    spec being read while earlier specs sit frozen in their own ledgers, so it
    supplies the Referee's count instead. **The bar does not depend on it** —
    it is alpha / K_declared regardless — but the printed "N unread slot(s)
    charged" does, and a label that is off by one is the kind of slightly-wrong
    signal this project keeps having to catch.
    """
    n_supplied = len(series_by_spec)
    if n_supplied == 0:
        raise ValueError("no specifications supplied")
    k_tested = n_supplied if k_tested is None else int(k_tested)
    if k_tested < n_supplied:
        raise ValueError(f"k_tested={k_tested} is fewer than the {n_supplied} "
                         f"series supplied — it counts read specs, not a subset")
    if k_tested > k_declared:
        raise ValueError(f"{k_tested} specs exceeds the declared K={k_declared}")
    adj = alpha / float(k_declared)
    results = {}
    rng = np.random.default_rng(rng)
    for spec_id, x in series_by_spec.items():
        x = np.asarray(x, dtype=float)
        x = x[~np.isnan(x)]
        if len(x) < 2:
            results[spec_id] = MTResult("EVENT-BONF", k_tested, k_declared,
                                        len(x), float("nan"), float("nan"),
                                        alpha, adj, False,
                                        note="too few observations")
            continue
        p, stat = bootstrap_mean_p(x, B, 1.0, rng)
        results[spec_id] = MTResult(
            "EVENT-BONF", k_tested, k_declared, len(x), stat, p, alpha, adj,
            passes=bool(p <= adj),
            per_series_mean=[float(x.mean())], per_series_p=[p],
            note=(f"raw p={p:.4f}; K_declared bar={adj:.4f}; "
                  f"{k_declared - k_tested} unread slot(s) charged"))
    return results
