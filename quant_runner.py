#!/usr/bin/env python3
"""
Quant Terminal v25 — GitHub Actions Runner
=============================================
Supports three run types controlled by RUN_TYPE env var:

  morning  (9:35 AM ET)  — full cycle: macro + train + signals + trade + score + learn
  intraday (12:00, 3:00) — light: load cached models, refresh signals, trade if changed
  evening  (5:30 PM ET)  — close: score matured outcomes + self-learning + weights update

State persists via:
  1. Google Drive (rclone) if GDRIVE_RCLONE_CONF secret is set
  2. Git commit of data/ directory (always — fallback and audit trail)
"""

import os
import sys
import json
import pickle
import subprocess
import datetime
import traceback
from pathlib import Path

# ── Run type ──────────────────────────────────────────────────────────────
RUN_TYPE = os.environ.get("RUN_TYPE", "morning").lower()
print(f"\n{'='*60}")
print(f"QUANT TERMINAL v25 — {RUN_TYPE.upper()} RUN")
print(f"Started: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
print(f"{'='*60}\n")

# ── Logging — tee stdout to cycle_output.log ─────────────────────────────
import io

class _Tee:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            try: s.write(data); s.flush()
            except Exception: pass
        return len(data)
    def flush(self):
        for s in self._streams:
            try: s.flush()
            except Exception: pass

_log_fh = open("cycle_output.log", "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _log_fh)
sys.stderr = _Tee(sys.__stderr__, _log_fh)

# ── Paths ─────────────────────────────────────────────────────────────────
LOCAL_DATA     = Path("data")
MODEL_CACHE    = LOCAL_DATA / "models" / "model_cache.pkl"
GDRIVE_CONF    = os.environ.get("GDRIVE_RCLONE_CONF", "")
GDRIVE_FOLDER  = "quant_terminal_v25"

for sub in ["paper_trades", "predictions", "weights", "models"]:
    (LOCAL_DATA / sub).mkdir(parents=True, exist_ok=True)

_KILL_FLAG = LOCAL_DATA / "KILL_SWITCH_ACTIVE.flag"

# ── rclone helpers ────────────────────────────────────────────────────────
def _write_rclone_conf():
    if not GDRIVE_CONF:
        print("  GDRIVE_RCLONE_CONF not set — Drive sync disabled")
        return False
    import base64
    p = Path.home() / ".config" / "rclone" / "rclone.conf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(base64.b64decode(GDRIVE_CONF.encode('ascii', 'ignore')))
    print("  rclone config written")
    return True

def _rclone(src, dst, label):
    try:
        r = subprocess.run(
            ["rclone", "copy", src, dst,
             "--exclude", "model_cache.pkl",
             "--transfers", "8", "--quiet"],
            capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            print(f"  {label} OK")
        else:
            print(f"  {label} warning: {r.stderr[:200]}")
    except Exception as e:
        print(f"  {label} error: {e}")

_drive_ok = _write_rclone_conf()
if _drive_ok:
    print("Stage 0a: Drive -> local sync...")
    _rclone(f"gdrive:{GDRIVE_FOLDER}", str(LOCAL_DATA), "Drive->local")

# ── GitHub Actions config patch injected into Cell 3 ─────────────────────
GH_PATCH = """
import os as _os
if _os.environ.get("GH_ACTIONS"):
    from pathlib import Path as _P
    _drive_dir     = _P("data")
    _drive_mounted = False
    PT_LOG_FILE         = "data/paper_trades/paper_trades.csv"
    PRED_LOG_FILE       = "data/predictions/predictions.csv"
    DAILY_PNL_LOG_FILE  = "data/predictions/daily_pnl_log.csv"
    LOG_DIR             = "data"
    RULES_FILE          = "data/weights/learned_rules.json"
    WEIGHTS_FILE        = "data/weights/adaptive_weights.json"
    RIVER_MODEL_FILE    = "data/weights/river_model.pkl"
    TICKER_CALIB_FILE   = "data/weights/ticker_calibration.json"
    FEATURE_IMP_FILE    = "data/weights/feature_importance.json"
    TICKER_ACC_FILE     = "data/predictions/ticker_accuracy.json"
    MODEL_CACHE_FILE    = _P("data/models/model_cache.pkl")
    VWAP_LOG_FILE       = _P("data/vwap_benchmark.csv")
    EXEC_LOG_FILE       = _P("data/execution_quality.csv")
    KILL_FLAG_FILE      = _P("data/KILL_SWITCH_ACTIVE.flag")
    PDT_LOG_FILE        = _P("data/pdt_log.csv")
    MODEL_RETRAIN_FLAG  = _P("data/RETRAIN_NEEDED.flag")
    DISCORD_WEBHOOK_URL = _os.environ.get("DISCORD_WEBHOOK_URL", "")
    QUIVER_QUANT_KEY    = _os.environ.get("QUIVER_QUANT_KEY", "")
    ALPACA_API_KEY      = _os.environ.get("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY   = _os.environ.get("ALPACA_SECRET_KEY", "")
    ALPACA_BASE_URL     = _os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    RUN_TYPE_GH         = _os.environ.get("RUN_TYPE", "morning")
    FAST_MODE           = (RUN_TYPE_GH != "morning")
    GARCH_PATHS         = 100 if RUN_TYPE_GH == "morning" else 30
    QUICK_TUNE_TRIALS   = 2
    FULL_TUNE_TRIALS_XGB = 15
    FULL_TUNE_TRIALS_LGB = 15
    FULL_TUNE_TRIALS_CAT = 15
    print(f"GH_ACTIONS {RUN_TYPE_GH}: FAST_MODE={FAST_MODE} GARCH_PATHS={GARCH_PATHS}")
"""

# ── Cell skip rules per run type ──────────────────────────────────────────
ALWAYS_SKIP = {0, 1, 16, 17, 18, 19, 20, 21, 22, 23}

SKIP_BY_TYPE = {
    "morning":  ALWAYS_SKIP,
    "intraday": ALWAYS_SKIP | {7, 8, 9},           # skip model training; cell 13 runs stops-only
    "evening":  ALWAYS_SKIP | {7, 8, 9, 10, 11, 12, 13},  # skip through paper trade
}

# Fix #6: intraday runs execute cell 13 in stops-only mode (check_stops_and_expiry + exit)
# Injected into the notebook namespace before cell 13 executes on intraday runs.
INTRADAY_STOPS_PATCH = """
import os as _isp_os
if _isp_os.environ.get('GH_ACTIONS') and _isp_os.environ.get('RUN_TYPE','morning') == 'intraday':
    INTRADAY_STOPS_ONLY = True
else:
    INTRADAY_STOPS_ONLY = False
"""
SKIP_CELLS = SKIP_BY_TYPE.get(RUN_TYPE, ALWAYS_SKIP)

CELL_TAGS = {
    3: "Config", 4: "Macro data", 5: "Ticker download",
    6: "Feature engineering", 7: "HMM regimes", 8: "ML ensemble",
    9: "GARCH + IV", 10: "FinBERT sentiment", 11: "Signal generator",
    12: "CVaR optimization", 13: "Paper trading",
    14: "Outcome scoring", 15: "Self-learning",
}

# ══════════════════════════════════════════════════════════════════════════════
# CELL PATCHES — injected before/after each notebook cell at runtime.
# All structural fixes live here so the notebook JSON stays clean.
# ══════════════════════════════════════════════════════════════════════════════

# ── CELL 4 PREPATCH: FRED publication lags (prevents look-ahead bias) ─────────
CELL_4_PREPATCH = """
import time as _fl_time, os as _fl_os
_FRED_PUB_LAG = {
    # key matches the short names used in MACRO dict (not raw FRED series IDs)
    "gdp_growth":         30,   # GDP: released ~30 days after quarter-end
    "cpi_yoy":            15,   # CPI: released ~15 days after month-end
    "core_cpi":           15,
    "pce":                28,   # PCE: ~28 days after month-end
    "jolts":              45,   # JOLTS: ~45 days after month-end
    "unemployment":        5,   # Released first Friday of following month
    "retail_sales":       15,
    "housing_starts":     16,
    "consumer_sentiment": 14,
    "m2_growth":          30,
    "initial_claims":      7,
    "conf_board_lei":     30,
}

def _apply_fred_lag(macro_dict, ref_date=None):
    import datetime as _dt
    if ref_date is None:
        ref_date = _dt.date.today()
    elif hasattr(ref_date, 'date'):
        ref_date = ref_date.date()
    lagged = dict(macro_dict)
    for key, lag_days in _FRED_PUB_LAG.items():
        if key in lagged and lagged[key] is not None:
            # If the data would not yet be published on ref_date, null it out
            # (caller fills with prior value or default)
            pass  # lag applied at join time; here we just expose the map
    return lagged

# 24-hour FRED cache — FRED data does not change intraday
_FRED_CACHE_FILE = "data/fred_cache.json"
_FRED_CACHE_TTL  = 86400  # seconds

def _fred_cached(series_id, fallback=None, fred_key=None):
    import json as _jc, time as _tc, requests as _rc
    from pathlib import Path as _Pc
    _p = _Pc(_FRED_CACHE_FILE)
    try:
        _cache = _jc.loads(_p.read_text()) if _p.exists() else {}
    except Exception:
        _cache = {}
    _age = _tc.time() - _cache.get(f"{series_id}_ts", 0)
    if _age < _FRED_CACHE_TTL and series_id in _cache:
        return _cache[series_id]
    if not fred_key:
        return fallback
    try:
        _url = (f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={series_id}&api_key={fred_key}"
                f"&file_type=json&limit=1&sort_order=desc")
        _r = _rc.get(_url, timeout=10)
        _val = _r.json()["observations"][0]["value"]
        _val = round(float(_val), 4) if _val != "." else fallback
    except Exception:
        _val = fallback
    _cache[series_id] = _val
    _cache[f"{series_id}_ts"] = _tc.time()
    try:
        _p.write_text(_jc.dumps(_cache, indent=2))
    except Exception:
        pass
    return _val

print("  [patch] FRED lag map and 24h cache injected")
"""

# ── CELL 5 PREPATCH: extend download history to 10 years ─────────────────────
# The notebook fetches 1-2 years by default. 10 years captures multiple market
# cycles (2015-16 correction, 2018 Q4 crash, 2020 COVID, 2022 rate-hike bear)
# which dramatically improves regime generalization and reduces overfitting to
# the most recent bull market. Monkey-patches yfinance.download so the notebook
# code needs no changes.
CELL_5_PREPATCH = """
import yfinance as _yf5

_yf5_orig_download = _yf5.download

def _yf5_patched_download(*args, **kwargs):
    # Override period to at least 10y; respect explicit start/end if provided
    if "start" not in kwargs and "end" not in kwargs:
        _orig_period = kwargs.get("period", "2y")
        # Map short periods to 10y; leave anything already >= 5y alone
        _short = {"1d","5d","1mo","3mo","6mo","1y","2y","ytd"}
        if str(_orig_period) in _short:
            kwargs["period"] = "10y"
    return _yf5_orig_download(*args, **kwargs)

_yf5.download = _yf5_patched_download
print("  [patch] yfinance.download patched: history extended to 10y")
"""

# ── CELL 5 POSTPATCH: OHLCV sanity check (drops corrupt/delisted tickers) ────
CELL_5_POSTPATCH = """
import pandas as _pd5
_bad_tickers = []
for _tk5, _df5 in list(raw_data.items()):
    if _df5 is None or _df5.empty:
        _bad_tickers.append(_tk5); continue
    _close5 = _pd5.to_numeric(_df5.get("Close", _pd5.Series()), errors="coerce")
    _vol5   = _pd5.to_numeric(_df5.get("Volume", _pd5.Series()), errors="coerce")
    _nan_frac = _close5.isna().mean()
    if (_close5.dropna() <= 0).all() or (_vol5.dropna() == 0).all() or _nan_frac > 0.50:
        _bad_tickers.append(_tk5)
if _bad_tickers:
    for _tk5 in _bad_tickers:
        raw_data.pop(_tk5, None)
    print(f"  [patch] OHLCV sanity: dropped {len(_bad_tickers)} tickers: {_bad_tickers[:10]}")
else:
    print("  [patch] OHLCV sanity: all tickers clean")
"""

# ── CELL 6 PREPATCH: helper functions for Fixes 10-13 ───────────────────────
# Injected into namespace before Cell 6. Cell 6 then calls these functions
# when building features (they appear in the namespace via exec scope).
CELL_6_PREPATCH = """
import numpy as _np6
import pandas as _pd6

# ── Fix 12: Hurst Exponent ────────────────────────────────────────────────────
def _hurst_exponent(ts, max_lag=100):
    \"\"\"
    Hurst exponent via rescaled range analysis.
    H < 0.5: mean-reverting  |  H = 0.5: random walk  |  H > 0.5: trending
    \"\"\"
    try:
        arr = _np6.array(ts, dtype=float)
        arr = arr[~_np6.isnan(arr)]
        if len(arr) < 20:
            return 0.5
        lags = range(2, min(max_lag, len(arr) // 2))
        tau  = []
        for lag in lags:
            diffs = _np6.subtract(arr[lag:], arr[:-lag])
            if len(diffs) == 0:
                continue
            tau.append(_np6.std(diffs))
        if len(tau) < 3:
            return 0.5
        lags_arr = _np6.array(list(range(2, 2 + len(tau))), dtype=float)
        tau_arr  = _np6.array(tau, dtype=float)
        # Avoid log of zero
        mask = (lags_arr > 0) & (tau_arr > 0)
        if mask.sum() < 3:
            return 0.5
        H = _np6.polyfit(_np6.log(lags_arr[mask]), _np6.log(tau_arr[mask]), 1)[0]
        return float(_np6.clip(H, 0.0, 1.0))
    except Exception:
        return 0.5

# ── Fix 13: Fractional Differentiation ───────────────────────────────────────
def _frac_diff(series, d=0.4, thres=0.01):
    \"\"\"
    Fractionally differentiate a price series at order d.
    Preserves memory while achieving approximate stationarity.
    Typical d in [0.3, 0.5] for equity prices.
    Returns a pd.Series aligned to input index.
    \"\"\"
    try:
        arr = _np6.array(series.values if hasattr(series, 'values') else series,
                         dtype=float)
        # Build weight vector
        w = [1.0]
        for k in range(1, len(arr)):
            w_k = -w[-1] / k * (d - k + 1)
            if abs(w_k) < thres:
                break
            w.append(w_k)
        w = _np6.array(w[::-1])
        width = len(w)
        out = _np6.full(len(arr), _np6.nan)
        for i in range(width - 1, len(arr)):
            out[i] = float(_np6.dot(w, arr[i - width + 1: i + 1]))
        idx = series.index if hasattr(series, 'index') else range(len(arr))
        return _pd6.Series(out, index=idx)
    except Exception:
        return _pd6.Series(_np6.full(len(series), _np6.nan),
                           index=series.index if hasattr(series, 'index') else None)

# ── Fix 10: Triple Barrier Label ─────────────────────────────────────────────
def _triple_barrier_labels(close_series, atr_series, horizon=5, atr_mult=1.5):
    \"\"\"
    Label each bar by which barrier is hit first within `horizon` days:
      +1 = upper barrier hit (profit)
      -1 = lower barrier hit (stop-loss)
       0 = time barrier hit (neither)
    Barriers: upper = price + atr_mult * ATR, lower = price - atr_mult * ATR.
    Returns a pd.Series of int8 labels, NaN at tail.
    \"\"\"
    try:
        close = _np6.array(close_series.values if hasattr(close_series, 'values')
                           else close_series, dtype=float)
        atr   = _np6.array(atr_series.values if hasattr(atr_series, 'values')
                           else atr_series, dtype=float)
        n     = len(close)
        labels = _np6.full(n, _np6.nan)
        for i in range(n - horizon):
            p0    = close[i]
            h_val = atr[i] * atr_mult
            if _np6.isnan(p0) or _np6.isnan(h_val) or h_val <= 0:
                continue
            upper = p0 + h_val
            lower = p0 - h_val
            label = 0  # default: time barrier
            for j in range(i + 1, min(i + horizon + 1, n)):
                if close[j] >= upper:
                    label = 1
                    break
                elif close[j] <= lower:
                    label = -1
                    break
            labels[i] = label
        idx = (close_series.index if hasattr(close_series, 'index')
               else range(n))
        return _pd6.Series(labels, index=idx)
    except Exception:
        return _pd6.Series(_np6.full(len(close_series), _np6.nan))

# ── Fix 11: Proper SUE (Standardized Unexpected Earnings) ────────────────────
def _compute_sue(ticker):
    \"\"\"
    SUE = recent_surprise / std_dev_of_historical_surprises.
    Uses yfinance earnings_history Surprise(%) column.
    Returns float in [-5, 5]; 0.0 on failure.
    \"\"\"
    try:
        import yfinance as _yf11
        _eh = _yf11.Ticker(ticker).earnings_history
        if _eh is None or _eh.empty:
            return 0.0
        if "Surprise(%)" not in _eh.columns:
            return 0.0
        _s = _eh["Surprise(%)"].replace(
            [float("inf"), float("-inf")], float("nan")).dropna()
        if len(_s) < 2:
            return float(_s.iloc[-1] / 100.0) if len(_s) == 1 else 0.0
        _recent = float(_s.iloc[-1])
        _std    = max(float(_s.std()), 0.1)  # floor to avoid div-by-zero
        return float(_np6.clip(_recent / _std, -5.0, 5.0))
    except Exception:
        return 0.0

print("  [patch] Feature helpers injected: hurst, frac_diff, triple_barrier, SUE")

# ── Fix E: Cross-sectional momentum helper ────────────────────────────────────
# xs_mom = ticker_1d_return - sector_etf_1d_return, z-scored cross-sectionally.
# Captures relative strength vs. the sector, removing market/sector beta from
# the momentum signal so only idiosyncratic price strength remains.
_SECTOR_ETF_MAP_XS = {
    "Technology":    "XLK",
    "Financials":    "XLF",
    "Healthcare":    "XLV",
    "Energy":        "XLE",
    "Consumer Disc": "XLY",
    "Consumer Staples": "XLP",
    "Industrials":   "XLI",
    "Materials":     "XLB",
    "Utilities":     "XLU",
    "Real Estate":   "XLRE",
    "Communication": "XLC",
}

def _fetch_sector_etf_returns(etf_tickers, period="1y"):
    \"\"\"Download daily returns for sector ETFs; returns dict[ticker -> pd.Series].\"\"\"
    import yfinance as _yf6xs
    _results = {}
    for _etf in etf_tickers:
        try:
            _df = _yf6xs.download(_etf, period=period, progress=False, auto_adjust=True)
            if _df is not None and not _df.empty and "Close" in _df.columns:
                _results[_etf] = _df["Close"].pct_change()
        except Exception:
            pass
    return _results

print("  [patch] Cross-sectional momentum helpers injected")

# ── Intraday feature helpers ───────────────────────────────────────────────────
# Fetches 15-min bars (60-day max on yfinance free tier) for a single ticker
# and returns a daily DataFrame with intraday-derived features:
#   intraday_mom     : (close - open) / open — how the day closed vs. opened
#   overnight_gap    : (open - prev_close) / prev_close — gap signal
#   vwap_dev         : (close - VWAP) / VWAP — mean reversion vs. daily average
#   intraday_range   : (high - low) / close — realized intraday volatility
#   close_to_high    : (high - close) / (high - low + 1e-8) — selling pressure
def _fetch_intraday_features(ticker, period="60d", interval="15m"):
    \"\"\"Returns a pd.DataFrame indexed by date with 5 intraday-derived features.\"\"\"
    try:
        import yfinance as _yfi
        _bars = _yfi.download(ticker, period=period, interval=interval,
                              progress=False, auto_adjust=True)
        if _bars is None or _bars.empty or len(_bars) < 10:
            return _pd6.DataFrame()
        # Flatten MultiIndex columns if present
        if isinstance(_bars.columns, _pd6.MultiIndex):
            _bars.columns = _bars.columns.get_level_values(0)
        _bars.index = _pd6.to_datetime(_bars.index)
        _bars["_date"] = _bars.index.normalize()
        _grp = _bars.groupby("_date")

        _daily = _pd6.DataFrame(index=_grp.groups.keys())
        _daily.index = _pd6.to_datetime(_daily.index)

        # VWAP per day
        _daily["_vwap"]  = _grp.apply(
            lambda g: (g["Close"] * g["Volume"]).sum() / g["Volume"].sum().clip(1))
        _daily["_open"]  = _grp["Open"].first()
        _daily["_close"] = _grp["Close"].last()
        _daily["_high"]  = _grp["High"].max()
        _daily["_low"]   = _grp["Low"].min()

        _pc = _daily["_close"].shift(1)
        _hl = (_daily["_high"] - _daily["_low"]).clip(1e-8)

        _daily["intraday_mom"]   = (_daily["_close"] - _daily["_open"]) / _daily["_open"].clip(1e-8)
        _daily["overnight_gap"]  = (_daily["_open"]  - _pc) / _pc.clip(1e-8)
        _daily["vwap_dev"]       = (_daily["_close"] - _daily["_vwap"]) / _daily["_vwap"].clip(1e-8)
        _daily["intraday_range"] = _hl / _daily["_close"].clip(1e-8)
        _daily["close_to_high"]  = (_daily["_high"] - _daily["_close"]) / _hl

        return _daily[["intraday_mom","overnight_gap","vwap_dev",
                        "intraday_range","close_to_high"]].shift(1)  # shift=1: no lookahead
    except Exception:
        return _pd6.DataFrame()

# ── SEC Form 4 insider net-buy score ─────────────────────────────────────────
# Uses yfinance Ticker.insider_transactions (no API key required).
# Returns a float in [-1, +1]: +1 = all buys, -1 = all sells, 0 = balanced.
def _insider_net_buy_score(ticker, lookback_days=30):
    \"\"\"Compute insider net-buy ratio over trailing lookback_days.\"\"\"
    try:
        import yfinance as _yfi2
        import datetime as _dt_ins
        _it = _yfi2.Ticker(ticker).insider_transactions
        if _it is None or _it.empty:
            return 0.0
        # Normalize column names
        _it.columns = [c.lower().replace(" ","_") for c in _it.columns]
        # Date column
        _date_col = next((c for c in _it.columns if "date" in c), None)
        if _date_col is None:
            return 0.0
        _it[_date_col] = _pd6.to_datetime(_it[_date_col], errors="coerce")
        _cutoff = _pd6.Timestamp.utcnow().tz_localize(None) - _pd6.Timedelta(days=lookback_days)
        _recent = _it[_it[_date_col] >= _cutoff]
        if _recent.empty:
            return 0.0
        # Transaction type
        _tx_col = next((c for c in _it.columns if "transaction" in c or "text" in c), None)
        if _tx_col is None:
            return 0.0
        _buys  = _recent[_recent[_tx_col].str.lower().str.contains("buy|purchase|acquire", na=False)]
        _sells = _recent[_recent[_tx_col].str.lower().str.contains("sell|sale|dispose", na=False)]
        _n_b, _n_s = len(_buys), len(_sells)
        if _n_b + _n_s == 0:
            return 0.0
        return float((_n_b - _n_s) / (_n_b + _n_s))
    except Exception:
        return 0.0

print("  [patch] Intraday feature helpers + insider net-buy score injected")
"""

# ── CELL 6 POSTPATCH: apply new features + triple barrier labels + VIF + parallel
CELL_6_POSTPATCH = """
import numpy as _np6p
import pandas as _pd6p
from concurrent.futures import ThreadPoolExecutor as _TPE
from pathlib import Path as _P6p

# ── Fix 8: Parallel feature re-build with additional features ─────────────────
# Re-runs build_features and adds hurst, frac_diff, SUE per ticker in parallel.
def _augment_ticker(tk_df_pair):
    tk, fd = tk_df_pair
    try:
        # Hurst exponent on Close prices (252-day rolling)
        _close = fd["Close"] if "Close" in fd.columns else None
        if _close is not None and len(_close) >= 60:
            _hurst_vals = _close.rolling(window=60, min_periods=30).apply(
                lambda x: _hurst_exponent(x), raw=True)
            fd["hurst_exp"] = _hurst_vals.shift(1)

            # Frac-diff of Close (d=0.4)
            _fd_vals = _frac_diff(_close, d=0.4)
            fd["frac_diff_close"] = _fd_vals.shift(1)

        # SUE score (scalar per ticker, from earnings history)
        fd["sue_score"] = _compute_sue(tk)

        # ── Fix 10: Override target with Triple Barrier labels ──────────────
        if "atr_14" in fd.columns and "Close" in fd.columns:
            _tb = _triple_barrier_labels(fd["Close"], fd["atr_14"],
                                         horizon=5, atr_mult=1.5)
            # Map: 1=BUY, -1=SELL→0 for binary, keep NaN as NaN
            # We keep original ternary but remap to binary (0/1) for sklearn:
            # +1 → 1 (correct BUY), -1 → 0 (correct SELL), 0 → NaN (flat)
            _bin = _pd6p.Series(_np6p.where(_tb == 1, 1,
                                _np6p.where(_tb == -1, 0, _np6p.nan)),
                                index=_tb.index)
            fd["target_tb"] = _bin    # keep original quintile in "target"
            # Use triple barrier as primary label if enough non-NaN
            _n_tb = _bin.notna().sum()
            _n_orig = fd["target"].notna().sum()
            if _n_tb > max(30, _n_orig * 0.30):
                fd["target"] = _bin
                fd["target_method"] = "triple_barrier"
            else:
                fd["target_method"] = "quintile"
        else:
            fd["target_method"] = "quintile"

        # ── Fix (from HANDOFF): Rolling quintile labels on non-TB tickers ──
        _is_quintile = ("target_method" not in fd.columns or
                        (fd["target_method"] == "quintile").all()
                        if "target_method" in fd.columns else True)
        if _is_quintile and "fwd_ret" in fd.columns:
            _fr = fd["fwd_ret"]
            _q80 = _fr.rolling(252, min_periods=63).quantile(0.80)
            _q20 = _fr.rolling(252, min_periods=63).quantile(0.20)
            fd["target"] = _np6p.where(_fr > _q80, 1,
                           _np6p.where(_fr < _q20, 0, _np6p.nan))

        return tk, fd
    except Exception as _e6p:
        return tk, fd  # return unmodified on error

print("  [patch] Augmenting features in parallel (hurst, frac_diff, SUE, triple barrier)...")
_n_workers = min(16, len(featured))
_augmented = {}
with _TPE(max_workers=_n_workers) as _ex6p:
    for _tk6p, _fd6p in _ex6p.map(_augment_ticker, featured.items()):
        _augmented[_tk6p] = _fd6p
featured = _augmented

# Rebuild FEATURE_COLS to include new columns
FEATURE_COLS = [c for c in next(iter(featured.values())).columns
                if c not in ["Open","High","Low","Close","Volume",
                             "target","target_tb","target_method","fwd_ret"]]

# ── Fix 7: VIF-based feature redundancy pruning ───────────────────────────────
# Drop features with Variance Inflation Factor > 10 (multicollinear).
# Run once on a representative ticker to determine FEATURE_COLS to drop.
_VIF_THRESHOLD = 10.0
try:
    from statsmodels.stats.outliers_influence import variance_inflation_factor as _vif_fn
    _rep_tk = next(iter(featured))
    _rep_df = featured[_rep_tk].dropna(subset=["target"])
    _rep_X  = _rep_df[FEATURE_COLS].replace([_np6p.inf, -_np6p.inf], _np6p.nan).dropna()
    if len(_rep_X) >= 50:
        _keep_cols = list(FEATURE_COLS)  # start with all
        # Iterative VIF: remove highest VIF > threshold, repeat
        _max_iter_vif = 5
        for _ in range(_max_iter_vif):
            _X_vif = _rep_X[_keep_cols].values.astype(float)
            if _X_vif.shape[1] < 2:
                break
            _vifs = []
            for _ci in range(_X_vif.shape[1]):
                try:
                    _vifs.append(_vif_fn(_X_vif, _ci))
                except Exception:
                    _vifs.append(0.0)
            _max_vif = max(_vifs)
            if _max_vif <= _VIF_THRESHOLD:
                break
            _drop_idx = _vifs.index(_max_vif)
            _keep_cols.pop(_drop_idx)
        _n_dropped = len(FEATURE_COLS) - len(_keep_cols)
        FEATURE_COLS = _keep_cols
        print(f"  [patch] VIF pruning: dropped {_n_dropped} collinear features, "
              f"{len(FEATURE_COLS)} remain (threshold={_VIF_THRESHOLD})")
except ImportError:
    print("  [patch] statsmodels not installed — VIF pruning skipped")
except Exception as _vif_e:
    print(f"  [patch] VIF pruning error (non-fatal): {_vif_e}")

# ── Fix E: Cross-sectional momentum feature ───────────────────────────────────
# xs_mom_5d = ticker_5d_return - sector_etf_5d_return, z-scored cross-sectionally.
try:
    _ticker_sectors = {}
    _rep_feat = next(iter(featured.values()))
    _feat_tks = list(featured.keys())

    # Determine sector for each ticker from signals if available, else skip
    if "signals" in dir():
        _ticker_sectors = {tk: signals[tk].get("sector", "Unknown")
                           for tk in _feat_tks if tk in signals}
    else:
        # Try from featured df columns
        for _tk6xs, _fd6xs in featured.items():
            _ticker_sectors[_tk6xs] = "Unknown"

    # Collect unique ETFs needed
    _needed_etfs = set()
    for _sec6xs in _ticker_sectors.values():
        _etf6xs = _SECTOR_ETF_MAP_XS.get(_sec6xs)
        if _etf6xs:
            _needed_etfs.add(_etf6xs)

    _etf_returns = _fetch_sector_etf_returns(_needed_etfs) if _needed_etfs else {}

    # Compute 5-day rolling return for each ticker and subtract sector ETF
    _xs_raw = {}  # {ticker: pd.Series of xs_mom values}
    for _tk6xs, _fd6xs in featured.items():
        try:
            _sec6xs = _ticker_sectors.get(_tk6xs, "Unknown")
            _etf6xs = _SECTOR_ETF_MAP_XS.get(_sec6xs)
            _close6xs = _fd6xs["Close"] if "Close" in _fd6xs.columns else None
            if _close6xs is None or len(_close6xs) < 10:
                continue
            _tk_ret5 = _close6xs.pct_change(5)
            if _etf6xs and _etf6xs in _etf_returns:
                _etf_ret5 = _etf_returns[_etf6xs].reindex(_tk_ret5.index).fillna(0)
                _xs_raw[_tk6xs] = _tk_ret5 - _etf_ret5
            else:
                _xs_raw[_tk6xs] = _tk_ret5  # no sector ETF; use raw return
        except Exception:
            pass

    # Z-score cross-sectionally by date
    if _xs_raw:
        import pandas as _pd6xs
        _xs_df = _pd6xs.DataFrame(_xs_raw)
        _xs_mean = _xs_df.mean(axis=1)
        _xs_std  = _xs_df.std(axis=1).replace(0, _np6p.nan)
        _xs_z    = (_xs_df.subtract(_xs_mean, axis=0)
                          .divide(_xs_std, axis=0))
        # Assign back and shift 1 (avoid lookahead)
        _n_xs_added = 0
        for _tk6xs, _fd6xs in featured.items():
            if _tk6xs in _xs_z.columns:
                _col = _xs_z[_tk6xs].reindex(_fd6xs.index).shift(1)
                featured[_tk6xs]["xs_mom_5d"] = _col
                _n_xs_added += 1
        # Add xs_mom_5d to FEATURE_COLS if not already there
        if "xs_mom_5d" not in FEATURE_COLS:
            FEATURE_COLS.append("xs_mom_5d")
        print(f"  [patch] Cross-sectional momentum (xs_mom_5d) added for "
              f"{_n_xs_added}/{len(featured)} tickers")
    else:
        print("  [patch] Cross-sectional momentum: no xs_raw data computed — skipped")
except Exception as _xs6e:
    print(f"  [patch] Cross-sectional momentum error (non-fatal): {_xs6e}")

# ── Intraday features (15-min bars → daily aggregates) ───────────────────────
# Only runs on morning cycle to avoid hitting Yahoo Finance rate limits.
# Fetches 60 calendar days of 15-min bars per ticker in parallel, computes
# 5 daily intraday features and merges them into featured[ticker].
import os as _os6p
if _os6p.environ.get("RUN_TYPE", "morning") == "morning":
    try:
        _INTRADAY_COLS = ["intraday_mom","overnight_gap","vwap_dev",
                          "intraday_range","close_to_high"]
        _n_intraday = 0
        _intraday_errors = 0

        def _fetch_intraday_safe(tk):
            try:
                return tk, _fetch_intraday_features(tk, period="60d", interval="15m")
            except Exception as _e6id:
                return tk, _pd6p.DataFrame()

        with _TPE(max_workers=12) as _ex6p2:
            for _tk6id, _id_df in _ex6p2.map(_fetch_intraday_safe, list(featured.keys())):
                if _id_df is None or _id_df.empty:
                    _intraday_errors += 1
                    continue
                _fd6id = featured[_tk6id]
                for _col in _INTRADAY_COLS:
                    if _col in _id_df.columns:
                        _aligned = _id_df[_col].reindex(_fd6id.index)
                        featured[_tk6id][_col] = _aligned
                _n_intraday += 1

        # Add intraday cols to FEATURE_COLS
        for _col in _INTRADAY_COLS:
            if _col not in FEATURE_COLS:
                FEATURE_COLS.append(_col)

        print(f"  [patch] Intraday features added for {_n_intraday}/{len(featured)} tickers "
              f"({_intraday_errors} failed/rate-limited)")
    except Exception as _id6e:
        print(f"  [patch] Intraday feature error (non-fatal): {_id6e}")
else:
    print("  [patch] Intraday features: skipped on intraday/evening run (morning-only)")

# ── Insider net-buy score (SEC Form 4) ────────────────────────────────────────
# Runs on morning cycle only. Computes trailing 30-day insider buy/sell ratio
# per ticker using yfinance's insider_transactions (no API key required).
# Score ∈ [-1, +1]: +1 = pure buying, -1 = pure selling, 0 = balanced.
if _os6p.environ.get("RUN_TYPE", "morning") == "morning":
    try:
        _INSIDER_CACHE_FILE = _P6p("data/predictions/insider_scores.json")
        _insider_scores_cache = {}
        if _INSIDER_CACHE_FILE.exists():
            try:
                import json as _j6p2
                _insider_scores_cache = _j6p2.loads(_INSIDER_CACHE_FILE.read_text())
            except Exception:
                _insider_scores_cache = {}

        _n_insider = 0

        def _score_insider_safe(tk):
            # Use cache if computed within last 24 hours
            import time as _t6ins
            _cached = _insider_scores_cache.get(tk)
            if _cached and (_t6ins.time() - _cached.get("ts", 0)) < 86400:
                return tk, _cached["score"]
            _score = _insider_net_buy_score(tk, lookback_days=30)
            return tk, _score

        _new_scores = {}
        with _TPE(max_workers=8) as _ex6p3:
            for _tk6ins, _score6ins in _ex6p3.map(_score_insider_safe, list(featured.keys())):
                if _tk6ins in featured:
                    featured[_tk6ins]["insider_net_buy"] = float(_score6ins)
                    _new_scores[_tk6ins] = {"score": _score6ins, "ts": __import__("time").time()}
                    _n_insider += 1

        if "insider_net_buy" not in FEATURE_COLS:
            FEATURE_COLS.append("insider_net_buy")

        # Persist scores cache
        try:
            import json as _j6p3
            _INSIDER_CACHE_FILE.write_text(_j6p3.dumps(_new_scores, indent=2))
        except Exception:
            pass

        print(f"  [patch] Insider net-buy scores added for {_n_insider} tickers")
    except Exception as _ins6e:
        print(f"  [patch] Insider score error (non-fatal): {_ins6e}")

print(f"  [patch] Final feature set: {len(FEATURE_COLS)} features")
"""

# ── CELL 8 PREPATCH: embargo CV + Optuna window + block-bootstrap SMOTE ──────
CELL_8_PREPATCH = """
import numpy as _np8
import sys as _sys8
from sklearn.model_selection import TimeSeriesSplit as _BaseTimeSeriesSplit

# ── Fix 1: EmbargoTimeSeriesSplit — 5-day embargo between folds ───────────────
# ── Fix 2: Optuna window restriction — tunes only on first 62.5% of data ─────
class TimeSeriesSplit:
    \"\"\"
    Drop-in replacement for sklearn TimeSeriesSplit with two correctness fixes:

    Fix 1 — Embargo: skips test samples within embargo_days of train-end to
    prevent serial-correlation leakage for 5-day forward-return labels.

    Fix 2 — Optuna window: restricts all splits to the first optuna_fraction
    of the data so hyperparameter selection cannot see the validation window
    (75-87.5%) or calibration window (87.5-100%).
    \"\"\"
    def __init__(self, n_splits=5, embargo_days=5, optuna_fraction=0.625, **kwargs):
        self._n_splits       = n_splits
        self._embargo        = embargo_days
        self._optuna_fraction = optuna_fraction

    def split(self, X, y=None, groups=None):
        n      = len(X)
        n_tune = max(int(n * self._optuna_fraction), self._n_splits * 10)
        inner  = _BaseTimeSeriesSplit(n_splits=self._n_splits)
        for train_idx, test_idx in inner.split(X[:n_tune]):
            if len(train_idx) == 0:
                continue
            train_end      = int(train_idx[-1])
            embargo_end    = train_end + self._embargo
            test_embargoed = test_idx[test_idx > embargo_end]
            if len(test_embargoed) >= 5:
                yield train_idx, test_embargoed

    def get_val_indices(self, X):
        \"\"\"
        Returns indices for the held-out AUC validation window (62.5%–75%).
        Use this in the Optuna objective to measure generalization on data the
        CV folds never touched — preventing hyperparameter leakage into val.
        \"\"\"
        n       = len(X)
        n_start = int(n * self._optuna_fraction)
        n_end   = int(n * (self._optuna_fraction + 0.125))
        return _np8.arange(n_start, min(n_end, n))

    def get_test_indices(self, X):
        \"\"\"
        Returns indices for the Platt calibration window (75%–87.5%).
        Calibrate predict_proba on data disjoint from both Optuna and val.
        \"\"\"
        n       = len(X)
        n_start = int(n * (self._optuna_fraction + 0.125))
        n_end   = int(n * (self._optuna_fraction + 0.250))
        return _np8.arange(n_start, min(n_end, n))

    def get_meta_indices(self, X):
        \"\"\"
        Returns indices for the stacking meta-learner window (87.5%–100%).
        Train the meta-learner on out-of-bag base-model predictions here.
        \"\"\"
        n       = len(X)
        n_start = int(n * (self._optuna_fraction + 0.250))
        return _np8.arange(n_start, n)

# ── Block-bootstrap minority-class oversampling (replaces SMOTE) ─────────────
# SMOTE interpolates between random minority samples from any time period —
# a subtle look-ahead for time-series data.
# Block-bootstrap duplicates contiguous 5-row blocks, preserving autocorrelation.
class _BlockBootstrapSMOTE:
    def __init__(self, random_state=42, **kwargs):
        self._rng = _np8.random.default_rng(random_state)
    def fit_resample(self, X, y):
        _classes, _counts = _np8.unique(y, return_counts=True)
        if len(_classes) < 2:
            return X, y
        _maj_cls = _classes[_np8.argmax(_counts)]
        _min_cls = _classes[_np8.argmin(_counts)]
        _n_needed = int(_counts.max()) - int(_counts.min())
        if _n_needed <= 0:
            return X, y
        _min_idx  = _np8.where(y == _min_cls)[0]
        _block_sz = 5
        _extra_X, _extra_y = [], []
        while sum(len(b) for b in _extra_X) < _n_needed:
            _s = int(self._rng.integers(0, max(1, len(_min_idx) - _block_sz)))
            _block = _min_idx[_s : _s + _block_sz]
            _extra_X.append(X[_block])
            _extra_y.append(y[_block])
        _extra_X = _np8.vstack(_extra_X)[:_n_needed]
        _extra_y = _np8.concatenate(_extra_y)[:_n_needed]
        return _np8.vstack([X, _extra_X]), _np8.concatenate([y, _extra_y])

try:
    import imblearn.over_sampling as _ios8
    _ios8.SMOTE = _BlockBootstrapSMOTE
    if "imblearn.over_sampling" in _sys8.modules:
        _sys8.modules["imblearn.over_sampling"].SMOTE = _BlockBootstrapSMOTE
except ImportError:
    pass
SMOTE = _BlockBootstrapSMOTE

# ── 4-window meta-learner fractions (from HANDOFF v25.1) ─────────────────────
_META_TRAIN_FRAC = 0.625   # Optuna tunes on 0-62.5%
_META_VAL_FRAC   = 0.125   # AUC reported on 62.5-75%
_META_CAL_FRAC   = 0.125   # Platt calibration on 75-87.5%
_META_META_FRAC  = 0.125   # Stacking meta-learner on 87.5-100%

print("  [patch] EmbargoTimeSeriesSplit, BlockBootstrap, 4-window fractions injected")
"""

# ── CELL 8 POSTPATCH: Ridge ensemble member ───────────────────────────────────
CELL_8_POSTPATCH = """
import numpy as _np8p
from sklearn.linear_model import RidgeClassifier as _RidgeCls8

_RIDGE_WEIGHT = 0.15
_BOOST_WEIGHT = round((1.0 - _RIDGE_WEIGHT) / 3, 6)   # ~0.2833 each

_ridge_added = 0
for _rtk8, _rm8 in models.items():
    try:
        _scl8 = _rm8["scaler"]
        _fd8  = featured[_rtk8].dropna(subset=["target"])
        if len(_fd8) < 50:
            continue
        _Xr8  = _scl8.transform(_fd8[FEATURE_COLS].values)
        _yr8  = _fd8["target"].values.astype(int)
        if len(_np8p.unique(_yr8)) < 2:
            continue
        _ridge8 = _RidgeCls8(alpha=1.0)
        _ridge8.fit(_Xr8, _yr8)
        _rm8["ridge"] = _ridge8
        _ridge_added += 1
    except Exception:
        pass

print(f"  [patch] Ridge ensemble members added: {_ridge_added}/{len(models)}")
"""

# ── CELL 9 PREPATCH: disable EarningsWhispers scraper ────────────────────────
CELL_9_PREPATCH = """
# Stub out earningswhispers.com requests — DOM changes cause silent failures.
# The 3-source consensus (yfinance + Alpha Vantage + Finnhub) is sufficient.
import requests as _req9_orig, unittest.mock as _mock9
_ew_sentinel = object()
def _stub_ew(*args, **kwargs):
    # Return a mock response that any EW parser will treat as empty
    _m = _mock9.MagicMock()
    _m.status_code = 404
    _m.text = ""
    _m.json.side_effect = Exception("EarningsWhispers disabled by runner patch")
    return _m

# Intercept any get/post to earningswhispers.com
_orig_get9  = _req9_orig.get
_orig_post9 = _req9_orig.post
def _patched_get9(url, *a, **kw):
    if "earningswhispers" in str(url).lower():
        return _stub_ew()
    return _orig_get9(url, *a, **kw)
def _patched_post9(url, *a, **kw):
    if "earningswhispers" in str(url).lower():
        return _stub_ew()
    return _orig_post9(url, *a, **kw)
_req9_orig.get  = _patched_get9
_req9_orig.post = _patched_post9
print("  [patch] EarningsWhispers scraper disabled")
"""

# ── CELL 11 PREPATCH: IC-weighted composite score weights ─────────────────────
CELL_11_PREPATCH = """
import json as _j11
from pathlib import Path as _P11

# Load IC-derived composite weights if they exist from a previous scoring cycle.
# On first run these fall back to the empirically reasonable defaults.
# After each scoring cycle (post-run), _IC_COMPOSITE_WEIGHTS is updated.
_IC_WEIGHTS_FILE = _P11("data/weights/ic_composite_weights.json")
_IC_COMPOSITE_WEIGHTS = {
    "ensemble":  0.60,
    "garch":     0.15,
    "sentiment": 0.10,
    "regime":    0.10,
    "macro":     0.05,
}
if _IC_WEIGHTS_FILE.exists():
    try:
        _loaded_w = _j11.loads(_IC_WEIGHTS_FILE.read_text())
        # Validate: all keys present, all positive, sum to ~1
        _req_keys = set(_IC_COMPOSITE_WEIGHTS.keys())
        if _req_keys.issubset(set(_loaded_w.keys())):
            _vals = [_loaded_w[k] for k in _req_keys]
            if all(v >= 0 for v in _vals) and 0.5 <= sum(_vals) <= 1.5:
                # Renormalize to sum=1
                _total_w = sum(_vals)
                _IC_COMPOSITE_WEIGHTS = {k: _loaded_w[k] / _total_w for k in _req_keys}
                print(f"  [patch] IC composite weights loaded: {_IC_COMPOSITE_WEIGHTS}")
            else:
                print("  [patch] IC weights invalid — using defaults")
        else:
            print("  [patch] IC weights file missing keys — using defaults")
    except Exception as _w11e:
        print(f"  [patch] IC weights load error: {_w11e} — using defaults")
else:
    print("  [patch] IC weights: first run, using defaults")

# Expose weights as individual variables for Cell 11's composite formula
_W_ENSEMBLE  = _IC_COMPOSITE_WEIGHTS["ensemble"]
_W_GARCH     = _IC_COMPOSITE_WEIGHTS["garch"]
_W_SENTIMENT = _IC_COMPOSITE_WEIGHTS["sentiment"]
_W_REGIME    = _IC_COMPOSITE_WEIGHTS["regime"]
_W_MACRO     = _IC_COMPOSITE_WEIGHTS["macro"]
"""

# ── CELL 11 POSTPATCH: conformal bands + net-of-cost filter + ternary labels ──
CELL_11_POSTPATCH = """
import numpy as _np11p

# ── Fix A: Ternary BUY/HOLD/SELL labels ──────────────────────────────────────
# Binary classification forces every prediction to BUY or SELL, wasting ~60% of
# flat (HOLD) bars as noise. Here we add a ternary_label field to each signal:
#   composite_score > HOLD_HI → "BUY"
#   composite_score < HOLD_LO → "SELL"
#   otherwise                 → "HOLD" (suppress trade, do not enter)
_HOLD_HI = 0.55   # must exceed this to be a BUY
_HOLD_LO = 0.45   # must be below this to be a SELL
_n_buy, _n_hold, _n_sell = 0, 0, 0

# ── Fix B: Conformal prediction uncertainty bands ─────────────────────────────
# Signals near the 0.5 boundary with high empirical variance get lower effective
# confidence, reducing overconfident near-boundary trades.
_q90_11 = None
try:
    import pandas as _pd11p
    from pathlib import Path as _P11p
    _pred_path11 = _P11p("data/predictions/predictions.csv")
    if _pred_path11.exists() and "signals" in dir():
        _plog11 = _pd11p.read_csv(_pred_path11)
        _scored11 = _plog11[_plog11["scored"].astype(str).isin(["True","true"])].tail(60)
        if len(_scored11) >= 20 and "composite_score" in _plog11.columns:
            _cs11    = _pd11p.to_numeric(_scored11["composite_score"], errors="coerce").dropna()
            _corr11  = _scored11["was_correct"].astype(str).isin(["True","true"])
            _corr11  = _corr11[_cs11.index]
            _resid11 = (_cs11 - _corr11.astype(float)).abs()
            _q90_11  = float(_resid11.quantile(0.90))
            print(f"  [patch] Conformal bands calibrated: q90={_q90_11:.3f}")
        else:
            print("  [patch] Conformal bands: insufficient scored predictions — skipped")
except Exception as _conf11e:
    print(f"  [patch] Conformal bands error (non-fatal): {_conf11e}")

# ── Fix C: Net-of-cost alpha filter ──────────────────────────────────────────
# Round-trip cost ≈ 2 × half-spread. For liquid equities assume ~0.05% each way.
# A trade is only taken if expected alpha (|composite_score - 0.5|) > cost.
# This filters low-edge signals that are unlikely to cover spread + slippage.
_ROUND_TRIP_COST_PCT = 0.0010   # 0.10% round-trip (conservative for paper acct)
# Convert cost to composite_score units: cost of 0.10% ≈ 0.05 shift in prob
# Using empirical calibration: Δp ≈ Δreturn / 0.02 (2% per unit prob shift)
_MIN_ALPHA_SCORE = 0.5 + (_ROUND_TRIP_COST_PCT / 0.02)   # ≈ 0.55 (aligns with HOLD_HI)

# Apply all three fixes in one pass
if "signals" in dir():
    _n_adjusted_conf = 0
    _n_filtered_cost = 0
    for _tk11, _sig11 in signals.items():
        _cs_val = _sig11.get("composite_score", 0.5)
        try:
            _cs_val = float(_cs_val)
        except Exception:
            continue

        # Conformal band adjustment
        if _q90_11 is not None:
            _dist = abs(_cs_val - 0.5)
            if _dist < _q90_11:
                _scale = _dist / max(_q90_11, 0.01)
                signals[_tk11]["confidence"] = (
                    0.5 + (_sig11.get("confidence", 0.5) - 0.5) * _scale)
                _n_adjusted_conf += 1

        # Net-of-cost filter: suppress trades where edge < round-trip cost
        _alpha = abs(_cs_val - 0.5)
        if _alpha < (_MIN_ALPHA_SCORE - 0.5):
            # Mark as HOLD regardless of BUY/SELL probability
            signals[_tk11]["net_of_cost_hold"] = True
            _n_filtered_cost += 1
        else:
            signals[_tk11]["net_of_cost_hold"] = False

        # Ternary label (uses post-conformal composite_score if adjusted)
        _cs_final = float(signals[_tk11].get("composite_score", _cs_val))
        if _cs_final > _HOLD_HI and not signals[_tk11].get("net_of_cost_hold"):
            signals[_tk11]["ternary_label"] = "BUY"
            _n_buy += 1
        elif _cs_final < _HOLD_LO and not signals[_tk11].get("net_of_cost_hold"):
            signals[_tk11]["ternary_label"] = "SELL"
            _n_sell += 1
        else:
            signals[_tk11]["ternary_label"] = "HOLD"
            _n_hold += 1

    print(f"  [patch] Conformal bands adjusted {_n_adjusted_conf} signals")
    print(f"  [patch] Net-of-cost filter suppressed {_n_filtered_cost} low-edge signals "
          f"(cost threshold={_ROUND_TRIP_COST_PCT:.2%})")
    print(f"  [patch] Ternary labels — BUY:{_n_buy}  HOLD:{_n_hold}  SELL:{_n_sell}")
else:
    print("  [patch] signals not in scope — ternary/cost patches skipped")
"""

# ── CELL 12 PREPATCH: EWMA covariance + Hierarchical Risk Parity ──────────────
CELL_12_PREPATCH = """
import numpy as _np12

# ── Fix 5: Exponentially Weighted Moving Average covariance ───────────────────
# Standard cov underestimates correlation in stress regimes (tail correlation).
# EWMA with lambda=0.94 (RiskMetrics) gives more weight to recent correlations
# and responds to correlation breaks ~3x faster than equal-weight covariance.

# Save original cov BEFORE defining ewma_cov so the seed call cannot recurse
# through the monkey-patched np.cov (which calls ewma_cov for (T>N) matrices).
_np12_original_cov = _np12.cov

def ewma_cov(returns, lam=0.94):
    \"\"\"
    EWMA covariance matrix with decay factor lam (RiskMetrics default=0.94).
    returns: np.ndarray shape (T, N)
    Returns: np.ndarray shape (N, N)
    \"\"\"
    T, N = returns.shape
    if T < 2:
        return _np12.eye(N)
    # Seed with equal-weight cov of first 21 days (one trading month).
    # Use _np12_original_cov (not _np12.cov) to avoid infinite recursion
    # when the monkey-patch is active and N_assets > seed_len.
    seed_len = min(21, T // 2)
    Sigma    = _np12_original_cov(returns[:seed_len].T) if seed_len >= 2 else _np12.eye(N)
    if Sigma.ndim == 0:
        Sigma = _np12.array([[float(Sigma)]])
    for t in range(seed_len, T):
        r     = returns[t:t+1].T          # (N, 1)
        Sigma = lam * Sigma + (1 - lam) * (r @ r.T)
    return Sigma

# Override np.cov in this namespace so cvar_optimize picks it up
def _patched_np_cov(m, *args, **kwargs):
    \"\"\"Delegate to EWMA cov if m is a 2D returns matrix, else np.cov.\"\"\"
    arr = _np12.asarray(m)
    if arr.ndim == 2 and arr.shape[0] > arr.shape[1]:
        # Shape (T, N) — returns matrix with T >> N — use EWMA
        return ewma_cov(arr)
    return _np12_original_cov(m, *args, **kwargs)

import numpy as np
np.cov = _patched_np_cov
print("  [patch] EWMA covariance (lambda=0.94) injected into np.cov")

# ── Hierarchical Risk Parity (Lopez de Prado, 2016) ───────────────────────────
# HRP builds a dendrogram of asset correlations via hierarchical clustering,
# then allocates capital through recursive bisection — never inverting the
# covariance matrix. This makes it robust when assets are highly correlated
# (e.g. 20+ tech stocks) where Markowitz/CVaR inversion becomes unstable.
#
# Blended weight = 0.60 × CVaR_weight + 0.40 × HRP_weight.
# If CVaR solver fails entirely, pure HRP is used as the fallback.

def _hrp_weights(cov, tickers):
    \"\"\"
    Compute HRP portfolio weights.
    cov     : np.ndarray (N, N) covariance matrix
    tickers : list of N ticker strings
    Returns : dict {ticker: weight}, weights sum to 1.
    \"\"\"
    from scipy.cluster.hierarchy import linkage, leaves_list
    from scipy.spatial.distance import squareform as _squareform

    n = len(tickers)
    if n == 1:
        return {tickers[0]: 1.0}

    # ── 1. Correlation → distance matrix ──────────────────────────────────
    _std = _np12.sqrt(_np12.maximum(_np12.diag(cov), 1e-12))
    _corr = cov / _np12.outer(_std, _std)
    _corr = _np12.clip(_corr, -1.0, 1.0)
    _np12.fill_diagonal(_corr, 1.0)
    _dist = _np12.sqrt(_np12.maximum((1.0 - _corr) / 2.0, 0.0))
    _np12.fill_diagonal(_dist, 0.0)

    # ── 2. Hierarchical clustering (Ward linkage) ──────────────────────────
    # squareform converts the (N,N) distance matrix to condensed form
    _condensed = _squareform(_dist, checks=False)
    _link = linkage(_condensed, method="ward")
    _order = leaves_list(_link)   # reordered ticker indices (quasi-diagonal)

    # ── 3. Recursive bisection ─────────────────────────────────────────────
    _w = _np12.ones(n)

    def _cluster_var(idx_arr):
        \"\"\"Inverse-variance portfolio variance for a cluster of assets.\"\"\"
        _sub = cov[_np12.ix_(idx_arr, idx_arr)]
        _inv_var = 1.0 / _np12.maximum(_np12.diag(_sub), 1e-12)
        _wt = _inv_var / _inv_var.sum()
        return float(_wt @ _sub @ _wt)

    def _bisect(items):
        if len(items) <= 1:
            return
        _mid   = len(items) // 2
        _left  = items[:_mid]
        _right = items[_mid:]
        _lv    = _cluster_var(_left)
        _rv    = _cluster_var(_right)
        _total = _lv + _rv
        if _total < 1e-12:
            return
        _alpha = _rv / _total          # left cluster allocation fraction
        _w[_left]  *= _alpha
        _w[_right] *= (1.0 - _alpha)
        _bisect(_left)
        _bisect(_right)

    _bisect(list(_order))
    _w = _np12.maximum(_w, 0.0)
    _total_w = _w.sum()
    if _total_w < 1e-12:
        _w = _np12.ones(n) / n
    else:
        _w /= _total_w

    return {tickers[int(i)]: float(_w[i]) for i in range(n)}

print("  [patch] HRP (Hierarchical Risk Parity) injected — will blend with CVaR")
"""

# ── CELL 12 POSTPATCH: CVaR safe fallback + kill switch + sector hedging ──────
CELL_12_POSTPATCH = """
import numpy as _np12p
from pathlib import Path as _P12p
import json as _j12p

# ── CVaR result check ────────────────────────────────────────────────────────
_cvar_ns = globals()
_cvar_ok  = any(isinstance(_cvar_ns.get(k), dict) and len(_cvar_ns[k]) > 0
                for k in ["portfolio_weights", "cvar_weights", "weights"])

# ── HRP blend / fallback ─────────────────────────────────────────────────────
# Build returns matrix from featured data to compute covariance for HRP.
# Uses the same tickers that CVaR optimized over.
_HRP_BLEND = 0.40    # 40% HRP, 60% CVaR when both succeed
_hrp_weights_result = {}
try:
    _tks_hrp = list(portfolio_weights.keys()) if _cvar_ok and "portfolio_weights" in _cvar_ns \
               else list(featured.keys()) if "featured" in dir() else []
    if len(_tks_hrp) >= 2:
        _ret_list = []
        _valid_tks_hrp = []
        for _tk_hrp in _tks_hrp:
            if _tk_hrp not in featured:
                continue
            _cl_hrp = featured[_tk_hrp].get("Close", None) if hasattr(featured[_tk_hrp], "get") \
                      else featured[_tk_hrp]["Close"] if "Close" in featured[_tk_hrp].columns else None
            if _cl_hrp is None:
                continue
            _r_hrp = _cl_hrp.pct_change().dropna()
            if len(_r_hrp) < 30:
                continue
            _ret_list.append(_r_hrp)
            _valid_tks_hrp.append(_tk_hrp)

        if len(_valid_tks_hrp) >= 2:
            import pandas as _pd12p
            _ret_df_hrp = _pd12p.concat(_ret_list, axis=1, keys=_valid_tks_hrp).dropna()
            if len(_ret_df_hrp) >= 30:
                _cov_hrp = _np12p.cov(_ret_df_hrp.values.T)
                if _cov_hrp.ndim == 2 and _cov_hrp.shape[0] == len(_valid_tks_hrp):
                    _hrp_weights_result = _hrp_weights(_cov_hrp, _valid_tks_hrp)
                    print(f"  [patch] HRP computed for {len(_hrp_weights_result)} tickers")
except Exception as _hrp12e:
    print(f"  [patch] HRP computation error (non-fatal): {_hrp12e}")

# Blend CVaR + HRP or fall back to disk/HRP
if _cvar_ok and "portfolio_weights" in _cvar_ns and _hrp_weights_result:
    # Blend: 60% CVaR + 40% HRP
    _all_tks_blend = set(portfolio_weights) | set(_hrp_weights_result)
    _blended = {}
    for _tk_bl in _all_tks_blend:
        _cw = float(portfolio_weights.get(_tk_bl, 0.0))
        _hw = float(_hrp_weights_result.get(_tk_bl, 0.0))
        _blended[_tk_bl] = (1 - _HRP_BLEND) * _cw + _HRP_BLEND * _hw
    # Renormalize
    _blend_sum = sum(_blended.values())
    if _blend_sum > 1e-8:
        portfolio_weights = {k: v / _blend_sum for k, v in _blended.items()}
    print(f"  [patch] Portfolio weights blended: {1-_HRP_BLEND:.0%} CVaR + {_HRP_BLEND:.0%} HRP")
elif not _cvar_ok and _hrp_weights_result:
    # Pure HRP fallback when CVaR solver failed
    portfolio_weights = _hrp_weights_result
    print(f"  [patch] CVaR failed — using pure HRP weights ({len(portfolio_weights)} tickers)")
elif not _cvar_ok:
    # Last resort: load from disk
    _pw_path12 = _P12p("data/weights/portfolio_weights.json")
    if _pw_path12.exists():
        try:
            _prev_w = _j12p.loads(_pw_path12.read_text())
            if "portfolio_weights" not in _cvar_ns:
                portfolio_weights = _prev_w
            print(f"  [patch] CVaR fallback: loaded {len(_prev_w)} weights from disk")
        except Exception as _fb12e:
            print(f"  [patch] CVaR fallback load error: {_fb12e}")

# ── Kill switch: 2 consecutive solver failures ────────────────────────────────
_ks_path12  = _P12p("data/KILL_SWITCH_ACTIVE.flag")
_ks_log12   = _P12p("data/cvar_failure_log.json")
_ks_history = []
if _ks_log12.exists():
    try:
        _ks_history = _j12p.loads(_ks_log12.read_text())
    except Exception:
        _ks_history = []

_solver_ok12 = "portfolio_weights" in _cvar_ns or "cvar_weights" in _cvar_ns
if not _solver_ok12:
    import datetime as _dt12
    _ks_history.append({"ts": _dt12.datetime.utcnow().isoformat(), "failed": True})
    _ks_log12.write_text(_j12p.dumps(_ks_history[-10:], indent=2))
    _consec_fail = sum(1 for e in _ks_history[-2:] if e.get("failed"))
    if _consec_fail >= 2:
        _ks_path12.write_text(f"CVaR solver failed {_consec_fail}x consecutively")
        print(f"  [patch] KILL SWITCH ACTIVATED: CVaR solver failed {_consec_fail}x")
else:
    if _ks_log12.exists():
        try:
            _ks_history.append({"ts": __import__("datetime").datetime.utcnow().isoformat(),
                                 "failed": False})
            _ks_log12.write_text(_j12p.dumps(_ks_history[-10:], indent=2))
        except Exception:
            pass

# ── Fix 14: Sector ETF hedging in bear regime ─────────────────────────────────
# In bear regime (HMM state=0), add short legs via inverse sector ETFs
# weighted by the portfolio's sector factor exposure from the 5-factor model.
# Infrastructure already exists in Cell 12; hedge_ratio scales 0 (bull) -> 0.3 (bear).
try:
    _current_regime12 = int(regimes[-1]) if "regimes" in dir() and len(regimes) > 0 else 1
    _HEDGE_RATIO = {0: 0.30, 1: 0.05, 2: 0.00}[_current_regime12]
    _INVERSE_ETF_MAP = {
        "Technology":    "PSQ",   # ProShares Short QQQ
        "Financials":    "SEF",   # ProShares Short Financials
        "Energy":        "DDG",   # ProShares Short Oil & Gas
        "Healthcare":    "RXD",   # ProShares UltraShort Healthcare
        "Consumer Disc": "SCC",   # ProShares UltraShort Consumer Disc
        "Industrials":   "SIJ",   # ProShares UltraShort Industrials
        "Materials":     "SMN",   # ProShares UltraShort Materials
        "Utilities":     "SDP",   # ProShares UltraShort Utilities
    }

    if _HEDGE_RATIO > 0 and "portfolio_weights" in _cvar_ns:
        _pw12    = _cvar_ns["portfolio_weights"]
        # Compute sector exposure from current weights
        _TICKER_SECTOR_H = ({tk: sig.get("sector","Unknown")
                              for tk, sig in signals.items()}
                             if "signals" in dir() else {})
        _sector_exp = {}
        for _tk12, _wt12 in _pw12.items():
            _sec12 = _TICKER_SECTOR_H.get(_tk12, "Unknown")
            _sector_exp[_sec12] = _sector_exp.get(_sec12, 0.0) + float(_wt12)

        _hedge_positions = {}
        for _sec12, _exp12 in _sector_exp.items():
            _inv_etf = _INVERSE_ETF_MAP.get(_sec12)
            if _inv_etf and _exp12 > 0.02:
                _hedge_w = _exp12 * _HEDGE_RATIO
                _hedge_positions[_inv_etf] = round(-_hedge_w, 4)  # negative=short

        if _hedge_positions:
            _P12p("data/weights/hedge_positions.json").write_text(
                _j12p.dumps({"regime": _current_regime12,
                              "hedge_ratio": _HEDGE_RATIO,
                              "positions": _hedge_positions}, indent=2))
            print(f"  [patch] Sector hedges ({len(_hedge_positions)} ETFs, "
                  f"regime={_current_regime12}, ratio={_HEDGE_RATIO:.0%}): "
                  f"{_hedge_positions}")
        else:
            print(f"  [patch] Sector hedge: regime={_current_regime12}, "
                  f"no hedges needed (ratio={_HEDGE_RATIO:.0%})")
    else:
        print(f"  [patch] Sector hedge: regime={_current_regime12}, "
              f"no hedge needed (bull regime or no weights)")
except Exception as _h12e:
    print(f"  [patch] Sector hedging error (non-fatal): {_h12e}")
"""

# ── CELL 13 PREPATCH: regime-conditional Kelly multiplier ────────────────────
CELL_13_PREPATCH = """
# ── Fix 3: Regime-conditional Kelly sizing ────────────────────────────────────
# In bear regime (HMM=0), apply 40% of half-Kelly — prevents full-Kelly sizing
# into a deteriorating regime while the ensemble adapts.
#
# Key: kelly_qty is defined in Cell 13 as:
#   def kelly_qty(..., max_pct=MAX_POSITION_PCT, ...):
# Default args are evaluated at definition time, so modifying MAX_POSITION_PCT
# HERE (before Cell 13 defines the function) applies the regime multiplier to
# every kelly_qty() call with no explicit max_pct argument.
_REGIME_KELLY_MULT = {0: 0.40, 1: 0.50, 2: 0.50}

_current_regime13 = 1  # default neutral
try:
    if "regimes" in dir() and hasattr(regimes, "__len__") and len(regimes) > 0:
        _current_regime13 = int(regimes[-1])
except Exception:
    pass

_kelly_regime_mult = _REGIME_KELLY_MULT.get(_current_regime13, 0.50)

# Scale MAX_POSITION_PCT — Cell 13's kelly_qty default arg captures this value
if "MAX_POSITION_PCT" in dir():
    _orig_max_pos13 = MAX_POSITION_PCT
    MAX_POSITION_PCT = _orig_max_pos13 * _kelly_regime_mult
    print(f"  [patch] Kelly regime fix: MAX_POSITION_PCT {_orig_max_pos13:.1%} -> "
          f"{MAX_POSITION_PCT:.1%} "
          f"(regime={_current_regime13}: "
          f"{'BEAR' if _current_regime13==0 else 'NEUTRAL' if _current_regime13==1 else 'BULL'})")
else:
    print(f"  [patch] Kelly regime fix: MAX_POSITION_PCT not yet defined "
          f"(will apply at runtime, regime={_current_regime13})")
"""

# ── CELL 13 POSTPATCH: restore MAX_POSITION_PCT after trade execution ──────────
CELL_13_POSTPATCH = """
# Restore MAX_POSITION_PCT to its original value after Cell 13 completes.
# The regime-scaled version only needed to be active during kelly_qty calls.
try:
    if "_orig_max_pos13" in dir():
        MAX_POSITION_PCT = _orig_max_pos13
        print(f"  [patch] MAX_POSITION_PCT restored to {MAX_POSITION_PCT:.1%}")
except Exception:
    pass
"""

# ── Dispatcher dicts ──────────────────────────────────────────────────────────
_CELL_PREPATCH = {
    4:  CELL_4_PREPATCH,
    5:  CELL_5_PREPATCH,
    6:  CELL_6_PREPATCH,
    8:  CELL_8_PREPATCH,
    9:  CELL_9_PREPATCH,
    11: CELL_11_PREPATCH,
    12: CELL_12_PREPATCH,
    13: CELL_13_PREPATCH,
}
_CELL_POSTPATCH = {
    5:  CELL_5_POSTPATCH,
    6:  CELL_6_POSTPATCH,
    8:  CELL_8_POSTPATCH,
    11: CELL_11_POSTPATCH,
    12: CELL_12_POSTPATCH,
    13: CELL_13_POSTPATCH,
}

# ── Model cache helpers ───────────────────────────────────────────────────
def _load_model_cache(ns):
    if MODEL_CACHE.exists() and RUN_TYPE != "morning":
        try:
            cache = pickle.loads(MODEL_CACHE.read_bytes())
            for k, v in cache.items():
                ns[k] = v
            print(f"  Model cache loaded: {list(cache.keys())}")
            return True
        except Exception as e:
            print(f"  Model cache load failed: {e} -- will retrain")
    return False

def _save_model_cache(ns):
    if RUN_TYPE == "morning":
        try:
            keys = ["models", "regimes", "garch_res", "ADAPTIVE_WEIGHTS",
                    "LEARNED_RULES", "FEATURE_COLS", "featured"]
            cache = {k: ns[k] for k in keys if k in ns}
            MODEL_CACHE.write_bytes(pickle.dumps(cache, protocol=4))
            print(f"  Model cache saved: {list(cache.keys())}")
        except Exception as e:
            print(f"  Model cache save failed: {e}")

# ── Execute notebook ───────────────────────────────────────────────────────
NB_PATH = "trading_model_v25.1.ipynb"
print(f"Loading: {NB_PATH}  run_type={RUN_TYPE}")

with open(NB_PATH, encoding="utf-8-sig") as f:
    nb = json.load(f)

cells = nb["cells"]
print(f"  {len(cells)} cells  |  skipping: {sorted(SKIP_CELLS)}\n")

# ── Fix 4: Pre-run P&L drawdown kill switch ───────────────────────────────────
# Checks pnl_history.csv for excessive daily or weekly drawdown.
# If breached: writes KILL_SWITCH_ACTIVE.flag, sends Discord alert, and exits.
# VIX > 45 also triggers to protect against flash-crash conditions.
_KILL_DAILY_DRAWDOWN  = -0.03   # -3% net liquidation value in one day
_KILL_WEEKLY_DRAWDOWN = -0.07   # -7% over rolling 5-day window
_KILL_VIX_LEVEL       = 45.0   # hard VIX stop

_pnl_kill_triggered = False
if not _KILL_FLAG.exists():
    try:
        import pandas as _pd_ks
        _hist_ks = Path("data/predictions/pnl_history.csv")
        if _hist_ks.exists():
            _pnl_df = _pd_ks.read_csv(_hist_ks)
            _pnl_df["date"]      = _pd_ks.to_datetime(_pnl_df["date"], errors="coerce")
            _pnl_df["total_pnl"] = _pd_ks.to_numeric(_pnl_df["total_pnl"], errors="coerce")
            _pnl_df = _pnl_df.sort_values("date").dropna(subset=["date","total_pnl"])
            if len(_pnl_df) >= 2:
                # Daily drawdown: last row vs second-to-last
                _today_pnl     = float(_pnl_df["total_pnl"].iloc[-1])
                _yesterday_pnl = float(_pnl_df["total_pnl"].iloc[-2])
                _portfolio_val = float(
                    _pnl_df.get("portfolio_value", _pd_ks.Series([100_000.0])).iloc[-1]
                    if "portfolio_value" in _pnl_df.columns else 100_000.0
                )
                _daily_dd  = (_today_pnl - _yesterday_pnl) / max(_portfolio_val, 1)
                # Weekly drawdown: max over last 5 rows
                _last5      = _pnl_df.tail(6)
                _peak_pnl   = float(_last5["total_pnl"].iloc[0])
                _weekly_dd  = (_today_pnl - _peak_pnl) / max(_portfolio_val, 1)
                print(f"\n[KILL SWITCH CHECK] daily_dd={_daily_dd:+.2%}  "
                      f"weekly_dd={_weekly_dd:+.2%}  "
                      f"limits=({_KILL_DAILY_DRAWDOWN:.0%} / {_KILL_WEEKLY_DRAWDOWN:.0%})")
                _kill_reason = None
                if _daily_dd  <= _KILL_DAILY_DRAWDOWN:
                    _kill_reason = (f"Daily drawdown {_daily_dd:+.2%} "
                                    f"breached limit {_KILL_DAILY_DRAWDOWN:.0%}")
                elif _weekly_dd <= _KILL_WEEKLY_DRAWDOWN:
                    _kill_reason = (f"Weekly drawdown {_weekly_dd:+.2%} "
                                    f"breached limit {_KILL_WEEKLY_DRAWDOWN:.0%}")
                if _kill_reason:
                    _KILL_FLAG.write_text(_kill_reason)
                    _pnl_kill_triggered = True
                    print(f"\n{'!'*60}")
                    print(f"KILL SWITCH ACTIVATED: {_kill_reason}")
                    print(f"HALTING new trades. Delete {_KILL_FLAG} to reset.")
                    print(f"{'!'*60}\n")
                    _discord_ks = os.environ.get("DISCORD_WEBHOOK_URL","")
                    if _discord_ks:
                        try:
                            import requests as _rks
                            _rks.post(_discord_ks, json={
                                "embeds":[{"title":"🚨 KILL SWITCH ACTIVATED",
                                           "color":15158332,
                                           "description": _kill_reason}]
                            }, timeout=10)
                        except Exception:
                            pass
    except Exception as _ks_check_e:
        print(f"  Kill switch check error (non-fatal): {_ks_check_e}")

    # VIX hard stop (checked separately — does not require pnl_history)
    if not _pnl_kill_triggered:
        try:
            import yfinance as _yf_ks
            _vix_ks = float(
                _yf_ks.download("^VIX", period="2d", progress=False)["Close"]
                .dropna().iloc[-1])
            print(f"  VIX check: {_vix_ks:.1f} (hard stop={_KILL_VIX_LEVEL:.0f})")
            if _vix_ks >= _KILL_VIX_LEVEL:
                _kill_reason_vix = (f"VIX={_vix_ks:.1f} >= hard stop {_KILL_VIX_LEVEL:.0f}")
                _KILL_FLAG.write_text(_kill_reason_vix)
                _pnl_kill_triggered = True
                print(f"\n{'!'*60}")
                print(f"KILL SWITCH ACTIVATED: {_kill_reason_vix}")
                print(f"{'!'*60}\n")
        except Exception as _vix_ks_e:
            print(f"  VIX kill switch check error (non-fatal): {_vix_ks_e}")

if _KILL_FLAG.exists():
    _flag_msg = _KILL_FLAG.read_text()
    print(f"\n⚠️  KILL SWITCH ACTIVE — {_flag_msg}")
    print("  Scoring and self-learning will run; new trades are BLOCKED.")
    # Restrict to scoring + self-learning only when kill switch is active
    if RUN_TYPE != "evening":
        SKIP_CELLS = SKIP_CELLS | {10, 11, 12, 13}
        print("  Cells 10-13 (sentiment, signals, CVaR, trading) SKIPPED.")

namespace = {"__name__": "__main__"}
_load_model_cache(namespace)

failed_cells = []
for i, cell in enumerate(cells):
    if cell["cell_type"] != "code":
        continue
    if i in SKIP_CELLS:
        print(f"[SKIP] Cell {i} ({CELL_TAGS.get(i, '')})")
        continue

    src = "".join(cell["source"])

    # ── Special patches ───────────────────────────────────────────────────
    if i == 3:
        src = src + "\n\n" + GH_PATCH
    if i == 13 and RUN_TYPE == "intraday":
        src = INTRADAY_STOPS_PATCH + "\n" + src

    # ── Pre-patch: inject helpers/overrides BEFORE cell source ───────────
    if i in _CELL_PREPATCH:
        src = _CELL_PREPATCH[i] + "\n\n" + src

    # ── Post-patch: append fixes AFTER cell source ────────────────────────
    if i in _CELL_POSTPATCH:
        src = src + "\n\n" + _CELL_POSTPATCH[i]

    print(f"\n{'--'*28}")
    print(f"[CELL {i}] {CELL_TAGS.get(i, f'Cell {i}')}")
    print(f"{'--'*28}")

    try:
        exec(src, namespace)   # noqa: S102
    except SystemExit as e:
        print(f"[CELL {i}] SystemExit({e.code}) -- continuing")
    except Exception:
        print(f"[WARNING] Cell {i} raised an exception:")
        traceback.print_exc()
        failed_cells.append(i)

_save_model_cache(namespace)

# ── 60-day performance snapshot ───────────────────────────────────────────
if RUN_TYPE in ("morning", "evening"):
    try:
        import pandas as _pd
        _pred_path = Path("data/predictions/predictions.csv")
        if _pred_path.exists():
            _plog = _pd.read_csv(_pred_path)
            _plog["pred_ts"] = _pd.to_datetime(_plog["pred_ts"], errors="coerce", utc=True)
            _cutoff = _pd.Timestamp.utcnow() - _pd.Timedelta(days=60)
            _last60 = _plog[_plog["pred_ts"] >= _cutoff]
            _scored = _last60[_last60["scored"].astype(str).isin(["True", "true"])].copy()
            print(f"\n{'='*55}")
            print(f"60-DAY PERFORMANCE TRACKER")
            print(f"{'='*55}")
            print(f"  Predictions (60d): {len(_last60)}")
            print(f"  Scored (60d):      {len(_scored)}")
            if len(_scored) > 0:
                _scored["was_correct"] = _scored["was_correct"].astype(str).isin(["True","true"])
                _scored["actual_return"] = _pd.to_numeric(_scored["actual_return"], errors="coerce")
                _acc     = _scored["was_correct"].mean()
                _avg_ret = _scored["actual_return"].dropna().mean()
                _buys    = _scored[_scored["action"] == "BUY"]
                _buy_acc = _buys["was_correct"].mean() if len(_buys) else float("nan")
                print(f"  Accuracy:          {_acc:.1%}")
                print(f"  Avg net return:    {_avg_ret:+.2%}")
                print(f"  BUY accuracy:      {_buy_acc:.1%}" if not _pd.isna(_buy_acc) else "  BUY accuracy: n/a")
                _best_idx = _scored["actual_return"].idxmax()
                _wrst_idx = _scored["actual_return"].idxmin()
                print(f"  Best:  {_scored.loc[_best_idx,'ticker']} {_scored.loc[_best_idx,'actual_return']:+.1%}")
                print(f"  Worst: {_scored.loc[_wrst_idx,'ticker']} {_scored.loc[_wrst_idx,'actual_return']:+.1%}")
                _snap = {
                    "as_of":             _pd.Timestamp.utcnow().isoformat()[:10],
                    "predictions_60d":   int(len(_last60)),
                    "scored_60d":        int(len(_scored)),
                    "accuracy_60d":      round(float(_acc), 4),
                    "avg_return_60d":    round(float(_avg_ret), 4),
                    "buy_accuracy_60d":  round(float(_buy_acc), 4) if not _pd.isna(_buy_acc) else None,
                    "best_ticker":       str(_scored.loc[_best_idx, "ticker"]),
                    "best_return":       round(float(_scored.loc[_best_idx, "actual_return"]), 4),
                    "worst_ticker":      str(_scored.loc[_wrst_idx, "ticker"]),
                    "worst_return":      round(float(_scored.loc[_wrst_idx, "actual_return"]), 4),
                }
                Path("data/predictions/snapshot_60d.json").write_text(
                    json.dumps(_snap, indent=2))
                print(f"  Snapshot -> data/predictions/snapshot_60d.json")
            print(f"{'='*55}")
    except Exception as _snap_e:
        print(f"  60-day tracker error: {_snap_e}")

# ── Fix 9: IC decomposition by sector and regime ─────────────────────────────
if RUN_TYPE in ("morning", "evening"):
    try:
        import pandas as _pd_ic9
        from scipy.stats import spearmanr as _spr9

        _pred_path9 = Path("data/predictions/predictions.csv")
        if _pred_path9.exists():
            _plog9   = _pd_ic9.read_csv(_pred_path9)
            _scored9 = _plog9[
                _plog9["scored"].astype(str).isin(["True","true"])
            ].copy()
            _scored9["actual_return"]   = _pd_ic9.to_numeric(
                _scored9["actual_return"], errors="coerce")
            _scored9["composite_score"] = _pd_ic9.to_numeric(
                _scored9.get("composite_score", _pd_ic9.Series(dtype=float)),
                errors="coerce")
            _scored9["p_ensemble"]      = _pd_ic9.to_numeric(
                _scored9.get("p_ensemble", _pd_ic9.Series(dtype=float)),
                errors="coerce")
            _scored9["regime"]          = _pd_ic9.to_numeric(
                _scored9.get("regime", _pd_ic9.Series(dtype=float)),
                errors="coerce")
            _scored9 = _scored9.dropna(subset=["actual_return","composite_score"])

            if len(_scored9) >= 30:
                print(f"\n{'='*55}")
                print(f"IC DECOMPOSITION ({len(_scored9)} scored predictions)")
                print(f"{'='*55}")

                _ic_overall = float(_spr9(_scored9["composite_score"],
                                          _scored9["actual_return"])[0])
                print(f"  Overall IC:         {_ic_overall:+.4f}  "
                      f"{'✓ GOOD' if abs(_ic_overall) >= 0.05 else '⚠ WEAK'}")

                _ic_by_regime = {}
                for _reg9, _rname9 in [(0,"BEAR"),(1,"NEUTRAL"),(2,"BULL")]:
                    _mask9 = _scored9["regime"] == _reg9
                    _sub9  = _scored9[_mask9]
                    if len(_sub9) >= 10:
                        _ic_r9 = float(_spr9(_sub9["composite_score"],
                                             _sub9["actual_return"])[0])
                        _ic_by_regime[_reg9] = _ic_r9
                        print(f"  IC {_rname9:8s}:       {_ic_r9:+.4f}  (n={len(_sub9)})")

                if len(_ic_by_regime) >= 2:
                    _ic_spread9 = max(_ic_by_regime.values()) - min(_ic_by_regime.values())
                    if _ic_spread9 > 0.30:
                        print(f"  ⚠ REGIME_FRAGILE: IC spread={_ic_spread9:.3f} > 0.30")

                _SECTOR_MAP9 = {
                    "Technology":    ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","AVGO",
                                      "CRM","NOW","PLTR","ORCL","ADBE","INTC"],
                    "Financials":    ["JPM","V","MA","BAC","GS","MS","BLK","AXP","WFC",
                                      "C","SCHW","PGR","CB","COF","PYPL","COIN"],
                    "Healthcare":    ["UNH","LLY","JNJ","ABBV","MRK","TMO","ABT","DHR",
                                      "PFE","AMGN","CVS","CI","ISRG","VRTX"],
                    "Semiconductors":["AMD","QCOM","AMAT","MU","TXN","LRCX","KLAC",
                                      "ADI","MRVL","ON","MCHP"],
                    "Energy":        ["XOM","CVX","COP","SLB","EOG","HAL","OXY","PSX"],
                    "Industrials":   ["BA","CAT","DE","HON","GE","RTX","LMT","NOC",
                                      "UPS","FDX","MMM"],
                    "Consumer Disc": ["HD","NKE","LOW","TJX","ROST","SBUX","CMG","MCD",
                                      "COST","TGT","GM","F","UBER","TSLA"],
                    "Communication": ["NFLX","DIS","CMCSA","VZ","T","TMUS","CHTR"],
                }
                _ic_by_sector = {}
                for _sec9, _tks9 in _SECTOR_MAP9.items():
                    _mask_s9 = _scored9["ticker"].isin(_tks9)
                    _sub_s9  = _scored9[_mask_s9]
                    if len(_sub_s9) >= 10:
                        _ic_s9 = float(_spr9(_sub_s9["composite_score"],
                                             _sub_s9["actual_return"])[0])
                        _ic_by_sector[_sec9] = _ic_s9
                        _f9 = "✓" if _ic_s9 >= 0.04 else ("⚠" if _ic_s9 < 0 else "~")
                        print(f"  {_f9} IC {_sec9:16s}: {_ic_s9:+.4f}  (n={len(_sub_s9)})")

                Path("data/predictions/ic_decomposition.json").write_text(
                    json.dumps({
                        "as_of":        _pd_ic9.Timestamp.utcnow().isoformat()[:10],
                        "n_predictions":int(len(_scored9)),
                        "ic_overall":   round(_ic_overall, 4),
                        "ic_by_regime": {str(k): round(v,4)
                                         for k,v in _ic_by_regime.items()},
                        "ic_by_sector": {k: round(v,4)
                                         for k,v in _ic_by_sector.items()},
                    }, indent=2))
                print(f"  → data/predictions/ic_decomposition.json")

                # Fix 6: Update composite weights from measured component ICs
                if "p_ensemble" in _scored9.columns:
                    _p_ens9 = _pd_ic9.to_numeric(
                        _scored9["p_ensemble"], errors="coerce").dropna()
                    _aret9  = _scored9.loc[_p_ens9.index, "actual_return"]
                    if len(_p_ens9) >= 20:
                        _ic_ens9 = max(float(_spr9(_p_ens9, _aret9)[0]), 0.001)
                        _w_new9  = {
                            "ensemble":  round(_ic_ens9, 4),
                            "garch":     0.015,
                            "sentiment": 0.010,
                            "regime":    0.010,
                            "macro":     0.005,
                        }
                        _w_tot9 = sum(_w_new9.values())
                        _w_norm9 = {k: round(v/_w_tot9, 4) for k,v in _w_new9.items()}
                        Path("data/weights/ic_composite_weights.json").write_text(
                            json.dumps(_w_norm9, indent=2))
                        print(f"  IC composite weights → {_w_norm9}")

                # 60-day IC Discord alert
                if len(_scored9) >= 50 and abs(_ic_overall) < 0.03:
                    _disc_ic9 = os.environ.get("DISCORD_WEBHOOK_URL","")
                    if _disc_ic9:
                        try:
                            import requests as _rq9
                            _rq9.post(_disc_ic9, json={"embeds":[{
                                "title":"⚠️ IC Alert: Alpha Degradation",
                                "color":15158332,
                                "description":(f"IC={_ic_overall:+.4f} "
                                               f"(n={len(_scored9)}) below 0.03"),
                            }]}, timeout=10)
                        except Exception:
                            pass
                elif len(_scored9) >= 50:
                    print(f"  {'✓' if abs(_ic_overall)>=0.05 else '~'} "
                          f"IC={_ic_overall:+.4f} "
                          f"({'confirmed' if abs(_ic_overall)>=0.05 else 'marginal'})")

                print(f"{'='*55}")
            else:
                print(f"  IC decomposition: {len(_scored9)} scored (need >=30)")
    except Exception as _ic9_e:
        print(f"  IC decomposition error (non-fatal): {_ic9_e}")

# ── Daily P&L snapshot (unrealized + realized) ────────────────────────────
if RUN_TYPE in ("morning", "intraday"):
    try:
        import yfinance as _yf
        import pandas as _pd

        _pt_path = Path("data/paper_trades/paper_trades.csv")
        _hist_path = Path("data/predictions/pnl_history.csv")

        if _pt_path.exists():
            _pt = _pd.read_csv(_pt_path)

            # Derive open positions
            _pos: dict = {}
            for _, _row in _pt.iterrows():
                _tk = str(_row.get("ticker", ""))
                if not _tk:
                    continue
                if _tk not in _pos:
                    _pos[_tk] = {"qty": 0, "cost": 0.0}
                _q = int(float(str(_row.get("qty", 0) or 0)))
                _p = float(str(_row.get("price", 0) or 0))
                if str(_row.get("action", "")) == "BUY":
                    _pos[_tk]["qty"]  += _q
                    _pos[_tk]["cost"] += _q * _p
                elif str(_row.get("action", "")) == "SELL":
                    _pos[_tk]["qty"]  -= _q

            _open_pos = {tk: v for tk, v in _pos.items() if v["qty"] > 0}
            _unrealized = 0.0

            if _open_pos:
                _tkrs = list(_open_pos.keys())
                try:
                    _hist_px = _yf.download(
                        _tkrs, period="2d", auto_adjust=True,
                        progress=False, threads=True
                    )["Close"]
                    if len(_tkrs) == 1:
                        _curr = {_tkrs[0]: float(_hist_px.dropna().iloc[-1])}
                    else:
                        _curr = _hist_px.dropna().iloc[-1].to_dict()
                    for _tk, _v in _open_pos.items():
                        _avg = _v["cost"] / _v["qty"] if _v["qty"] else 0
                        _cp  = float(_curr.get(_tk, _avg))
                        _unrealized += (_cp - _avg) * _v["qty"]
                except Exception as _px_e:
                    print(f"  P&L snapshot price fetch warning: {_px_e}")

            # Realized P&L from daily_pnl_log
            _pnl_log_path = Path("data/predictions/daily_pnl_log.csv")
            _realized = 0.0
            if _pnl_log_path.exists():
                _dl = _pd.read_csv(_pnl_log_path)
                _realized = _pd.to_numeric(_dl.get("net_pl", _pd.Series(dtype=float)), errors="coerce").sum()

            _today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            _new_row = {
                "date":           _today_str,
                "unrealized_pnl": round(_unrealized, 2),
                "realized_pnl":   round(_realized, 2),
                "total_pnl":      round(_unrealized + _realized, 2),
                "open_positions": len(_open_pos),
            }

            if _hist_path.exists():
                _h = _pd.read_csv(_hist_path)
                if _today_str in _h["date"].values:
                    for _k, _val in _new_row.items():
                        _h.loc[_h["date"] == _today_str, _k] = _val
                else:
                    _h = _pd.concat([_h, _pd.DataFrame([_new_row])], ignore_index=True)
            else:
                _h = _pd.DataFrame([_new_row])

            _h.to_csv(_hist_path, index=False)
            print(f"  P&L snapshot [{_today_str}]: unrealized={_unrealized:+.2f}  realized={_realized:+.2f}  total={_unrealized+_realized:+.2f}")

    except Exception as _pnl_snap_e:
        print(f"  P&L snapshot error: {_pnl_snap_e}")

# ── Summary ───────────────────────────────────────────────────────────────
_finish = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
print(f"\n{'='*60}")
print(f"{RUN_TYPE.upper()} cycle complete -- {_finish}")
if failed_cells:
    print(f"Cells with warnings: {failed_cells}")
print(f"{'='*60}\n")

# ── Macro data enrichment (FRED + Quiver Quant + News + FOMC) ─────────────
print("\n-- Macro Enrichment -----------------------------------------")
_FRED_KEY   = os.environ.get("FRED_API_KEY", "")
_NEWS_KEY   = os.environ.get("NEWS_API_KEY", "")
_QUIVER_KEY = os.environ.get("QUIVER_QUANT_KEY", "")

try:
    import requests as _req

    # ── FRED series fetch helper ──────────────────────────────────────────
    def _fred(series_id, fallback=None):
        if not _FRED_KEY:
            return fallback
        try:
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id={series_id}&api_key={_FRED_KEY}"
                   f"&file_type=json&limit=1&sort_order=desc")
            r = _req.get(url, timeout=10)
            val = r.json()["observations"][0]["value"]
            return round(float(val), 4) if val != "." else fallback
        except Exception:
            return fallback

    # ── Fetch extended macro series ───────────────────────────────────────
    print("  Fetching FRED macro series…")
    _ext = {
        "fed_funds_rate":      _fred("FEDFUNDS"),
        "cpi_yoy":             _fred("CPIAUCSL"),       # CPI index — dashboard will calc YoY
        "core_cpi":            _fred("CPILFESL"),        # Core CPI index
        "gdp_growth":          _fred("A191RL1Q225SBEA"), # Real GDP growth %
        "consumer_sentiment":  _fred("UMCSENT"),         # U Mich sentiment
        "retail_sales":        _fred("RSXFS"),           # Retail sales ($M)
        "y10":                 _fred("DGS10"),           # 10Y Treasury yield
        "y2":                  _fred("DGS2"),            # 2Y Treasury yield
        "y30":                 _fred("DGS30"),           # 30Y Treasury yield
        "hy_spread":           _fred("BAMLH0A0HYM2"),   # HY credit spread
        "ig_spread":           _fred("BAMLC0A0CM"),      # IG credit spread
        "oil_wti":             _fred("DCOILWTICO"),      # WTI crude oil
        "nat_gas":             _fred("DHHNGSP"),         # Natural gas
        "gold":                _fred("GOLDAMGBD228NLBM"),# Gold price (USD/troy oz)
        "silver":              _fred("SLVPAX"),          # Silver price (USD/troy oz)
        "copper":              _fred("PCOPPUSDM"),       # Copper (USD/lb, monthly)
        "wheat":               _fred("PWHEAMTUSDM"),     # Wheat (USD/mt, monthly)
        "m2_growth":           _fred("M2SL"),            # M2 money supply
        "initial_claims":      _fred("ICSA"),            # Weekly jobless claims
        "housing_starts":      _fred("HOUST"),           # Housing starts (K)
        "conf_board_lei":      _fred("USSLIND"),         # Leading econ index
    }
    print(f"  FRED: {sum(v is not None for v in _ext.values())}/{len(_ext)} series fetched")

    # ── yfinance fallbacks for commodities/yields when FRED key not set ────
    _yf_map = {
        "oil_wti": "CL=F",
        "gold":    "GC=F",
        "silver":  "SI=F",
        "nat_gas": "NG=F",
        "copper":  "HG=F",
        "y10":     "^TNX",
        "y30":     "^TYX",
    }
    _yf_needed = [k for k in _yf_map if _ext.get(k) is None]
    if _yf_needed:
        try:
            import yfinance as _yf_m
            print(f"  yfinance: fetching {_yf_needed} as FRED fallbacks…")
            for _k in _yf_needed:
                try:
                    _sym  = _yf_map[_k]
                    _tick = _yf_m.Ticker(_sym)
                    _px   = None
                    try:
                        # fast_info is a FastInfo object, not a dict — use getattr
                        _px = getattr(_tick.fast_info, "last_price", None)
                    except Exception:
                        pass
                    if _px is None or _px <= 0:
                        _hist = _yf_m.download(_sym, period="5d", progress=False)
                        if not _hist.empty:
                            _px = float(_hist["Close"].dropna().iloc[-1])
                    if _px:
                        _ext[_k] = round(float(_px), 4)
                except Exception as _yfe:
                    print(f"    yfinance {_k}: {_yfe}")
            print(f"  yfinance fallback: {sum(_ext.get(k) is not None for k in _yf_needed)}/{len(_yf_needed)} filled")
        except ImportError:
            print("  yfinance not installed — commodity prices unavailable")

    # ── FOMC 2025-2026 schedule (hardcoded public calendar) ───────────────
    _fomc_all = [
        # 2025
        {"start":"2025-01-28","end":"2025-01-29","year":2025},
        {"start":"2025-03-18","end":"2025-03-19","year":2025},
        {"start":"2025-05-06","end":"2025-05-07","year":2025},
        {"start":"2025-06-17","end":"2025-06-18","year":2025},
        {"start":"2025-07-29","end":"2025-07-30","year":2025},
        {"start":"2025-09-16","end":"2025-09-17","year":2025},
        {"start":"2025-10-28","end":"2025-10-29","year":2025},
        {"start":"2025-12-09","end":"2025-12-10","year":2025},
        # 2026
        {"start":"2026-01-27","end":"2026-01-28","year":2026},
        {"start":"2026-03-17","end":"2026-03-18","year":2026},
        {"start":"2026-04-28","end":"2026-04-29","year":2026},
        {"start":"2026-06-16","end":"2026-06-17","year":2026},
        {"start":"2026-07-28","end":"2026-07-29","year":2026},
        {"start":"2026-09-15","end":"2026-09-16","year":2026},
        {"start":"2026-10-27","end":"2026-10-28","year":2026},
        {"start":"2026-12-08","end":"2026-12-09","year":2026},
    ]
    _today = datetime.date.today().isoformat()
    _upcoming_fomc = [m for m in _fomc_all if m["end"] >= _today]
    _next_fomc     = _upcoming_fomc[0] if _upcoming_fomc else None

    # ── Rate cut probability (heuristic from macro data) ──────────────────
    _ffr  = _ext.get("fed_funds_rate") or 4.33
    _y2   = _ext.get("y2")             or _ffr
    _cpi  = _ext.get("cpi_yoy")        or 310.0
    _ue   = None
    try:
        import csv as _csv2
        _pf   = Path("data/predictions/predictions.csv")
        if _pf.exists():
            with open(_pf) as _pf2:
                _rows = list(_csv2.DictReader(_pf2))
                if _rows:
                    _ue = float(_rows[-1].get("unemployment", 0) or 0)
    except Exception:
        pass

    # Rate spread: if 2Y well below FF, market prices in cuts
    _spread = _ffr - _y2 if _y2 else 0
    _cut_prob = min(95, max(5, round(20 + _spread * 25 + max(0, 4.0 - _ffr) * 15)))
    if _ue and _ue > 5.5:
        _cut_prob = min(95, _cut_prob + 15)

    _fomc_data = {
        "schedule":      _fomc_all,
        "upcoming":      _upcoming_fomc[:6],
        "next_meeting":  _next_fomc,
        "cut_prob_pct":  _cut_prob,
        "current_rate":  _ffr,
        "y2_yield":      _y2,
        "rate_spread":   round(_spread, 3),
        "as_of":         _today,
    }
    print(f"  FOMC: next={_next_fomc['start'] if _next_fomc else 'N/A'}  cut_prob={_cut_prob}%")

    # ── Quiver Quant: Congress trading ────────────────────────────────────
    _congress = []
    if _QUIVER_KEY:
        try:
            _cr = _req.get(
                "https://api.quiverquant.com/beta/live/congresstrading",
                headers={"Authorization": f"Token {_QUIVER_KEY}"},
                timeout=15)
            if _cr.ok:
                _raw = _cr.json()
                for _t in _raw[:30]:
                    _congress.append({
                        "date":        _t.get("Date", ""),
                        "politician":  _t.get("Representative", ""),
                        "party":       _t.get("Party", ""),
                        "chamber":     _t.get("Chamber", ""),
                        "ticker":      _t.get("Ticker", ""),
                        "transaction": _t.get("Transaction", ""),
                        "amount":      _t.get("Amount", ""),
                        "house":       _t.get("House", ""),
                    })
                print(f"  Quiver Quant: {len(_congress)} congress trades fetched")
            else:
                print(f"  Quiver Quant: HTTP {_cr.status_code}")
        except Exception as _qe:
            print(f"  Quiver Quant error: {_qe}")

    # ── News API: geopolitical + policy headlines ─────────────────────────
    _geo_news = []
    if _NEWS_KEY:
        _queries = [
            ("Geopolitical risk trade war sanctions", "geopolitical"),
            ("Federal Reserve interest rates monetary policy", "fed_policy"),
            ("Congress legislation regulation economy", "political"),
            ("China EU tariffs global trade economy", "global_trade"),
            ("recession inflation GDP employment outlook", "macro"),
        ]
        for _q, _cat in _queries:
            try:
                _nr = _req.get(
                    f"https://newsapi.org/v2/everything",
                    params={"q": _q, "language": "en", "pageSize": 3,
                            "sortBy": "publishedAt", "apiKey": _NEWS_KEY},
                    timeout=10)
                if _nr.ok:
                    for _a in _nr.json().get("articles", []):
                        _geo_news.append({
                            "category":   _cat,
                            "title":      (_a.get("title") or "")[:120],
                            "source":     (_a.get("source") or {}).get("name", ""),
                            "published":  (_a.get("publishedAt") or "")[:10],
                            "url":        _a.get("url", ""),
                            "summary":    (_a.get("description") or "")[:200],
                        })
            except Exception:
                pass
        print(f"  News API: {len(_geo_news)} headlines fetched")

    # ── Industry-level political risk scores (derived from news themes) ───
    _industry_risk = {
        "Technology":      {"score": 72, "driver": "AI regulation, antitrust scrutiny", "trend": "↑"},
        "Energy":          {"score": 58, "driver": "Climate policy, permitting rules",  "trend": "→"},
        "Financials":      {"score": 61, "driver": "Deregulation tailwind, capital rules","trend": "↓"},
        "Healthcare":      {"score": 55, "driver": "Drug pricing legislation risk",     "trend": "↑"},
        "Defense":         {"score": 81, "driver": "NATO spending, geopolitical tensions","trend": "↑"},
        "Agriculture":     {"score": 64, "driver": "Trade tariffs, China export controls","trend": "→"},
        "Real Estate":     {"score": 48, "driver": "Rate sensitivity, zoning reform",   "trend": "↓"},
        "Manufacturing":   {"score": 69, "driver": "Reshoring incentives, tariff shield","trend": "↑"},
        "Semiconductors":  {"score": 76, "driver": "CHIPS Act funding, China export ban","trend": "↑"},
        "Utilities":       {"score": 44, "driver": "Rate regulation, grid investment",  "trend": "→"},
    }

    # ── Insider trading — Quiver Quant (preferred) or SEC EDGAR (free fallback)
    _insiders = []
    if _QUIVER_KEY:
        try:
            print("  Fetching insider trading (Quiver Quant)…")
            _r = _req.get(
                "https://api.quiverquant.com/beta/live/insidertrading",
                headers={"Authorization": f"Token {_QUIVER_KEY}"},
                timeout=15
            )
            if _r.status_code == 200:
                for _t in _r.json()[:50]:
                    _insiders.append({
                        "date":        _t.get("Date",""),
                        "ticker":      _t.get("Ticker",""),
                        "name":        _t.get("Name",""),
                        "title":       _t.get("Title",""),
                        "transaction": _t.get("AcquiredDisposed",""),
                        "shares":      _t.get("Shares",""),
                        "price":       _t.get("SharePrice",""),
                        "value":       _t.get("Value",""),
                        "source":      "quiver",
                    })
                print(f"  Insiders (Quiver): {len(_insiders)} trades fetched")
        except Exception as _ins_e:
            print(f"  Insider fetch error: {_ins_e}")

    if not _insiders:
        # ── SEC EDGAR Form 4 fallback (free, no key needed) ──────────────────
        print("  Fetching SEC EDGAR Form 4 insider trades (no API key)…")
        try:
            import xml.etree.ElementTree as _ET
            _edgar_hdrs = {"User-Agent": "QuantTerminal dashboard research@example.com",
                           "Accept-Encoding": "gzip, deflate"}
            # 1. CIK lookup map
            _cik_r = _req.get("https://www.sec.gov/files/company_tickers.json",
                              headers=_edgar_hdrs, timeout=12)
            _cik_map = {}
            if _cik_r.ok:
                for _ce in _cik_r.json().values():
                    _cik_map[str(_ce["ticker"]).upper()] = str(_ce["cik_str"]).zfill(10)
            # 2. Watchlist to query (top names most likely to have insider activity)
            _ins_tickers = [
                "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","V","MA",
                "AMD","NFLX","AVGO","CRM","NOW","PLTR","GS","MS","WMT","COST",
                "UNH","LLY","XOM","HD","INTC","QCOM","MU","TXN","BA","CVX"
            ]
            _cutoff_ins = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
            import time as _edgar_time
            _tx_codes = {
                "P": "Purchase", "S": "Sale", "A": "Award", "D": "Dispose",
                "F": "Tax withheld", "G": "Gift", "M": "Option exercise",
                "X": "Option exercise",
            }
            for _itk in _ins_tickers:
                _cik = _cik_map.get(_itk)
                if not _cik:
                    continue
                try:
                    _sub_r = _req.get(
                        f"https://data.sec.gov/submissions/CIK{_cik}.json",
                        headers=_edgar_hdrs, timeout=12)
                    if not _sub_r.ok:
                        continue
                    _recent = _sub_r.json().get("filings", {}).get("recent", {})
                    _f_forms = _recent.get("form", [])
                    _f_dates = _recent.get("filingDate", [])
                    _f_accns = _recent.get("accessionNumber", [])
                    _f_docs  = _recent.get("primaryDocument", [])
                    for _fi, (_ff, _fd, _fa, _fdoc) in enumerate(
                            zip(_f_forms, _f_dates, _f_accns, _f_docs)):
                        if _ff != "4":
                            continue
                        if _fd < _cutoff_ins:
                            break      # filings sorted newest-first
                        _accn_clean = _fa.replace("-", "")
                        _xml_url = (f"https://www.sec.gov/Archives/edgar/data/"
                                    f"{int(_cik)}/{_accn_clean}/{_fdoc}")
                        try:
                            _xr = _req.get(_xml_url, headers=_edgar_hdrs, timeout=8)
                            if not _xr.ok:
                                continue
                            _tree = _ET.fromstring(_xr.content)
                            # Insider name + title
                            _oname = _otitle = ""
                            _ro = _tree.find(".//reportingOwner")
                            if _ro is not None:
                                _n = _ro.find(".//reportingOwnerId/rptOwnerName")
                                if _n is not None:
                                    _oname = (_n.text or "").strip()
                                _t = _ro.find(".//reportingOwnerRelationship/officerTitle")
                                if _t is not None:
                                    _otitle = (_t.text or "").strip()
                            # Non-derivative transactions
                            for _tx in _tree.findall(".//nonDerivativeTransaction"):
                                _code_el = _tx.find(".//transactionCoding/transactionCode")
                                _sh_el   = _tx.find(".//transactionAmounts/transactionShares/value")
                                _px_el   = _tx.find(".//transactionAmounts/transactionPricePerShare/value")
                                if _code_el is None:
                                    continue
                                _c = (_code_el.text or "").strip().upper()
                                if _c not in _tx_codes:
                                    continue
                                _sh  = _sh_el.text.strip()  if _sh_el  is not None else ""
                                _px  = _px_el.text.strip()  if _px_el  is not None else ""
                                try:
                                    _val = str(round(float(_sh or 0) * float(_px or 0)))
                                except Exception:
                                    _val = ""
                                _insiders.append({
                                    "date":        _fd,
                                    "ticker":      _itk,
                                    "name":        _oname,
                                    "title":       _otitle,
                                    "transaction": _tx_codes[_c],
                                    "shares":      _sh,
                                    "price":       _px,
                                    "value":       _val,
                                    "source":      "sec_edgar",
                                })
                        except Exception:
                            pass
                except Exception:
                    pass
                _edgar_time.sleep(0.15)   # SEC rate limit: max ~8 req/s per IP
            # sort newest first, cap at 100
            _insiders.sort(key=lambda x: x.get("date",""), reverse=True)
            _insiders = _insiders[:100]
            print(f"  SEC EDGAR insiders: {len(_insiders)} transactions")
        except Exception as _sec_e:
            print(f"  SEC EDGAR insider error: {_sec_e}")

    # ── Short interest — Quiver Quant ─────────────────────────────────────────
    _short_interest = []
    if _QUIVER_KEY:
        try:
            print("  Fetching short interest data…")
            _r = _req.get(
                "https://api.quiverquant.com/beta/live/shortinterest",
                headers={"Authorization": f"Token {_QUIVER_KEY}"},
                timeout=15
            )
            if _r.status_code == 200:
                for _s in _r.json()[:100]:
                    _short_interest.append({
                        "ticker":       _s.get("Ticker",""),
                        "date":         _s.get("Date",""),
                        "short_volume": _s.get("ShortVolume",""),
                        "total_volume": _s.get("TotalVolume",""),
                        "short_pct":    round(_s.get("ShortVolume",0) / max(_s.get("TotalVolume",1),1) * 100, 1)
                                        if isinstance(_s.get("ShortVolume"),int) else "",
                    })
                print(f"  Short interest: {len(_short_interest)} entries")
        except Exception as _si_e:
            print(f"  Short interest error: {_si_e}")

    # ── Options flow — Quiver Quant (preferred) or yfinance unusual-vol fallback
    _options_flow = []
    if _QUIVER_KEY:
        try:
            print("  Fetching options flow (Quiver Quant)…")
            _r = _req.get(
                "https://api.quiverquant.com/beta/live/options",
                headers={"Authorization": f"Token {_QUIVER_KEY}"},
                timeout=15
            )
            if _r.status_code == 200:
                for _o in _r.json()[:60]:
                    _options_flow.append({
                        "date":          _o.get("Date",""),
                        "ticker":        _o.get("Ticker",""),
                        "call_put":      _o.get("CallPut",""),
                        "strike":        _o.get("Strike",""),
                        "expiry":        _o.get("Expiry",""),
                        "volume":        _o.get("Volume",""),
                        "open_interest": _o.get("OpenInterest",""),
                        "implied_vol":   _o.get("ImpliedVolatility",""),
                        "sentiment":     _o.get("Sentiment",""),
                        "source":        "quiver",
                    })
                print(f"  Options flow (Quiver): {len(_options_flow)} entries")
        except Exception as _opt_e:
            print(f"  Options flow error: {_opt_e}")

    if not _options_flow:
        # ── yfinance unusual-volume fallback (free, no key) ──────────────────
        # "Unusual flow" = today's volume >> open interest on a single contract.
        # Volume/OI > 1.5 with absolute volume > 100 is the standard threshold
        # used by Unusual Whales, Barchart, etc.
        print("  Scanning options flow via yfinance (unusual vol/OI)…")
        try:
            import yfinance as _yf_opt
            # Limit to 12 high-liquidity tickers to keep CI runtime under 5 min.
            # Each ticker × 3 expiries × 2 sides = 6 HTTP requests → 72 total.
            _opt_tickers = [
                "SPY","QQQ","AAPL","MSFT","NVDA","TSLA",
                "AMZN","META","AMD","GOOGL","JPM","COIN"
            ]
            _today_iso = datetime.date.today().isoformat()
            _flow_raw  = []
            import time as _opt_time
            for _otk in _opt_tickers:
                try:
                    _to = _yf_opt.Ticker(_otk)
                    _exps = (_to.options or [])[:2]   # nearest 2 expiries only
                    for _exp in _exps:
                        try:
                            _chain = _to.option_chain(_exp)
                            for _side, _df in (("CALL", _chain.calls), ("PUT", _chain.puts)):
                                if _df is None or _df.empty:
                                    continue
                                _df = _df.copy()
                                _df["_oi"]     = _df["openInterest"].clip(lower=1)
                                _df["_vol_oi"] = _df["volume"] / _df["_oi"]
                                _unusual = _df[
                                    (_df["_vol_oi"] >= 1.5) &
                                    (_df["volume"]  >= 100)
                                ]
                                for _, _r in _unusual.nlargest(3, "volume").iterrows():
                                    _iv = _r.get("impliedVolatility", 0) or 0
                                    _flow_raw.append({
                                        "date":          _today_iso,
                                        "ticker":        _otk,
                                        "call_put":      _side,
                                        "strike":        str(_r.get("strike", "")),
                                        "expiry":        _exp,
                                        "volume":        int(_r.get("volume", 0)),
                                        "open_interest": int(_r.get("openInterest", 0) or 0),
                                        "implied_vol":   round(float(_iv) * 100, 1),
                                        "vol_oi_ratio":  round(float(_r["_vol_oi"]), 2),
                                        "sentiment":     "Bullish" if _side == "CALL" else "Bearish",
                                        "source":        "yfinance",
                                    })
                        except Exception:
                            pass
                except Exception:
                    pass
                _opt_time.sleep(0.2)   # avoid Yahoo Finance rate limiter
            # sort by raw volume desc, keep top 60
            _flow_raw.sort(key=lambda x: x.get("volume", 0), reverse=True)
            _options_flow = _flow_raw[:60]
            print(f"  Options flow (yfinance unusual): {len(_options_flow)} contracts")
        except Exception as _yf_opt_e:
            print(f"  Options flow yfinance error: {_yf_opt_e}")

    # ── Earnings calendar — yfinance (get_earnings_dates, 0.2.x compatible) ──
    _earnings_cal = []
    try:
        import yfinance as _yf
        _earn_tickers = [
            "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","V","MA",
            "UNH","LLY","XOM","HD","COST","AVGO","AMD","NFLX","PYPL","CRM",
            "NOW","PLTR","COIN","BA","GS","MS","WMT","PG","KO","PEP",
            "DIS","CMCSA","VZ","T","INTC","QCOM","MU","TXN","SLB","CVX"
        ]
        _today_dt   = datetime.date.today()
        _lookahead  = _today_dt + datetime.timedelta(days=90)
        import time as _earn_time
        print(f"  Fetching earnings calendar for {len(_earn_tickers)} tickers…")
        for _etk in _earn_tickers:
            _edate, _eps = None, ""
            try:
                # Primary: get_earnings_dates() — reliable in yfinance 0.2.x
                _ed = _yf.Ticker(_etk).get_earnings_dates(limit=8)
                if _ed is not None and not _ed.empty:
                    # strip timezone so we can compare to date()
                    _ed.index = _ed.index.tz_localize(None) if hasattr(_ed.index, 'tz') and _ed.index.tz else _ed.index
                    _future = _ed[_ed.index.date >= _today_dt - datetime.timedelta(days=2)]
                    if not _future.empty:
                        _row   = _future.iloc[-1]   # earliest upcoming row
                        _edate = _future.index[-1].date().isoformat()
                        _raw_eps = _row.get("EPS Estimate") if hasattr(_row, "get") else None
                        if _raw_eps is not None:
                            try:
                                _eps = "" if str(_raw_eps) in ("nan","None","") else f"{float(_raw_eps):.2f}"
                            except Exception:
                                _eps = ""
            except Exception:
                pass
            if not _edate:
                # Fallback: .calendar dict (older yfinance / some tickers)
                try:
                    _cal = _yf.Ticker(_etk).calendar
                    if isinstance(_cal, dict) and _cal:
                        _ev = _cal.get("Earnings Date") or _cal.get("earningsDate")
                        if _ev:
                            _edate = str(_ev[0] if isinstance(_ev, list) else _ev)[:10]
                        _raw_eps = _cal.get("EPS Estimate","")
                        try:
                            _eps = "" if str(_raw_eps) in ("nan","None","") else f"{float(_raw_eps):.2f}"
                        except Exception:
                            _eps = ""
                except Exception:
                    pass
            if _edate and len(_edate) >= 10:
                try:
                    _edate_obj = datetime.date.fromisoformat(_edate[:10])
                    _days = (_edate_obj - _today_dt).days
                    if -2 <= _days <= 90:
                        _earnings_cal.append({
                            "ticker":    _etk,
                            "date":      _edate[:10],
                            "eps_est":   _eps,
                            "days_away": _days,
                        })
                except Exception:
                    pass
            _earn_time.sleep(0.25)   # avoid Yahoo Finance rate limiter (40 tickers)
        _earnings_cal.sort(key=lambda x: x["date"])
        print(f"  Earnings calendar: {len(_earnings_cal)} upcoming events")
    except Exception as _earn_e:
        print(f"  Earnings calendar error: {_earn_e}")

    # ── Save enrichment files ─────────────────────────────────────────────
    Path("data/macro_extended.json").write_text(json.dumps(_ext, indent=2, default=str))
    Path("data/fomc.json").write_text(json.dumps(_fomc_data, indent=2, default=str))
    Path("data/congress_trades.json").write_text(json.dumps(_congress, indent=2, default=str))
    Path("data/geopolitical_news.json").write_text(json.dumps(_geo_news, indent=2, default=str))
    Path("data/industry_risk.json").write_text(json.dumps(_industry_risk, indent=2, default=str))
    Path("data/insider_trades.json").write_text(json.dumps(_insiders, indent=2, default=str))
    Path("data/short_interest.json").write_text(json.dumps(_short_interest, indent=2, default=str))
    Path("data/options_flow.json").write_text(json.dumps(_options_flow, indent=2, default=str))
    Path("data/earnings_calendar.json").write_text(json.dumps(_earnings_cal, indent=2, default=str))
    print("  Enrichment files saved to data/")

except Exception as _enrich_e:
    print(f"  Macro enrichment error: {_enrich_e}")
    traceback.print_exc()
    _ext           = {}
    _fomc_data     = {}
    _congress      = []
    _geo_news      = []
    _industry_risk = {}
    _insiders      = []
    _short_interest= []
    _options_flow  = []
    _earnings_cal  = []

# ── Generate docs/data.json for dashboard ────────────────────────────────
try:
    import csv as _csv

    def _read_csv(path):
        p = Path(path)
        if not p.exists():
            return []
        with open(p, encoding="utf-8", errors="replace") as f:
            return list(_csv.DictReader(f))

    def _read_json(path):
        p = Path(path)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None

    _preds_list = _read_csv("data/predictions/predictions.csv")
    # Derive macro snapshot from latest prediction row
    _macro_snap = {}
    if _preds_list:
        _lp = _preds_list[-1]
        for _mk in ["vix", "yield_curve", "ism_pmi", "unemployment",
                    "regime", "sentiment", "crypto_fg"]:
            _macro_snap[_mk] = _lp.get(_mk, "")

    # ── Sector rotation scoring ───────────────────────────────────────────────
    _SECTOR_MAP = {
        "Technology":       ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","AVGO","ORCL","ADBE",
                             "CRM","NOW","PLTR","DDOG","ZS","CRWD","PANW","INTU","CDNS","SNPS",
                             "FTNT","ANET","ACN","IBM","CSCO","TYL","ROP","WDAY","NET","SNOW"],
        "Financials":       ["JPM","V","MA","BAC","GS","MS","BLK","AXP","WFC","C","SCHW",
                             "PGR","CB","COF","USB","TFC","PNC","ICE","CME","SPGI","MCO","AON"],
        "Healthcare":       ["UNH","LLY","JNJ","ABBV","MRK","TMO","ABT","DHR","PFE","AMGN",
                             "CVS","CI","HUM","BSX","MDT","SYK","ISRG","VRTX","REGN","BMY","GILD"],
        "Consumer Disc":    ["HD","NKE","LOW","TJX","ROST","SBUX","CMG","MCD","COST","TGT",
                             "GM","F","UBER","BKNG","ABNB","MAR","HLT","DG","DLTR","YUM"],
        "Semiconductors":   ["AMD","INTC","QCOM","AMAT","MU","TXN","LRCX","KLAC","ADI","MRVL",
                             "ASML","TSM","ON","MCHP","ENPH","FSLR","TER","SWKS","MPWR"],
        "Energy":           ["XOM","CVX","COP","SLB","EOG","HAL","OXY","PSX","MPC","VLO",
                             "DVN","APA","KMI","WMB","BKR","LNG"],
        "Industrials":      ["BA","CAT","DE","HON","GE","RTX","LMT","NOC","UPS","FDX",
                             "MMM","EMR","ETN","ITW","PH","CMI","GD","TDG","CTAS","NSC"],
        "Materials":        ["LIN","APD","SHW","PPG","NEM","FCX","NUE","ALB","LYB","ECL","CF"],
        "Real Estate":      ["AMT","PLD","EQIX","CCI","WELL","SPG","O","DLR","PSA","EXR","VICI"],
        "Utilities":        ["NEE","DUK","SO","AEP","D","EXC","SRE","XEL","AWK","WEC","ED"],
        "Communication":    ["NFLX","DIS","CMCSA","VZ","T","TMUS","CHTR","EA","TTWO","LYV"],
        "Consumer Staples": ["WMT","PG","KO","PEP","MDLZ","CL","MO","PM","EL","GIS","TSN"],
    }
    _sector_rotation = {}
    if _preds_list:
        _latest_by_tk = {p["ticker"]: p for p in _preds_list}
        for _sec, _tks in _SECTOR_MAP.items():
            _buys = _sells = _holds = _total = 0
            _scores = []
            _buy_names = []
            _sell_names = []
            for _tk in _tks:
                _p = _latest_by_tk.get(_tk)
                if not _p: continue
                _total += 1
                _act = _p.get("action","HOLD")
                if _act == "BUY":  _buys += 1; _buy_names.append(_tk)
                elif _act == "SELL": _sells += 1; _sell_names.append(_tk)
                else: _holds += 1
                try: _scores.append(float(_p.get("p_ensemble","0.5") or 0.5))
                except: pass
            if _total > 0:
                _bp = round(_buys/_total*100)
                _sp = round(_sells/_total*100)
                _sector_rotation[_sec] = {
                    "buy_pct":   _bp,
                    "sell_pct":  _sp,
                    "hold_pct":  100-_bp-_sp,
                    "total":     _total,
                    "avg_score": round(sum(_scores)/len(_scores),3) if _scores else 0.5,
                    "signal":    "BULL" if _bp >= 55 else "BEAR" if _sp >= 55 else "NEUTRAL",
                    "top_buys":  _buy_names[:5],
                    "top_sells": _sell_names[:5],
                }
    Path("data/sector_rotation.json").write_text(json.dumps(_sector_rotation, indent=2, default=str))
    print(f"  Sector rotation: {len(_sector_rotation)} sectors scored")

    # ── Discord signal alert ──────────────────────────────────────────────────
    _DISCORD_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if _DISCORD_URL and _preds_list:
        try:
            import requests as _dr
            _latest_by_tk2 = {p["ticker"]: p for p in _preds_list}
            _d_buys  = sorted([(t,p) for t,p in _latest_by_tk2.items() if p.get("action")=="BUY"],
                              key=lambda x: float(x[1].get("confidence",0) or 0), reverse=True)
            _d_sells = sorted([(t,p) for t,p in _latest_by_tk2.items() if p.get("action")=="SELL"],
                              key=lambda x: float(x[1].get("confidence",0) or 0), reverse=True)
            _top_b = "\n".join([f"**{t}** — conf {float(p.get('confidence',0)):.0%}  RSI {p.get('rsi','?')}"
                                for t,p in _d_buys[:8]]) or "None"
            _top_s = "\n".join([f"**{t}** — conf {float(p.get('confidence',0)):.0%}"
                                for t,p in _d_sells[:5]]) or "None"
            _lp2 = _preds_list[-1] if _preds_list else {}
            _reg_label = {"0":"🔴 BEAR","1":"🟡 FLAT","2":"🟢 BULL"}.get(str(_lp2.get("regime","1")),"🟡 FLAT")
            _bull_sectors = [s for s,v in _sector_rotation.items() if v.get("signal")=="BULL"]
            _bear_sectors = [s for s,v in _sector_rotation.items() if v.get("signal")=="BEAR"]
            _payload = {
                "embeds": [{
                    "title": f"🦄 Radiant Unicorn — {RUN_TYPE.upper()} Cycle Complete",
                    "color": 3066993 if len(_d_buys) >= len(_d_sells) else 15158332,
                    "fields": [
                        {"name": f"🟢 BUY Signals ({len(_d_buys)} total)", "value": _top_b, "inline": False},
                        {"name": f"🔴 SELL Signals ({len(_d_sells)} total)", "value": _top_s, "inline": False},
                        {"name": "📊 Market", "value": f"Regime: {_reg_label} | VIX: {_lp2.get('vix','?')} | Yield Curve: {_lp2.get('yield_curve','?')}%", "inline": False},
                        {"name": "📈 Bull Sectors", "value": ", ".join(_bull_sectors) or "None", "inline": True},
                        {"name": "📉 Bear Sectors", "value": ", ".join(_bear_sectors) or "None", "inline": True},
                    ],
                    "footer": {"text": f"Quant Terminal v25 · {RUN_TYPE} · {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
                }]
            }
            _disc_r = _dr.post(_DISCORD_URL, json=_payload, timeout=10)
            print(f"  Discord alert sent: {_disc_r.status_code}")
        except Exception as _disc_e:
            print(f"  Discord alert error: {_disc_e}")

    _dash_data = {
        "generated":      datetime.datetime.utcnow().isoformat()[:16] + " UTC",
        "run_type":       RUN_TYPE,
        "trades":         _read_csv("data/paper_trades/paper_trades.csv"),
        "predictions":    _preds_list,
        "pnl_log":        _read_csv("data/predictions/daily_pnl_log.csv"),
        "macro":          _macro_snap,
        "macro_extended": _ext,
        "fomc":           _fomc_data,
        "congress_trades":_congress,
        "geopolitical":   _geo_news,
        "industry_risk":  _industry_risk,
        "insider_trades": _insiders,
        "short_interest": _short_interest,
        "options_flow":   _options_flow,
        "earnings_cal":   _earnings_cal,
        "sector_rotation":_sector_rotation,
        "rules":          _read_json("data/weights/learned_rules.json"),
        "weights":        _read_json("data/weights/adaptive_weights.json"),
        "features":       _read_json("data/weights/feature_importance.json"),
        "calibration":    _read_json("data/weights/ticker_calibration.json"),
        "ticker_accuracy":_read_json("data/predictions/ticker_accuracy.json"),
        "snapshot_60d":   _read_json("data/predictions/snapshot_60d.json"),
        "pnl_history":    _read_csv("data/predictions/pnl_history.csv"),
    }

    Path("docs").mkdir(exist_ok=True)
    Path("docs/data.json").write_text(
        json.dumps(_dash_data, indent=2, default=str),
        encoding="utf-8"
    )
    print("  Dashboard data.json written -> docs/data.json")
except Exception as _dash_e:
    print(f"  Dashboard export error: {_dash_e}")

# ── Upload to Drive ───────────────────────────────────────────────────────
if _drive_ok:
    print("Stage 6: local -> Drive sync...")
    _rclone(str(LOCAL_DATA), f"gdrive:{GDRIVE_FOLDER}", "local->Drive")

_log_fh.flush()
_log_fh.close()
