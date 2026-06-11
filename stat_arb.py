#!/usr/bin/env python3
"""
stat_arb.py — Statistical Arbitrage Pairs Scanner (Level 3, Phase 2)
=====================================================================
Standalone script. Run weekly (or call from quant_runner.py on morning cycle).

Phase 2 of Level 3 rebuild plan:
  - Engle-Granger cointegration screen across tickers in data/
  - Kalman filter for time-varying hedge ratio
  - Signal: enter when spread > 2σ, exit at reversion (< 0.5σ)
  - Market-neutral by construction — uncorrelated to directional book

Outputs:
  data/stat_arb/pairs.json          — live cointegrated pairs + hedge ratios
  data/stat_arb/signals.json        — current spread z-scores + entry/exit signals
  data/stat_arb/pair_history.csv    — rolling spread history for monitoring

Usage:
  python stat_arb.py                 # scan + signal (full)
  python stat_arb.py --signal-only   # signal from cached pairs (no cointegration scan)
  python stat_arb.py --scan-only     # cointegration scan only, no trading signals
"""

import os
import sys
import json
import datetime
import argparse
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
STAT_ARB_DIR      = Path("data/stat_arb")
PAIRS_FILE        = STAT_ARB_DIR / "pairs.json"
SIGNALS_FILE      = STAT_ARB_DIR / "signals.json"
HISTORY_FILE      = STAT_ARB_DIR / "pair_history.csv"
PRICE_DATA_DIR    = Path("data")

COINT_P_THRESH    = 0.05     # cointegration p-value threshold
HALF_LIFE_MIN     = 3        # minimum mean-reversion half-life (days)
HALF_LIFE_MAX     = 60       # maximum mean-reversion half-life (days; widened
                             # from 5–40 for the evidence-gathering phase so the
                             # screen isn't over-tight while we accumulate a track)
ENTRY_Z           = 2.0      # enter when |z-score| exceeds this
EXIT_Z            = 0.5      # exit when |z-score| falls below this
LOOKBACK_DAYS     = 252      # days of price history for cointegration test
SCAN_TOP_N_PAIRS  = 50       # max pairs to store
KALMAN_Q          = 1e-5     # Kalman process noise (hedge ratio drift rate)
KALMAN_R          = 1e-3     # Kalman observation noise

# ── Universe ─────────────────────────────────────────────────────────────────
# Focus on tickers where pairs trading makes economic sense (same sector).
PAIR_UNIVERSE = {
    "Semiconductors": ["NVDA","AMD","INTC","QCOM","AMAT","MU","TXN","LRCX","KLAC","ADI"],
    "Mega-Cap Tech":  ["AAPL","MSFT","GOOGL","META","AMZN"],
    "Financials":     ["JPM","BAC","GS","MS","WFC","C","BLK","SCHW"],
    "Energy":         ["XOM","CVX","COP","EOG","SLB"],
    "Airlines":       ["DAL","UAL","AAL","LUV"],
    "Retailers":      ["WMT","TGT","COST","HD","LOW"],
    "Pharma":         ["JNJ","PFE","MRK","ABBV","BMY","GILD","AMGN"],
    "EV":             ["TSLA","GM","F","RIVN"],
    "Cloud":          ["CRM","NOW","SNOW","DDOG","ZS","CRWD","NET"],
    "Consumer":       ["KO","PEP","MCD","SBUX","CMG","YUM"],
}

STAT_ARB_DIR.mkdir(parents=True, exist_ok=True)


# ── Kalman filter for time-varying hedge ratio ─────────────────────────────────
def kalman_hedge_ratio(y: np.ndarray, x: np.ndarray,
                       Q: float = KALMAN_Q, R: float = KALMAN_R):
    """
    Estimate time-varying hedge ratio β via Kalman filter.
    Model: y_t = β_t × x_t + ε_t,  β_t = β_{t-1} + w_t
    Returns: betas (array), spread (array)
    """
    n = len(y)
    beta    = np.zeros(n)
    P       = np.ones(n)   # prediction error covariance
    beta[0] = y[0] / (x[0] if x[0] != 0 else 1.0)
    P[0]    = 1.0

    for t in range(1, n):
        # Predict
        beta_pred = beta[t - 1]
        P_pred    = P[t - 1] + Q
        # Update
        xt    = x[t]
        innov = y[t] - beta_pred * xt
        S     = P_pred * xt ** 2 + R
        K     = P_pred * xt / S
        beta[t] = beta_pred + K * innov
        P[t]    = (1 - K * xt) * P_pred

    spread = y - beta * x
    return beta, spread


# ── Half-life of mean reversion (Ornstein-Uhlenbeck fit) ──────────────────────
def half_life(spread: np.ndarray) -> float:
    """
    Estimate OU half-life via OLS: Δspread_t = λ × spread_{t-1} + ε
    half_life = -ln(2) / λ
    """
    try:
        # OU half-life via Δs_t = a + λ·s_{t-1}. Demean s_{t-1} so the slope λ is
        # not biased by a non-zero spread mean (the no-intercept version was).
        s_lag  = spread[:-1]
        s_diff = np.diff(spread)
        s_lag_dm = s_lag - s_lag.mean()
        lam = float(np.dot(s_lag_dm, s_diff - s_diff.mean())
                    / max(np.dot(s_lag_dm, s_lag_dm), 1e-10))
        if lam >= 0:
            return np.inf
        return float(-np.log(2) / lam)
    except Exception:
        return np.inf


# ── Engle-Granger cointegration test ──────────────────────────────────────────
def static_ols_spread(y: np.ndarray, x: np.ndarray) -> tuple[float, np.ndarray]:
    """
    Static OLS hedge ratio + spread over the whole window: y = a + b·x.
    This is the spread the cointegration test (statsmodels `coint`) actually
    builds internally, so the stationarity/half-life/z-score screen MUST use it
    too. (The Kalman spread re-estimates β every step to null the innovation, so
    its residual is near-white-noise with a sub-day half-life — using it for the
    half-life screen drops cointegrated pairs on an artifact. Kalman β is kept
    only as the live, time-varying *trading* hedge ratio.)
    Returns: (hedge_ratio b, spread = y - (a + b·x)).
    """
    A = np.vstack([np.ones_like(x), x]).T
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    a, b = float(coef[0]), float(coef[1])
    spread = y - (a + b * x)
    return b, spread


def engle_granger_pvalue(y: np.ndarray, x: np.ndarray) -> float:
    """
    Engle-Granger two-step cointegration test p-value (low = cointegrated).

    BUGFIX (2026-06-06): the previous implementation regressed y on x WITHOUT an
    intercept (beta = x·y / x·x) on log-price *levels*. Log prices have large,
    non-zero means, so a no-intercept fit leaves a trending/biased residual and
    the ADF step almost never rejects → 0/172 pairs cointegrated every run. Use
    statsmodels' `coint` (proper Engle-Granger WITH a constant); fall back to an
    intercept OLS + ADF, then to a correlation proxy only if statsmodels is absent.
    """
    try:
        from statsmodels.tsa.stattools import coint
        # coint regresses y on [const, x] then ADF-tests the residual. trend="c"
        # includes the intercept the old code was missing.
        _t_stat, p_value, _crit = coint(y, x, trend="c", maxlag=5, autolag="AIC")
        return float(p_value)
    except ImportError:
        try:
            from statsmodels.tsa.stattools import adfuller
            # Intercept OLS: y = a + b·x  → residual is properly mean-zero.
            _A = np.vstack([np.ones_like(x), x]).T
            _coef, *_ = np.linalg.lstsq(_A, y, rcond=None)
            resid = y - (_coef[0] + _coef[1] * x)
            return float(adfuller(resid, maxlag=5, autolag="AIC", regression="c")[1])
        except Exception:
            corr = float(np.corrcoef(y, x)[0, 1])
            return 1.0 - abs(corr) ** 2   # proxy (not statistically rigorous)
    except Exception:
        return 1.0


# ── Load price data ───────────────────────────────────────────────────────────
def load_prices(tickers: list[str], lookback_days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """
    Load closing prices via yfinance for the given ticker list.
    Falls back to any cached CSVs in data/paper_trades if yfinance fails.
    """
    try:
        import yfinance as yf
        raw = yf.download(tickers, period=f"{lookback_days + 20}d",
                          auto_adjust=True, progress=False, threads=True)
        if "Close" in raw.columns or isinstance(raw.columns, pd.MultiIndex):
            prices = raw["Close"] if "Close" in raw else raw.xs("Close", axis=1, level=0)
        else:
            prices = raw
        return prices.dropna(how="all").tail(lookback_days)
    except Exception as e:
        print(f"  [stat_arb] Price load error: {e}")
        return pd.DataFrame()


# ── Cointegration scanner ─────────────────────────────────────────────────────
def scan_pairs(prices: pd.DataFrame) -> list[dict]:
    """
    Screen all within-sector pairs for cointegration.
    Returns list of pair dicts sorted by half-life.
    """
    valid_pairs = []
    tickers     = list(prices.columns)

    # Build within-sector pair list
    candidate_pairs = []
    for sector, members in PAIR_UNIVERSE.items():
        universe_tickers = [t for t in members if t in tickers]
        for i in range(len(universe_tickers)):
            for j in range(i + 1, len(universe_tickers)):
                candidate_pairs.append((universe_tickers[i], universe_tickers[j], sector))

    print(f"  [stat_arb] Testing {len(candidate_pairs)} candidate pairs...")
    tested        = 0   # passed length check (cointegration test actually run)
    passed_coint  = 0   # p < COINT_P_THRESH
    passed_hl     = 0   # also within the half-life band → stored

    for tk_y, tk_x, sector in candidate_pairs:
        try:
            y_raw = prices[tk_y].dropna().values.astype(float)
            x_raw = prices[tk_x].dropna().values.astype(float)
            # Align lengths
            n = min(len(y_raw), len(x_raw))
            if n < 60:
                continue
            y = np.log(y_raw[-n:])
            x = np.log(x_raw[-n:])

            # Cointegration test
            p = engle_granger_pvalue(y, x)
            tested += 1

            if p > COINT_P_THRESH:
                continue
            passed_coint += 1

            # Half-life / z-score screen on the STATIC OLS spread (the residual the
            # coint test itself uses). Kalman β is computed separately as the live
            # time-varying hedge ratio to store — NOT for the half-life screen.
            _ols_b, spread = static_ols_spread(y, x)
            hl = half_life(spread)

            if not (HALF_LIFE_MIN <= hl <= HALF_LIFE_MAX):
                continue
            passed_hl += 1

            betas, _kalman_spread = kalman_hedge_ratio(y, x)

            # Spread z-score (on the static spread, consistent with the screen)
            spread_mean = float(spread.mean())
            spread_std  = float(spread.std())
            z_score     = float((spread[-1] - spread_mean) / max(spread_std, 1e-8))
            hedge_ratio = float(betas[-1])

            valid_pairs.append({
                "y":           tk_y,
                "x":           tk_x,
                "sector":      sector,
                "coint_pval":  round(p, 6),
                "hedge_ratio": round(hedge_ratio, 6),
                "half_life":   round(hl, 2),
                "spread_mean": round(spread_mean, 6),
                "spread_std":  round(spread_std, 6),
                "z_score":     round(z_score, 4),
                "as_of":       datetime.date.today().isoformat(),
            })
        except Exception:
            pass

    # Sort by half_life (faster mean-reversion = more tradeable)
    valid_pairs.sort(key=lambda p: p["half_life"])
    print(f"  [stat_arb] Tested {tested} pairs → {passed_coint} cointegrated "
          f"(p<{COINT_P_THRESH}) → {passed_hl} also in HL band "
          f"({HALF_LIFE_MIN}–{HALF_LIFE_MAX}d) → {len(valid_pairs)} stored")
    return valid_pairs[:SCAN_TOP_N_PAIRS]


# ── Signal generation ─────────────────────────────────────────────────────────
def generate_signals(pairs: list[dict], prices: pd.DataFrame) -> list[dict]:
    """
    For each cointegrated pair, compute current spread z-score and signal.
    Signal: ENTER_LONG_Y  (buy Y, short X)
            ENTER_SHORT_Y (short Y, buy X)
            EXIT          (spread has reverted, close position)
            HOLD          (no actionable signal)
    """
    signals = []
    for pair in pairs:
        try:
            tk_y = pair["y"]
            tk_x = pair["x"]
            if tk_y not in prices.columns or tk_x not in prices.columns:
                continue
            y = np.log(prices[tk_y].dropna().values[-LOOKBACK_DAYS:].astype(float))
            x = np.log(prices[tk_x].dropna().values[-LOOKBACK_DAYS:].astype(float))
            n = min(len(y), len(x))
            if n < 30:
                continue
            # z-score on the static OLS spread (same basis the pair was screened
            # on); Kalman β kept as the live time-varying hedge ratio to trade.
            betas, _kalman_spread = kalman_hedge_ratio(y[-n:], x[-n:])
            _ols_b, spread = static_ols_spread(y[-n:], x[-n:])
            hedge_ratio   = float(betas[-1])
            spread_mean   = float(spread.mean())
            spread_std    = float(spread.std())
            z_score       = float((spread[-1] - spread_mean) / max(spread_std, 1e-8))

            if z_score > ENTRY_Z:
                action = "ENTER_SHORT_Y"      # Y expensive vs X: short Y, long X
            elif z_score < -ENTRY_Z:
                action = "ENTER_LONG_Y"       # Y cheap vs X: long Y, short X
            elif abs(z_score) < EXIT_Z:
                action = "EXIT"               # spread reverted: close position
            else:
                action = "HOLD"

            signals.append({
                "pair":        f"{tk_y}/{tk_x}",
                "y":           tk_y,
                "x":           tk_x,
                "sector":      pair.get("sector", ""),
                "hedge_ratio": round(hedge_ratio, 6),
                "half_life":   pair.get("half_life", 0),
                "z_score":     round(z_score, 4),
                "spread_mean": round(spread_mean, 6),
                "spread_std":  round(spread_std, 6),
                "action":      action,
                "generated":   datetime.datetime.utcnow().isoformat(),
            })
        except Exception:
            pass

    print(f"  [stat_arb] Signals: "
          + ", ".join(f"{a}={sum(1 for s in signals if s['action']==a)}"
                      for a in ["ENTER_LONG_Y","ENTER_SHORT_Y","EXIT","HOLD"]))
    return signals


# ── Append to pair history CSV ─────────────────────────────────────────────────
def append_history(signals: list[dict]):
    today = datetime.date.today().isoformat()
    rows  = [{"date": today, "pair": s["pair"], "z_score": s["z_score"],
              "action": s["action"], "hedge_ratio": s["hedge_ratio"]}
             for s in signals]
    df_new = pd.DataFrame(rows)
    try:
        df_old = pd.read_csv(HISTORY_FILE)
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        # missing, empty, or whitespace-only file — start fresh
        df_all = df_new
    df_all.to_csv(HISTORY_FILE, index=False)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Stat arb pairs scanner")
    parser.add_argument("--signal-only", action="store_true",
                        help="Use cached pairs file, skip cointegration scan")
    parser.add_argument("--scan-only", action="store_true",
                        help="Run scan only, do not generate trading signals")
    args = parser.parse_args()

    print(f"\n{'='*55}")
    print(f"STAT ARB — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}")

    all_tickers = [t for members in PAIR_UNIVERSE.values() for t in members]
    all_tickers = sorted(set(all_tickers))

    # ── Load prices ───────────────────────────────────────────────────────────
    print(f"\nLoading prices for {len(all_tickers)} tickers...")
    prices = load_prices(all_tickers)
    if prices.empty:
        print("  ERROR: no price data — exiting")
        sys.exit(1)
    print(f"  Prices loaded: {prices.shape[0]} days × {prices.shape[1]} tickers")

    # ── Cointegration scan ────────────────────────────────────────────────────
    if args.signal_only and PAIRS_FILE.exists():
        pairs = json.loads(PAIRS_FILE.read_text())
        print(f"\nUsing cached pairs: {len(pairs)} pairs from {PAIRS_FILE}")
    else:
        print(f"\nRunning cointegration scan...")
        pairs = scan_pairs(prices)
        PAIRS_FILE.write_text(json.dumps(pairs, indent=2))
        print(f"  → {PAIRS_FILE} ({len(pairs)} pairs)")

    if args.scan_only:
        print("\nScan complete (--scan-only mode). Exiting.")
        return

    # ── Generate signals ──────────────────────────────────────────────────────
    print(f"\nGenerating signals...")
    signals = generate_signals(pairs, prices)
    SIGNALS_FILE.write_text(json.dumps(signals, indent=2))
    print(f"  → {SIGNALS_FILE}")

    append_history(signals)
    print(f"  → {HISTORY_FILE} (history appended)")

    # ── Summary ───────────────────────────────────────────────────────────────
    actionable = [s for s in signals if s["action"] != "HOLD"]
    print(f"\n{'='*55}")
    print(f"STAT ARB COMPLETE — {len(actionable)} actionable signals")
    if actionable:
        for s in actionable[:10]:
            print(f"  {s['action']:18s}  {s['pair']:15s}  z={s['z_score']:+.2f}  "
                  f"HL={s['half_life']:.1f}d  β={s['hedge_ratio']:.4f}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
