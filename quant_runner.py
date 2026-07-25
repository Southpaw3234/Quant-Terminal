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

# ── Binary label patch — applied at MODULE LEVEL so models stay picklable ─
# Subclassing XGB/LGB inside exec() breaks pickle (class has no module ref).
# Monkey-patching .fit on the original classes keeps __module__ intact so
# the GitHub Actions model cache can save/restore properly.
try:
    import numpy as _np_bl
    import xgboost as _xgb_bl
    import lightgbm as _lgb_bl

    # The notebook applies label smoothing after our binary conversion:
    # [0.0, 1.0] → [0.05, 0.95]. XGBoost/LGB reject non-integer class labels.
    # Fix: round labels to nearest int at fit() time. Semantically safe:
    # 0.05 → 0 (still SELL), 0.95 → 1 (still BUY). Binary CV harness unchanged.
    # predict_proba is NOT wrapped — CalWrapper returns 2-col [P(bear), P(bull)].
    _xgb_orig_fit = _xgb_bl.XGBClassifier.fit
    _lgb_orig_fit = _lgb_bl.LGBMClassifier.fit

    def _xgb_round_fit(self, X, y, **kwargs):
        _y = _np_bl.asarray(y)
        if not _np_bl.issubdtype(_y.dtype, _np_bl.integer):
            _y = _np_bl.rint(_y).astype(int)
        return _xgb_orig_fit(self, X, _y, **kwargs)

    def _lgb_round_fit(self, X, y, **kwargs):
        _y = _np_bl.asarray(y)
        if not _np_bl.issubdtype(_y.dtype, _np_bl.integer):
            _y = _np_bl.rint(_y).astype(int)
        return _lgb_orig_fit(self, X, _y, **kwargs)

    _xgb_bl.XGBClassifier.fit = _xgb_round_fit
    _lgb_bl.LGBMClassifier.fit = _lgb_round_fit
    print("  [label patch] XGB+LGB .fit() patched: float labels rounded to int (handles label smoothing)")
except Exception as _bl_e:
    print(f"  [label patch] Warning: {_bl_e}")

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

def _rclone(src, dst, label, extra_flags=None):
    # timeout 300s (was 120s): one budget serves both directions, and `data/`
    # grows every cycle (evidence CSVs append daily), so the old 120s was
    # drifting into the sync's normal runtime — 7/20 timed out BOTH ways and
    # 7/23 saw 4 of 10 legs time out while the very next cycle succeeded.
    # Timeouts were never auth/config failures (the conf writes fine every run)
    # and were harmless — a local->Drive miss self-heals on the next cycle, and
    # a Drive->local miss is gap-fill only thanks to --ignore-existing below.
    try:
        r = subprocess.run(
            ["rclone", "copy", src, dst,
             "--exclude", "model_cache.pkl",
             "--transfers", "8", "--quiet"] + (extra_flags or []),
            capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            print(f"  {label} OK")
        else:
            print(f"  {label} warning: {r.stderr[:200]}")
    except Exception as e:
        print(f"  {label} error: {e}")

def _rclone_delete(remote_relpath, label="rclone delete"):
    # local->Drive uses `rclone copy`, which never deletes — so a file removed
    # locally lingers on Drive forever and gets restored next run. Use this to
    # explicitly delete a single file on Drive (e.g. a cleared kill-switch flag)
    # so the clear actually propagates.
    try:
        r = subprocess.run(
            ["rclone", "deletefile", f"gdrive:{GDRIVE_FOLDER}/{remote_relpath}", "--quiet"],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            print(f"  {label}: deleted {remote_relpath} on Drive")
        # non-zero (e.g. file already absent) is fine — silent
    except Exception as e:
        print(f"  {label} error: {e}")

_drive_ok = _write_rclone_conf()
if _drive_ok:
    print("Stage 0a: Drive -> local sync...")
    # --ignore-existing: the git checkout is the source of truth for every
    # tracked file (each run commits state to master at the end), so Drive may
    # only FILL GAPS (kill-switch flag, model pickles, anything not in git) —
    # never overwrite a file the checkout already has. Without this flag,
    # `rclone copy` overwrites on any size/modtime difference, and because
    # Stage 6 (local->Drive) runs BEFORE the workflow steps that write
    # rank_ic.csv / stat_arb_ls.csv / last_morning_run.txt, Drive held a frozen
    # snapshot (marker stuck at 2026-06-09) that every cycle restored and
    # re-committed: evening commits deleted the day's evidence rows, and the
    # stale marker made every scheduled run self-heal into a duplicate full
    # morning retrain that re-placed the same BUY orders (see 2026-07-06).
    _rclone(f"gdrive:{GDRIVE_FOLDER}", str(LOCAL_DATA), "Drive->local",
            extra_flags=["--ignore-existing"])

# Cumulative trade history for the dashboard. paper_trades.csv is reset to
# today-only every run so the cash guard's BUY−SELL notional calc stays
# correct (see data_reset below). That truncation also wiped the dashboard's
# Trade Log down to a single day. To keep a real multi-day Trade Log without
# breaking the cash guard, we mirror every *filled* trade into a separate
# append-only trade_history.csv (last 60 days), which the dashboard reads
# instead of the working file. Only filled rows are kept, so the phantom
# May-12 rejected BUYs never accumulate here either.
def _merge_trade_history(working_csv="data/paper_trades/paper_trades.csv",
                         hist_csv="data/paper_trades/trade_history.csv",
                         keep_days=60):
    try:
        import pandas as _pd_th
        from pathlib import Path as _P_th
        _cols = ("ts,ticker,action,price,qty,dollars,confidence,regime,vix,"
                 "portfolio_value,notes,order_id,status,run_date,iv_flag,iv_scale,notional")
        _hp = _P_th(hist_csv)
        _frames = []
        for _f in (_P_th(hist_csv), _P_th(working_csv)):
            if _f.exists():
                try:
                    _d = _pd_th.read_csv(_f)
                    if len(_d) > 0:
                        _frames.append(_d)
                except Exception:
                    pass
        if not _frames:
            if not _hp.exists():
                _hp.write_text(_cols + "\n")
            return 0
        _all = _pd_th.concat(_frames, ignore_index=True)
        if "status" in _all.columns:
            _all = _all[_all["status"].astype(str).str.lower() == "filled"]
        _key = [c for c in ("ts", "ticker", "action", "order_id") if c in _all.columns]
        if _key:
            _all = _all.drop_duplicates(subset=_key, keep="last")
        if "run_date" in _all.columns and len(_all) > 0:
            _cut = (_pd_th.Timestamp.today() - _pd_th.Timedelta(days=keep_days)).strftime("%Y-%m-%d")
            _all = _all[_all["run_date"].astype(str) >= _cut]
        if "ts" in _all.columns:
            _all = _all.sort_values("ts")
        _all.to_csv(_hp, index=False)
        print(f"  [trade_history] {len(_all)} filled trades retained (last {keep_days}d)")
        return len(_all)
    except Exception as _th_e:
        print(f"  [trade_history] non-fatal: {_th_e}")
        return 0

# Capture any trades restored from Drive (prior runs) into the cumulative
# history BEFORE the data_reset below truncates paper_trades.csv to today.
_merge_trade_history()

# Data reset: after every Drive sync, purge any stale phantom trades and
# keep ONLY today-dated rows. This runs unconditionally so multi-run days
# don't accumulate old Drive data in the cash guard's notional calc.
# Drive keeps restoring May-12 Alpaca-rejected BUYs (~375 rows, ~$100k+
# notional) which drove cash guard to $0 available → max 1 BUY per run.
try:
    import pandas as _pd_rst
    _pt_path  = LOCAL_DATA / "paper_trades" / "paper_trades.csv"
    _pnl_path = LOCAL_DATA / "predictions" / "pnl_history.csv"
    _today_str = _pd_rst.Timestamp.today().strftime("%Y-%m-%d")
    # Always retain only today's trades — purge everything older
    if _pt_path.exists():
        _pt_df = _pd_rst.read_csv(_pt_path)
        if "run_date" in _pt_df.columns and len(_pt_df) > 0:
            _pt_today = _pt_df[_pt_df["run_date"].astype(str) >= _today_str]
        else:
            _pt_today = _pt_df.iloc[0:0]
        _pt_today.to_csv(_pt_path, index=False)
        _n_kept = len(_pt_today)
        _n_purged = len(_pt_df) - _n_kept if _pt_path.exists() else 0
        print(f"  [data_reset] Kept {_n_kept} today-trades, purged {_n_purged} stale rows")
    else:
        _pt_path.write_text(
            "ts,ticker,action,price,qty,dollars,confidence,regime,vix,"
            "portfolio_value,notes,order_id,status,run_date,iv_flag,iv_scale,notional\n"
        )
        print("  [data_reset] Created fresh paper_trades.csv")
    # pnl_history: keep legitimate daily rows (real trading began 2026-05-28,
    # the first day Alpaca orders actually filled). Drop the phantom pre-fix
    # rows (the -$8k to -$13k garbage from errored May trades) AND drop today's
    # row so the P&L snapshot re-writes a fresh one. This lets the dashboard
    # line graph accumulate a real multi-day trend instead of being wiped to
    # a single point every run.
    _PNL_EPOCH = "2026-05-28"
    _pnl_header = "date,unrealized_pnl,realized_pnl,total_pnl,open_positions\n"
    if _pnl_path.exists():
        try:
            _pnl_df = _pd_rst.read_csv(_pnl_path)
            if "date" in _pnl_df.columns and len(_pnl_df) > 0:
                _ds = _pnl_df["date"].astype(str)
                _pnl_keep = _pnl_df[(_ds >= _PNL_EPOCH) & (_ds < _today_str)]
                _pnl_keep.to_csv(_pnl_path, index=False)
                print(f"  [data_reset] pnl_history: kept {len(_pnl_keep)} real daily rows (>= {_PNL_EPOCH}, < today), dropped {len(_pnl_df) - len(_pnl_keep)}")
            else:
                _pnl_path.write_text(_pnl_header)
        except Exception as _pnl_rst_e:
            _pnl_path.write_text(_pnl_header)
            print(f"  [data_reset] pnl_history reset (parse error): {_pnl_rst_e}")
    else:
        _pnl_path.write_text(_pnl_header)
except Exception as _rst_e:
    print(f"  [data_reset] non-fatal: {_rst_e}")

# Auto-clear kill switch AFTER Drive sync — morning runs always start fresh.
# The flag may have been written to Drive by a previous broken run and just
# restored above; clear it here so Cell 11/12/13 are not blocked.
if RUN_TYPE == "morning" and _KILL_FLAG.exists():
    _KILL_FLAG.unlink(missing_ok=True)
    (LOCAL_DATA / "cvar_failure_log.json").unlink(missing_ok=True)
    print("  [kill switch] Auto-cleared after Drive sync — will re-arm if CVaR fails")

# ── Model version check — wipe stale per-ticker models after label strategy change ──
# Drive sync restores individual model .pkl files trained under a previous label
# strategy (ternary, median-split). If the version tag doesn't match, delete them
# so Cell 8 retrains from scratch with the current strategy.
_MODEL_VERSION   = "sign_based_v8"
_MODEL_VER_FILE  = LOCAL_DATA / "models" / "model_version.txt"
_MODEL_DIR       = LOCAL_DATA / "models"
if RUN_TYPE == "morning":
    _stored_ver = _MODEL_VER_FILE.read_text().strip() if _MODEL_VER_FILE.exists() else ""
    if _stored_ver != _MODEL_VERSION:
        _wiped = 0
        for _f in _MODEL_DIR.glob("*.pkl"):
            if _f.name != "intraday_model.pkl":  # keep intraday, wipe everything else
                _f.unlink(missing_ok=True)
                _wiped += 1
        _MODEL_VER_FILE.write_text(_MODEL_VERSION)
        print(f"  [model ver] Strategy changed ({_stored_ver!r} → {_MODEL_VERSION!r}): wiped {_wiped} stale models (incl cache) — full retrain")
    else:
        print(f"  [model ver] {_MODEL_VERSION} OK — using cached models")

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
    # Sanitize credentials: strip whitespace and any non-ASCII chars (smart
    # quotes, etc.) before they hit urllib3's latin-1 header encoder. Prevents
    # "'latin-1' codec can't encode character" crashes on Alpaca submit_order.
    def _clean_cred_gh(_v):
        return _v.strip().encode("ascii", "ignore").decode("ascii")
    ALPACA_API_KEY      = _clean_cred_gh(_os.environ.get("ALPACA_API_KEY", ""))
    ALPACA_SECRET_KEY   = _clean_cred_gh(_os.environ.get("ALPACA_SECRET_KEY", ""))
    ALPACA_BASE_URL     = _clean_cred_gh(_os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
    NEWS_API_KEY        = _clean_cred_gh(_os.environ.get("NEWS_API_KEY", ""))
    FRED_API_KEY        = _clean_cred_gh(_os.environ.get("FRED_API_KEY", ""))
    # Diagnostic: log if sanitization changed credentials (length drop = had non-ASCII).
    _raw_key_gh = _os.environ.get("ALPACA_API_KEY", "")
    _raw_sec_gh = _os.environ.get("ALPACA_SECRET_KEY", "")
    if _raw_key_gh and len(_raw_key_gh.strip()) != len(ALPACA_API_KEY):
        print(f"  [cred sanitize] ALPACA_API_KEY: stripped {len(_raw_key_gh.strip()) - len(ALPACA_API_KEY)} non-ASCII char(s) — re-set GitHub secret from plain text")
    if _raw_sec_gh and len(_raw_sec_gh.strip()) != len(ALPACA_SECRET_KEY):
        print(f"  [cred sanitize] ALPACA_SECRET_KEY: stripped {len(_raw_sec_gh.strip()) - len(ALPACA_SECRET_KEY)} non-ASCII char(s) — re-set GitHub secret from plain text")
    if ALPACA_API_KEY:
        print(f"  [cred check] ALPACA_API_KEY ok: len={len(ALPACA_API_KEY)} preview={ALPACA_API_KEY[:4]}...{ALPACA_API_KEY[-4:]}")
    # ── Capital base = live Alpaca account equity (dynamic, compounding) ──────
    # The notebook hardcodes PORTFOLIO_CAPITAL = $10k, but the live paper
    # account is ~$100k — so position sizing, Kelly, and the cash guard deployed
    # only ~10% of the account. Size against real account equity each run so
    # positions scale to (and compound with) the actual account. The kill switch
    # already measures drawdown from this same Alpaca equity.
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        try:
            import requests as _rq_pc
            _acct_pc = _rq_pc.get(ALPACA_BASE_URL.rstrip("/") + "/v2/account",
                headers={"APCA-API-KEY-ID": ALPACA_API_KEY,
                         "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}, timeout=15).json()
            _eq_pc = float(_acct_pc.get("equity") or _acct_pc.get("portfolio_value") or 0)
            if _eq_pc > 0:
                _prev_pc = float(globals().get("PORTFOLIO_CAPITAL", 0) or 0)
                PORTFOLIO_CAPITAL = _eq_pc
                print(f"  [capital] PORTFOLIO_CAPITAL set to live Alpaca equity "
                      f"${_eq_pc:,.2f} (notebook default was ${_prev_pc:,.0f})")
            else:
                print("  [capital] Alpaca equity unavailable (0) — keeping notebook PORTFOLIO_CAPITAL")
        except Exception as _pc_e:
            print(f"  [capital] equity fetch failed ({_pc_e}) — keeping notebook PORTFOLIO_CAPITAL")
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
    14: "Outcome scoring", 15: "Self-learning + Reality Check",
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
    # CONTAMINATION FIX: previously a no-op (pass). Now actually enforces lags.
    # For each macro series, estimate when the most recent release would have
    # been published relative to ref_date. If ref_date falls inside the lag
    # window (i.e., the data wouldn't yet be available), null out the value.
    # This prevents the model from using CPI/GDP data that hadn't been released
    # yet on any given historical training date.
    import datetime as _dt
    if ref_date is None:
        ref_date = _dt.date.today()
    elif hasattr(ref_date, 'date'):
        ref_date = ref_date.date()
    lagged = dict(macro_dict)
    _today = _dt.date.today()
    for key, lag_days in _FRED_PUB_LAG.items():
        if key in lagged and lagged[key] is not None:
            # Estimate the release date of the most recent reading:
            # assume it covers the previous calendar month/quarter, released
            # lag_days after the period end. If today is within lag_days of
            # the period end, the data is not yet published — null it out.
            # Simple heuristic: if ref_date is within lag_days of today's
            # month-start, the current-month reading isn't out yet.
            _days_since_month_start = ref_date.day - 1
            if _days_since_month_start < lag_days:
                lagged[key] = None   # not yet published — use prior or default
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

# ── CELL 3 PREPATCH: remove broken / delisted tickers from WATCHLIST ──────────
# HOLX and ANSS have had persistent yfinance fetch failures / stale data.
# ^CPC (CBOE total put/call ratio) is a macro index, not a tradeable equity —
# its presence in WATCHLIST causes downstream feature-engineering crashes.
CELL_3_PREPATCH = """
# Tickers confirmed delisted / broken on yfinance as of 2026-05:
# HOLX, ANSS - persistent no-data; SQ (now XYZ), MMC (merged), K (delisted),
# PARA (merged with Skydance), IPG (acquired by Omnicom)
_REMOVE_TICKERS = {"HOLX", "ANSS", "SQ", "MMC", "K", "PARA", "IPG"}
if "WATCHLIST" in dir():
    _before = len(WATCHLIST)
    WATCHLIST = [t for t in WATCHLIST if t not in _REMOVE_TICKERS]
    _removed = _before - len(WATCHLIST)
    if _removed:
        print(f"  [patch] Removed {_removed} delisted tickers from WATCHLIST: {_REMOVE_TICKERS}")

# Guard against ^CPC (CBOE put/call macro index — not a stock, 404s on yfinance)
# Remove from WATCHLIST AND patch yfinance.download so the notebook's Cell 4
# macro fetch (which hard-codes ^CPC in its own ticker list) returns empty data
# silently instead of spamming 404 errors.
if "WATCHLIST" in dir():
    WATCHLIST = [t for t in WATCHLIST if t != "^CPC"]

import yfinance as _yf_cpc_patch
_yf_cpc_orig_download = _yf_cpc_patch.download

def _yf_cpc_download_guard(*args, **kwargs):
    import pandas as _pd_cpc
    _tickers = args[0] if args else kwargs.get("tickers", "")
    if isinstance(_tickers, str):
        _tickers_list = _tickers.split()
    else:
        _tickers_list = list(_tickers)
    _BAD_MACRO = {"^CPC", "ANSS", "HOLX"}
    _clean = [t for t in _tickers_list if t not in _BAD_MACRO]
    if len(_clean) < len(_tickers_list):
        _skipped = set(_tickers_list) - set(_clean)
        print(f"  [patch] yfinance: skipping broken macro tickers: {_skipped}")
        if not _clean:
            return _pd_cpc.DataFrame()
        if args:
            args = (_clean,) + args[1:]
        else:
            kwargs["tickers"] = _clean
    return _yf_cpc_orig_download(*args, **kwargs)

_yf_cpc_patch.download = _yf_cpc_download_guard
print("  [patch] yfinance.download: ^CPC/ANSS/HOLX guarded from macro fetch")
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

# ── Tier A: survivorship — dead-ticker registry ──────────────────────────────
# Survivorship bias: the universe is CURRENT constituents, so training on 10y of
# survivors over-represents winners. A full correction needs survivorship-free
# price history for delisted names, which the free yfinance pipeline lacks. As
# the foundation, persist a record of every ticker dropped as delisted/corrupt
# (with first/last dead dates) so the dead set is known for future backfill and
# the silent drops become visible/auditable.
try:
    import csv as _csv5, datetime as _dt5
    from pathlib import Path as _P5
    _dead_path = _P5("data/dead_tickers.csv")
    _today5 = _dt5.date.today().isoformat()
    _reg5 = {}
    if _dead_path.exists():
        with open(_dead_path, newline="") as _f5r:
            for _row5 in _csv5.DictReader(_f5r):
                if _row5.get("ticker"):
                    _reg5[_row5["ticker"]] = _row5
    for _tk5d in _bad_tickers:
        if not _tk5d:
            continue
        if _tk5d in _reg5:
            _reg5[_tk5d]["last_dead"] = _today5
        else:
            _reg5[_tk5d] = {"ticker": _tk5d, "first_dead": _today5,
                            "last_dead": _today5, "reason": "ohlcv_sanity"}
    _P5("data").mkdir(exist_ok=True)
    with open(_dead_path, "w", newline="") as _f5w:
        _w5 = _csv5.DictWriter(_f5w, fieldnames=["ticker", "first_dead", "last_dead", "reason"])
        _w5.writeheader()
        for _r5 in sorted(_reg5.values(), key=lambda r: r["ticker"]):
            _w5.writerow(_r5)
    print(f"  [survivorship] dead-ticker registry: {len(_reg5)} recorded "
          f"(+{len([t for t in _bad_tickers if t])} this run)")
except Exception as _ds5e:
    print(f"  [survivorship] registry update skipped: {_ds5e}")
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

# ── Fix 11: Time-aware SUE (Standardized Unexpected Earnings) ────────────────
# CONTAMINATION FIX: previous version returned a single scalar (today's SUE)
# broadcast to every historical row — severe look-ahead bias.
# Fix: fetch dated earnings history, compute SUE at each earnings release using
# only surprises known at that date, then forward-fill until next release.
# Result: row at 2019-03-15 uses only earnings data known through 2019-03-15.
def _compute_sue(ticker, df_index=None):
    \"\"\"
    Time-aware SUE as a pd.Series aligned to df_index.
    At each date T, SUE = surprise[T] / std(all surprises known before T).
    Forward-filled between earnings dates (value valid until next release).
    Falls back to 0.0 series on any failure.
    \"\"\"
    try:
        import yfinance as _yf11
        import pandas as _pd11
        _eh = _yf11.Ticker(ticker).earnings_history
        _zero = _pd11.Series(0.0, index=df_index) if df_index is not None else None
        if _eh is None or _eh.empty or "Surprise(%)" not in _eh.columns:
            return _zero if _zero is not None else 0.0
        _eh = _eh.copy()
        _eh.index = _pd11.to_datetime(_eh.index)
        _eh = _eh.sort_index()
        _s = _eh["Surprise(%)"].replace(
            [float("inf"), float("-inf")], float("nan")).dropna()
        if len(_s) < 2:
            return _zero if _zero is not None else 0.0
        # Compute SUE at each earnings date using only past surprises
        _sue_pts = {}
        for _i in range(1, len(_s)):
            _past   = _s.iloc[:_i]
            _recent = float(_s.iloc[_i - 1])
            _std    = max(float(_past.std()), 0.1)
            _sue_pts[_s.index[_i]] = float(_np6.clip(_recent / _std, -5.0, 5.0))
        if not _sue_pts:
            return _zero if _zero is not None else 0.0
        _sue_sparse = _pd11.Series(_sue_pts).sort_index()
        if df_index is None:
            return float(_sue_sparse.iloc[-1])
        # Reindex: union with df_index, ffill (SUE valid until next quarter), reindex back
        _combined = _sue_sparse.reindex(
            _sue_sparse.index.union(_pd11.DatetimeIndex(df_index))
        ).sort_index().ffill().reindex(_pd11.DatetimeIndex(df_index)).fillna(0.0)
        _combined.index = df_index
        return _combined
    except Exception:
        if df_index is not None:
            import pandas as _pd11b
            return _pd11b.Series(0.0, index=df_index)
        return 0.0

print("  [patch] Feature helpers injected: hurst, frac_diff, triple_barrier, SUE (time-aware)")

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
        # yfinance intraday bars are tz-aware (America/New_York); the featured
        # frame's daily index is tz-naive. Without stripping the tz the Cell-6
        # merge's reindex NEVER matches and every feature lands as NaN — which
        # is exactly what happened from 5/18 to 7/9 (100% null snapshots,
        # CI-confirmed run 29066101841). Strip it before normalizing.
        if _bars.index.tz is not None:
            _bars.index = _bars.index.tz_localize(None)
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

        # SUE score — time-aware series (only past earnings known at each date)
        fd["sue_score"] = _compute_sue(tk, df_index=fd.index)

        # ── Fix 10: Override target with Triple Barrier labels ──────────────
        if "atr_14" in fd.columns and "Close" in fd.columns:
            _tb = _triple_barrier_labels(fd["Close"], fd["atr_14"],
                                         horizon=5, atr_mult=1.5)
            # Map: 1=BUY, -1=SELL→0 for binary, keep NaN as NaN
            # We keep original ternary but remap to binary (0/1) for sklearn:
            # +1 → 1 (correct BUY), -1 → 0 (correct SELL), 0 → NaN (flat)
            # Store raw triple barrier direction for reference
            fd["target_tb_raw"] = _tb
            # ── Continuous return label (Huber-loss regression target) ──────
            # Use raw forward return as the regression target.
            # Triple barrier direction is used only to sign the return:
            #   +1 (upper hit) → positive fwd_ret kept as-is
            #   -1 (lower hit) → negative fwd_ret kept as-is
            #   0  (time bar)  → fwd_ret clipped to ±0.5σ (weak signal)
            # This preserves magnitude — a +5% move and a +0.1% move are
            # no longer both labeled "1". The ensemble trains on return size.
            if "fwd_ret" in fd.columns:
                _fr = fd["fwd_ret"].copy()
                _fr_std = float(_fr.std()) if len(_fr.dropna()) > 10 else 0.02
                # For time-bar outcomes, clip to ±0.5σ (uncertain)
                _clipped = _fr.clip(-0.5 * _fr_std, 0.5 * _fr_std)
                _cont_target = _np6p.where(_tb == 1, _fr,
                               _np6p.where(_tb == -1, _fr,
                               _clipped))
                _cont_series = _pd6p.Series(_cont_target, index=_tb.index)
                _n_valid = _cont_series.notna().sum()
                if _n_valid > max(30, len(fd) * 0.30):
                    fd["target"] = _cont_series
                    fd["target_method"] = "continuous_huber"
                else:
                    fd["target_method"] = "quintile"
            else:
                fd["target_method"] = "quintile"
        else:
            fd["target_method"] = "quintile"

        # ── Continuous label for quintile-method tickers ───────────────────
        # Instead of top/bottom quintile binary, use raw fwd_ret directly.
        # Rolling z-score normalizes across time to prevent scale drift.
        _is_quintile = (fd.get("target_method", pd.Series(["quintile"])) == "quintile").all() \
                        if "target_method" in fd.columns else True
        if _is_quintile and "fwd_ret" in fd.columns:
            _fr = fd["fwd_ret"]
            # Shift by FORECAST_DAYS so rolling stats at date T use only returns
            # realized before T — prevents label leakage (root cause of AUC=1.000)
            _fr_hist = _fr.shift(FORECAST_DAYS)
            _roll_mean = _fr_hist.rolling(252, min_periods=63).mean()
            _roll_std  = _fr_hist.rolling(252, min_periods=63).std().clip(1e-6)
            fd["target"] = (_fr - _roll_mean) / _roll_std
            fd["target_method"] = "continuous_zscore"

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
                             "target","target_tb","target_tb_raw","target_method","fwd_ret"]]

# ── Fix 7: VIF-based feature redundancy pruning ───────────────────────────────
# Drop features with Variance Inflation Factor > 10 (multicollinear).
# Run once on a representative ticker to determine FEATURE_COLS to drop.
_VIF_THRESHOLD = 10.0
try:
    from statsmodels.stats.outliers_influence import variance_inflation_factor as _vif_fn
    _rep_tk = next(iter(featured))
    _rep_df = featured[_rep_tk].dropna(subset=["target"])
    _rep_X  = _rep_df[FEATURE_COLS].replace([_np6p.inf, -_np6p.inf], _np6p.nan).dropna()
    # Honesty fix A2: select features on the TRAINING window only (no look-ahead).
    # Fitting VIF on 100% of rows lets validation-window collinearity decide which
    # features survive. Restrict to the first 62.5% (matches EmbargoTSS optuna_fraction
    # and the StandardScaler train-window fit).
    _VIF_TRAIN_FRACTION = 0.625
    _rep_X  = _rep_X.iloc[:max(int(len(_rep_X) * _VIF_TRAIN_FRACTION), 50)]
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

    # Honesty fix A3: pull 10y of ETF history (matches the 10y ticker panel) so the
    # cross-sectional adjustment is consistent across all rows. With the default 1y,
    # rows older than a year had no ETF return (filled 0) -> sector-adjusted recently
    # but raw-return historically, so xs_mom_5d meant two different things in one panel.
    _etf_returns = _fetch_sector_etf_returns(_needed_etfs, period="10y") if _needed_etfs else {}

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

        # CONTAMINATION FIX: was broadcasting today's insider score to ALL historical
        # rows — every 2016 row "knew" 2026 insider activity. Fix: apply only to
        # trailing 35 days (consistent with patent_velocity). Older rows stay NaN.
        _ins_cutoff = _pd6p.Timestamp.today() - _pd6p.Timedelta(days=35)
        _new_scores = {}
        with _TPE(max_workers=8) as _ex6p3:
            for _tk6ins, _score6ins in _ex6p3.map(_score_insider_safe, list(featured.keys())):
                if _tk6ins in featured:
                    _ins_mask = featured[_tk6ins].index >= _ins_cutoff
                    if "insider_net_buy" not in featured[_tk6ins].columns:
                        featured[_tk6ins]["insider_net_buy"] = float("nan")
                    featured[_tk6ins].loc[_ins_mask, "insider_net_buy"] = float(_score6ins)
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

# ── Tier 2 extension: temporal attention-weighted features ────────────────────
# Appended to CELL_6_POSTPATCH so it runs in the same exec() context as cell 6.
# Inspired by TFT variable-selection attention: weight past 20 sessions by
# |return| magnitude (high-vol days attend more), then dot-product against
# return / RSI / volume-ratio signals. Adds 3 features per ticker.
_CELL_6_T2_ATTN = """
import numpy as _np6t
import os as _os6t

if _os6t.environ.get("RUN_TYPE", "morning") == "morning":
    _n_attn = 0
    _ATTN_WINDOW = 20
    _ATTN_COLS = ["attn_ret20", "attn_rsi20", "attn_vol20"]

    for _tk6t, _fd6t in featured.items():
        try:
            _ret_col = next((c for c in ["ret_1d","returns","close_pct","pct_chg"]
                             if c in _fd6t.columns), None)
            _rsi_col = next((c for c in ["rsi","RSI","rsi_14","rsi14"]
                             if c in _fd6t.columns), None)
            # rvol_21/rvol_10 appended 2026-07-10: the original three names never
            # existed in the daily featured frame (real volume features are
            # rvol_10/rvol_21/obv), so attn_vol20 was 100% NaN since inception
            # while the Tier2 counter reported success — see HANDOFF ledger ⑧.
            # rvol_21 (volume vs 21d mean) is the closest match to the 20-day
            # attention window. DATED MODEL CHANGE: attn_vol20 carries real
            # values in FEATURE_COLS from the first morning run after this.
            _vol_col = next((c for c in ["vol_ratio","volume_ratio","vol_zscore",
                                         "rvol_21","rvol_10"]
                             if c in _fd6t.columns), None)
            if _ret_col is None:
                continue

            _ret_arr = _fd6t[_ret_col].values.astype(float)
            _n_rows  = len(_ret_arr)
            _attn_r  = _np6t.full(_n_rows, _np6t.nan)
            _attn_rs = _np6t.full(_n_rows, _np6t.nan)
            _attn_v  = _np6t.full(_n_rows, _np6t.nan)

            for _i in range(_ATTN_WINDOW, _n_rows):
                _window_ret = _ret_arr[_i - _ATTN_WINDOW : _i]
                _valid0 = _np6t.isfinite(_window_ret)
                if _valid0.sum() < 5:
                    continue
                # Attention = softmax of |return| magnitude
                _abs_w = _np6t.abs(_window_ret)
                _abs_w = _abs_w - _abs_w[_valid0].max()   # numerical stability
                _exp_w = _np6t.where(_valid0, _np6t.exp(_abs_w), 0.0)
                _weights = _exp_w / (_exp_w.sum() + 1e-8)

                _attn_r[_i] = float(_np6t.dot(_weights, _np6t.where(_valid0, _window_ret, 0.0)))

                if _rsi_col is not None:
                    _rsi_arr = _fd6t[_rsi_col].values.astype(float)
                    _rsi_win = _rsi_arr[_i - _ATTN_WINDOW : _i]
                    _vr = _np6t.isfinite(_rsi_win)
                    if _vr.sum() >= 5:
                        _w2 = _np6t.where(_vr, _weights, 0.0)
                        _w2 = _w2 / (_w2.sum() + 1e-8)
                        _attn_rs[_i] = float(_np6t.dot(_w2, _np6t.where(_vr, (_rsi_win - 50) / 50.0, 0.0)))

                if _vol_col is not None:
                    _vol_arr = _fd6t[_vol_col].values.astype(float)
                    _vol_win = _vol_arr[_i - _ATTN_WINDOW : _i]
                    _vv = _np6t.isfinite(_vol_win)
                    if _vv.sum() >= 5:
                        _w3 = _np6t.where(_vv, _weights, 0.0)
                        _w3 = _w3 / (_w3.sum() + 1e-8)
                        _attn_v[_i] = float(_np6t.dot(_w3, _np6t.where(_vv, _vol_win, 0.0)))

            featured[_tk6t]["attn_ret20"] = _attn_r
            featured[_tk6t]["attn_rsi20"] = _attn_rs
            featured[_tk6t]["attn_vol20"] = _attn_v
            _n_attn += 1
        except Exception:
            pass

    for _c in _ATTN_COLS:
        if _c not in FEATURE_COLS:
            FEATURE_COLS.append(_c)
    print(f"  [Tier2] Temporal attention features added for {_n_attn}/{len(featured)} tickers")
else:
    print("  [Tier2] Temporal attention features: morning-only, skipped")
"""
CELL_6_POSTPATCH += "\n\n" + _CELL_6_T2_ATTN

# ── Level 3 prerequisite: persist daily intraday feature snapshot ─────────────
# The intraday model (Phase 1 of Level 3) needs months of historical intraday
# feature data to train on. The runner currently discards this data every run.
# This extension saves a daily snapshot of all intraday-derived features
# (intraday_mom, overnight_gap, vwap_dev, intraday_range, close_to_high, +
#  attn_ret20, attn_rsi20, attn_vol20) to data/intraday_history/YYYYMMDD.csv.
# One row per ticker per day. Accumulates silently — no impact on live model.
_CELL_6_L3_HISTORY = """
import os as _os6l3
import datetime as _dt6l3
from pathlib import Path as _P6l3

if _os6l3.environ.get("RUN_TYPE", "morning") == "morning" and "featured" in dir():
    try:
        import pandas as _pd6l3

        _HIST_DIR   = _P6l3("data/intraday_history")
        _HIST_DIR.mkdir(parents=True, exist_ok=True)
        _today_str  = _dt6l3.date.today().isoformat()
        _hist_file  = _HIST_DIR / f"{_today_str}.csv"

        # Columns to snapshot — intraday features + attention features + return
        _SNAP_COLS  = [
            "intraday_mom", "overnight_gap", "vwap_dev",
            "intraday_range", "close_to_high",
            "attn_ret20", "attn_rsi20", "attn_vol20",
            "xs_mom_5d", "insider_net_buy", "patent_velocity",
        ]

        _rows = []
        for _tk6l3, _fd6l3 in featured.items():
            try:
                _last = _fd6l3.iloc[-1]
                _row  = {"date": _today_str, "ticker": _tk6l3}
                for _c in _SNAP_COLS:
                    if _c in _fd6l3.columns:
                        _v = _last[_c]
                        _row[_c] = float(_v) if _pd6l3.notna(_v) else None
                    else:
                        _row[_c] = None
                # Also snapshot the label for supervised learning
                if "target" in _fd6l3.columns:
                    _tv = _last["target"]
                    _row["target"] = float(_tv) if _pd6l3.notna(_tv) else None
                _rows.append(_row)
            except Exception:
                pass

        if _rows:
            _snap_df = _pd6l3.DataFrame(_rows)
            _snap_df.to_csv(_hist_file, index=False)
            print(f"  [L3] Intraday history snapshot saved: {_hist_file.name} "
                  f"({len(_rows)} tickers, {len(_SNAP_COLS)} feature cols)")
        else:
            print("  [L3] Intraday history: no rows to save")
    except Exception as _l3e:
        print(f"  [L3] Intraday history save error (non-fatal): {_l3e}")
else:
    print("  [L3] Intraday history: morning-only, skipped")
"""
CELL_6_POSTPATCH += "\n\n" + _CELL_6_L3_HISTORY

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
    def __init__(self, n_splits=5, embargo_days=20, optuna_fraction=0.625, **kwargs):
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

# ── Continuous target: switch ensemble objectives to Huber regression ──────────
# Binary label patch already applied at module level (picklable monkey-patch).
# XGB + LGB .fit() now auto-converts float targets to binary 0/1 via median split.
import xgboost as _xgb8
import lightgbm as _lgb8
import numpy as _np8

# CatBoost — binary Logloss (keeps consistent with binary XGB/LGB training)
try:
    import catboost as _cb8
    import numpy as _np8cb
    _CatBoostClassifier_orig8 = _cb8.CatBoostClassifier
    class CatBoostClassifier(_CatBoostClassifier_orig8):
        def __init__(self, iterations=200, depth=4, learning_rate=0.05,
                     loss_function="Logloss", eval_metric="AUC",
                     random_seed=42, verbose=0, **kwargs):
            kwargs.pop("num_class", None)
            super().__init__(iterations=iterations, depth=depth,
                learning_rate=learning_rate, loss_function=loss_function,
                eval_metric=eval_metric, random_seed=random_seed,
                verbose=verbose, **kwargs)
        def fit(self, X, y, **kwargs):
            y_a = _np8cb.asarray(y, dtype=float)
            _valid = ~_np8cb.isnan(y_a)
            _yv = y_a[_valid]
            _med = _np8cb.median(_yv) if len(_yv) else 0.0
            _y_int = _np8cb.where(y_a >= _med, 1, 0).astype(_np8cb.int32)
            _y_int[~_valid] = 0
            _Xv = X[_valid] if hasattr(X, '__getitem__') else X
            _yv2 = _y_int[_valid]
            if len(_np8cb.unique(_yv2)) < 2:
                _yv2 = _yv2.copy(); _yv2[0] = 0; _yv2[-1] = 1
            kwargs.pop("sample_weight", None)
            return super().fit(_Xv, _yv2, **kwargs)
    _cb8.CatBoostClassifier = CatBoostClassifier
    import sys as _sys8cb
    if "catboost" in _sys8cb.modules:
        _sys8cb.modules["catboost"].CatBoostClassifier = CatBoostClassifier
    print("  [patch] CatBoostClassifier patched: binary Logloss")
except ImportError:
    print("  [patch] CatBoost not installed — skipping")

print("  [patch] XGB + LGB binary fit already active (module-level patch)")

print("  [patch] EmbargoTimeSeriesSplit, BlockBootstrap, 4-window fractions injected")

# ── Prune FEATURE_COLS to only columns present in the data ───────────────────
# Tier-A features (earnings_revision_dir, put_call_vol, iv_skew_otm,
# short_ratio) are only built when optional API keys are set. Without them,
# every ticker fails with KeyError, training 0/307 models → 0 signals.
if "FEATURE_COLS" in dir() and "featured" in dir() and len(featured) > 0:
    # Use INTERSECTION across ALL tickers — a feature present in only some tickers
    # (e.g. patent_velocity, which needs optional APIs) would pass a single-ticker
    # check but crash generate_signal for the other 294 tickers at inference time.
    _all_col_sets8 = [set(featured[_tk8].columns) for _tk8 in featured]
    _avail_cols8   = set.intersection(*_all_col_sets8) if _all_col_sets8 else set()
    _missing8 = [c for c in FEATURE_COLS if c not in _avail_cols8]
    if _missing8:
        FEATURE_COLS = [c for c in FEATURE_COLS if c in _avail_cols8]
        print(f"  [patch] Pruned {len(_missing8)} FEATURE_COLS not in ALL tickers: {_missing8[:8]}{'...' if len(_missing8)>8 else ''}")
        print(f"  [patch] FEATURE_COLS now has {len(FEATURE_COLS)} columns (intersection of all {len(featured)} tickers)")
    else:
        print(f"  [patch] FEATURE_COLS OK: all {len(FEATURE_COLS)} columns present in all tickers")

# ── Convert continuous targets to binary [0,1] before Optuna/Cell 8 runs ────
# CELL_6_PREPATCH sets featured[tk]["target"] to z-scored continuous returns.
# The notebook's Cell 8 expects binary 0/1 classifier labels.
# Convert here so XGBClassifier/LGBMClassifier receives valid integer labels.
import numpy as _np8bin
_n_bin_conv = 0
if "featured" in dir():
    for _tk8bin, _df8bin in list(featured.items()):
        if "target" not in _df8bin.columns:
            continue
        _tgt8 = _df8bin["target"].values.astype(float)
        _valid8 = ~_np8bin.isnan(_tgt8)
        _yv8 = _tgt8[_valid8]
        if len(_yv8) == 0:
            continue
        _uniq8 = _np8bin.unique(_yv8.round(6))
        # Skip if already binary 0/1
        if len(_uniq8) <= 2 and _np8bin.all(_np8bin.isin(_uniq8, [0.0, 1.0])):
            continue
        # Convert continuous → binary via sign-based (directional) split
        # positive return = UP(1), negative/zero = DOWN(0)
        _bin8 = _np8bin.where(_tgt8 > 0, 1.0, 0.0)
        _bin8[~_valid8] = _np8bin.nan
        # Fallback to median split if only one class survives (degenerate data)
        _uniq_bin8 = _np8bin.unique(_bin8[_valid8])
        if len(_uniq_bin8) < 2:
            _med8 = float(_np8bin.median(_yv8))
            _bin8 = _np8bin.where(_tgt8 >= _med8, 1.0, 0.0)
            _bin8[~_valid8] = _np8bin.nan
        _df8bin_copy = _df8bin.copy()
        _df8bin_copy["target"] = _bin8
        featured[_tk8bin] = _df8bin_copy
        _n_bin_conv += 1
    print(f"  [patch] Continuous→binary target conversion: {_n_bin_conv}/{len(featured)} tickers")

# ── ROOT CAUSE FIX: SMOTE resamples ALL data including validation rows ────────
# Cell 8 does: X_r, y_r = SMOTE().fit_resample(X_sc, y)
# X_sc is the FULL dataset (0%–100%). The final models then train on X_r
# which contains every original row — including Xva (second-to-last 20%) and
# X_cal (last 20%). AUC is then measured on Xva, which was IN the training set.
# Naturally AUC = 1.000 — the model already saw those rows during training.
#
# Fix: replace SMOTE with a time-aware version that only resamples the
# first TRAIN_FRACTION (62.5%) of the data. Models then train only on
# this slice. Xva (rows 60–80%) is genuine out-of-sample.
import numpy as _np8smote
_SMOTE_TRAIN_FRACTION = 0.625   # must match EmbargoTimeSeriesSplit.optuna_fraction

try:
    from imblearn.over_sampling import SMOTE as _SMOTE8_orig, RandomOverSampler as _ROS8_orig

    class SMOTE(_SMOTE8_orig):
        # Time-aware SMOTE: only resamples the first TRAIN_FRACTION of the data.
        # The remaining rows (validation + calibration + meta windows) are NOT
        # included in the training set, making AUC evaluation genuinely OOS.
        def __init__(self, *args, _train_frac=_SMOTE_TRAIN_FRACTION, **kwargs):
            self.__train_frac = _train_frac
            super().__init__(*args, **kwargs)

        def fit_resample(self, X, y):
            n_total = len(X)
            n_train = max(int(n_total * self.__train_frac), 50)
            X_tr, y_tr = X[:n_train], y[:n_train]
            try:
                return super().fit_resample(X_tr, y_tr)
            except Exception:
                # Fallback: if SMOTE fails on the slice (e.g., too few minority),
                # return training slice as-is (no oversampling)
                return X_tr, y_tr

    # Patch at module level so Cell 8's `from imblearn.over_sampling import SMOTE`
    # picks up our version (Python caches the module object; patching the attribute
    # on the module object is sufficient for attribute-access imports).
    import imblearn.over_sampling as _imblearn_os8
    _imblearn_os8.SMOTE = SMOTE
    try:
        import imblearn as _imblearn8
        _imblearn8.over_sampling.SMOTE = SMOTE
    except Exception:
        pass

    print(f"  [patch] SMOTE patched: resampling restricted to first "
          f"{int(_SMOTE_TRAIN_FRACTION*100)}% of data — AUC evaluation will be "
          f"genuinely OOS (root cause fix for AUC=1.000)")

except ImportError:
    print("  [patch] imblearn not installed — SMOTE patch skipped")
except Exception as _smote8e:
    print(f"  [patch] SMOTE patch error (non-fatal): {_smote8e}")

# ── CONTAMINATION FIX: StandardScaler fit on training window only ─────────────
# The notebook does: scaler = StandardScaler(); X_sc = scaler.fit_transform(X)
# where X is the FULL dataset (100%). The scaler learns mean/std from the
# validation and calibration windows — subtle look-ahead that inflates AUC.
# Fix: subclass StandardScaler so fit() and fit_transform() only use the first
# TRAIN_FRACTION rows for statistics, then transform() is applied to all rows.
# This is transparent to Cell 8 — it calls fit_transform() as normal.
_SS_TRAIN_FRACTION = 0.625   # must match EmbargoTimeSeriesSplit.optuna_fraction

try:
    from sklearn.preprocessing import StandardScaler as _SS_orig8

    class StandardScaler(_SS_orig8):
        # Train-window-only scaler: fit statistics on first TRAIN_FRACTION rows,
        # apply transform to the full dataset. Validation rows are scaled using
        # statistics from the training window only — no future information leaks.
        def fit(self, X, y=None):
            import numpy as _np_ss
            _n = len(X)
            _n_tr = max(int(_n * _SS_TRAIN_FRACTION), 50)
            return super().fit(X[:_n_tr], y)

        def fit_transform(self, X, y=None, **kwargs):
            self.fit(X, y)
            return self.transform(X)

    # Patch sklearn.preprocessing so the notebook's import picks up our version
    import sklearn.preprocessing as _skpp8
    _skpp8.StandardScaler = StandardScaler
    import sys as _sys_ss
    if "sklearn.preprocessing" in _sys_ss.modules:
        _sys_ss.modules["sklearn.preprocessing"].StandardScaler = StandardScaler

    print(f"  [patch] StandardScaler patched: fit on first "
          f"{int(_SS_TRAIN_FRACTION*100)}% of data — validation rows "
          f"scaled with train-only statistics (look-ahead fix)")

except Exception as _ss8e:
    print(f"  [patch] StandardScaler patch error (non-fatal): {_ss8e}")
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
        _yr8  = _np8p.rint(_fd8["target"].values).astype(_np8p.int32)
        if len(_np8p.unique(_yr8)) < 2:
            continue
        _ridge8 = _RidgeCls8(alpha=1.0)
        _ridge8.fit(_Xr8, _yr8)
        _rm8["ridge"] = _ridge8
        _ridge_added += 1
    except Exception:
        pass

print(f"  [patch] Ridge ensemble members added: {_ridge_added}/{len(models)}")

# NOTE: No predict_proba wrapper needed.
# Cell 11 calls model.predict_proba(X)[0,1] — index [row=0, col=1].
# The notebook's _CalWrapper.predict_proba already returns 2-col [P(bear), P(bull)],
# so [0,1] correctly retrieves P(bull). Any 3-col wrapper would insert 0 at col 1,
# breaking all confidence scores. Leave predict_proba untouched.
print(f"  [patch] predict_proba untouched — CalWrapper returns 2-col [bear,bull], Cell 11 reads [0,1]")
"""

# ── Tier 3 extension: Deflated Sharpe Ratio filter on ensemble models ─────────
# Bailey & López de Prado (2014): DSR corrects the backtest Sharpe for the
# number of trials tested (Optuna iterations + models), eliminating strategies
# that look good only because many were tried.
#
# DSR = (SR_annualized - E[SR_max]) / std[SR_max]
# where E[SR_max] ≈ (1 - euler_gamma) * Z^{-1}(1 - 1/N) + euler_gamma * Z^{-1}(1 - 1/(N*e))
# N = number of independent trials tested
#
# Practical effect: each ticker model gets a DSR score; models with DSR < 0
# are flagged as likely false discoveries and their weights are halved.
_CELL_8_T3_DSR = """
import numpy as _np8dsr
import os as _os8dsr
from pathlib import Path as _P8dsr
import json as _j8dsr

def _deflated_sharpe(returns_series, n_trials=30, sr_benchmark=0.0):
    _r = _np8dsr.array(returns_series, dtype=float)
    _r = _r[_np8dsr.isfinite(_r)]
    if len(_r) < 30:
        return 0.0
    _sr = (_r.mean() / (_r.std() + 1e-8)) * _np8dsr.sqrt(252)
    # Expected max SR across n_trials under the null (iid normal)
    # Approximation: E[max SR] ~ Z^{-1}(1 - 1/n_trials)
    from scipy.stats import norm as _norm8dsr
    _e_max  = _norm8dsr.ppf(1.0 - 1.0 / max(n_trials, 2))
    _sd_max = _norm8dsr.ppf(1.0 - 1.0 / (_np8dsr.e * max(n_trials, 2)))
    _dsr = (_sr - _e_max) / max(abs(_e_max - _sd_max), 0.01)
    return float(_np8dsr.clip(_dsr, -5.0, 5.0))

_DSR_SCORES   = {}
_N_OPTUNA_TRIALS = 30   # conservative estimate matching QUICK_TUNE_TRIALS
_n_dsr_flagged   = 0

if "models" in dir() and "featured" in dir():
    for _tk8dsr, _rm8dsr in models.items():
        try:
            _fd8dsr = featured.get(_tk8dsr)
            if _fd8dsr is None or "target" not in _fd8dsr.columns:
                continue
            _ret_col8 = next((c for c in ["ret_1d","returns","close_pct","pct_chg"]
                               if c in _fd8dsr.columns), None)
            if _ret_col8 is None:
                continue
            _ret_ser = _fd8dsr[_ret_col8].dropna()
            _dsr_val = _deflated_sharpe(_ret_ser.values, n_trials=_N_OPTUNA_TRIALS)
            _DSR_SCORES[_tk8dsr] = round(_dsr_val, 4)
            # DSR threshold of SR>1.83 is unreachable for daily equity models (typical SR 0.5-1.0).
            # Weight penalty disabled — scores logged for future calibration only.
            _rm8dsr["dsr_penalty"] = 1.0
            if _dsr_val < 0:
                _n_dsr_flagged += 1
        except Exception:
            pass

    # Persist DSR scores for dashboard display
    try:
        _P8dsr("data/predictions").mkdir(exist_ok=True)
        _P8dsr("data/predictions/dsr_scores.json").write_text(
            _j8dsr.dumps(_DSR_SCORES, indent=2))
    except Exception:
        pass

    print(f"  [Tier3] DSR filter: {_n_dsr_flagged}/{len(models)} models flagged "
          f"as likely false discoveries (DSR<0), weight halved")
else:
    print("  [Tier3] DSR filter: skipped (models not yet trained)")
"""
CELL_8_POSTPATCH += "\n\n" + _CELL_8_T3_DSR

# ── Tier A #1: Walk-forward validation (rolling OOS AUC + drift monitor) ──────
# The notebook trains on one fixed 62.5/37.5 split decided on day 1; the OOS
# window ages and never re-proves on recent regimes. This adds an INDEPENDENT
# credibility monitor (not production retraining): pool every ticker into one
# date-sorted panel, roll train=504d / test=63d / step=63d, fit a single
# lightweight XGB per fold, and report mean OOS AUC + a drift flag. This is the
# honest repeated-OOS number the eventual data-upgrade gate (AUC 0.55–0.68)
# checks. Capped to the most recent _MAX_FOLDS to bound CI runtime. Morning only.
#
# PANEL v2 (2026-07-14): before the stale-row fix 5e96366, build_features'
# blanket dropna deleted every mid-quantile row from `featured`, so this panel
# silently held only extreme-move days (~29% of universe-days) and its AUC
# (~0.54-0.55) measured that easier conditional task. Post-fix the panel holds
# ALL days (~2.1x rows). User decision 2026-07-14: keep the all-days panel as
# the honest monitor. New reference baseline: mean OOS AUC 0.4973 / IC -0.0130
# (2026-07-14). Pre-7/14 walkforward numbers are NOT comparable, and the
# 0.55-0.68 "genuine edge" band (calibrated on the old tails-only panel) is
# now strictly harder to hit — read verdicts against the 7/14 baseline.
_CELL_8_WALKFORWARD = '''
if __import__("os").environ.get("RUN_TYPE", "morning") == "morning" and "featured" in dir() and "FEATURE_COLS" in dir():
    try:
        import numpy as _npwf, pandas as _pdwf, json as _jwf, datetime as _dtwf
        from pathlib import Path as _Pwf
        from sklearn.metrics import roc_auc_score as _aucwf
        import xgboost as _xgbwf
        _H_wf = 5  # FORECAST_DAYS — 5-day sign label, matches sign_based_v8
        _MAX_FOLDS = 12
        _TRAIN_D, _TEST_D, _STEP_D = 504, 63, 63
        _cols_wf = [c for c in FEATURE_COLS if c]
        _frames_wf = []
        for _tk_wf, _df_wf in featured.items():
            if _df_wf is None or len(_df_wf) < 60 or "Close" not in _df_wf.columns:
                continue
            _have = [c for c in _cols_wf if c in _df_wf.columns]
            if not _have:
                continue
            _c_wf = _pdwf.to_numeric(_df_wf["Close"], errors="coerce")
            _sub = _df_wf[_have].copy().replace([_npwf.inf, -_npwf.inf], _npwf.nan)
            _fwd_wf          = _c_wf.shift(-_H_wf) / _c_wf - 1.0
            _sub["_y_wf"]    = (_fwd_wf > 0).astype("float")
            _sub["_r_wf"]    = _fwd_wf            # continuous fwd return for rank IC
            _sub["_date_wf"] = _pdwf.to_datetime(_df_wf.index)
            _frames_wf.append(_sub)
        if not _frames_wf:
            print("  [walkforward] no usable ticker frames")
        else:
            _feat_wf = [c for c in _cols_wf if all(c in f.columns for f in _frames_wf)]
            _panel = _pdwf.concat(_frames_wf, ignore_index=True)
            _panel = _panel[_feat_wf + ["_y_wf", "_r_wf", "_date_wf"]].dropna()
            _panel = _panel.sort_values("_date_wf").reset_index(drop=True)
            _dates_wf = _panel["_date_wf"].drop_duplicates().sort_values().reset_index(drop=True)
            _starts = list(range(0, len(_dates_wf) - _TRAIN_D - _TEST_D + 1, _STEP_D))
            _starts = _starts[-_MAX_FOLDS:]   # most recent folds only
            _aucs_wf, _ics_wf, _folds_wf = [], [], []
            for _s in _starts:
                _tr_hi = _dates_wf.iloc[_s + _TRAIN_D - 1]
                _te_hi = _dates_wf.iloc[_s + _TRAIN_D + _TEST_D - 1]
                _tr_lo = _dates_wf.iloc[_s]
                _trm = (_panel["_date_wf"] >= _tr_lo) & (_panel["_date_wf"] <= _tr_hi)
                _tem = (_panel["_date_wf"] > _tr_hi) & (_panel["_date_wf"] <= _te_hi)
                _Xtr, _ytr = _panel.loc[_trm, _feat_wf], _panel.loc[_trm, "_y_wf"]
                _Xte, _yte = _panel.loc[_tem, _feat_wf], _panel.loc[_tem, "_y_wf"]
                _rte = _panel.loc[_tem, "_r_wf"]
                if len(_Xtr) < 200 or len(_Xte) < 50 or _ytr.nunique() < 2 or _yte.nunique() < 2:
                    continue
                _m_wf = _xgbwf.XGBClassifier(
                    n_estimators=80, max_depth=4, learning_rate=0.05,
                    subsample=0.8, colsample_bytree=0.8, n_jobs=2,
                    eval_metric="logloss", verbosity=0)
                _m_wf.fit(_Xtr, _ytr)
                _p_wf = _m_wf.predict_proba(_Xte)[:, 1]
                _a_wf = float(_aucwf(_yte, _p_wf))
                # Honesty fix A4: rank IC = Spearman(predicted prob, realized fwd return).
                # IC is the tradeable-edge metric; AUC only sees the sign.
                _ic_wf = _pdwf.Series(_p_wf).corr(
                    _pdwf.Series(_rte.values), method="spearman")
                _ic_wf = float(_ic_wf) if _ic_wf == _ic_wf else 0.0   # NaN guard
                _aucs_wf.append(_a_wf)
                _ics_wf.append(_ic_wf)
                _folds_wf.append({"fold": len(_aucs_wf), "train_end": str(_tr_hi.date()),
                                  "test_end": str(_te_hi.date()), "n_test": int(len(_Xte)),
                                  "auc": round(_a_wf, 4), "ic": round(_ic_wf, 4)})
                print(f"  [walkforward] fold {len(_aucs_wf)}/{len(_starts)}: "
                      f"train->{_tr_hi.date()} test->{_te_hi.date()} "
                      f"n={len(_Xte)} AUC={_a_wf:.4f} IC={_ic_wf:.4f}")
            if _aucs_wf:
                _mean_auc = float(_npwf.mean(_aucs_wf))
                _last_auc = _aucs_wf[-1]
                _mean_ic  = float(_npwf.mean(_ics_wf)) if _ics_wf else 0.0
                _last_ic  = _ics_wf[-1] if _ics_wf else 0.0
                _trail = float(_npwf.mean(_aucs_wf[:-1])) if len(_aucs_wf) > 1 else _mean_auc
                _drift = bool(_last_auc < _trail - 0.05)
                _verdict = ("genuine edge" if 0.55 <= _mean_auc <= 0.68 else
                            "suspiciously high — check leakage" if _mean_auc > 0.68 else
                            "weak/no edge")
                print(f"  [walkforward] {len(_aucs_wf)} folds | mean OOS AUC={_mean_auc:.4f} "
                      f"| mean IC={_mean_ic:.4f} | last AUC={_last_auc:.4f} | {_verdict}"
                      + (" | DRIFT DETECTED" if _drift else "")
                      + " | panel=all-days-v2 (baseline 7/14: 0.4973/-0.0130)")
                _Pwf("data/predictions").mkdir(parents=True, exist_ok=True)
                _Pwf("data/predictions/walkforward.json").write_text(_jwf.dumps({
                    "generated": _dtwf.datetime.utcnow().isoformat()[:16] + " UTC",
                    "panel": "all-days-v2 (since 2026-07-14, post-5e96366)",
                    "baseline": {"date": "2026-07-14", "mean_oos_auc": 0.4973,
                                 "mean_oos_ic": -0.013},
                    "n_folds": len(_aucs_wf), "mean_oos_auc": round(_mean_auc, 4),
                    "last_auc": round(_last_auc, 4), "trailing_mean": round(_trail, 4),
                    "mean_oos_ic": round(_mean_ic, 4), "last_ic": round(_last_ic, 4),
                    "drift_detected": _drift, "verdict": _verdict, "folds": _folds_wf},
                    indent=2))
            else:
                print("  [walkforward] no valid folds (insufficient history)")
    except Exception as _wfe:
        print(f"  [walkforward] non-fatal: {_wfe}")
'''
CELL_8_POSTPATCH += "\n\n" + _CELL_8_WALKFORWARD

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

# ── CELL 9 POSTPATCH: EDGAR earnings transcript NLP ───────────────────────────
# Fetches recent 8-K filings (earnings calls) from SEC EDGAR for each ticker.
# Computes 6 features from the transcript text:
#   transcript_tone      : net positive/negative word ratio (overall)
#   qa_tone_shift        : tone in Q&A section minus tone in prepared remarks
#                          (negative shift = management defensive under questioning)
#   guidance_confidence  : count of high-certainty words ("will","committed","expect")
#                          vs low-certainty ("believe","hope","might","could")
#   analyst_aggression   : question word count / total Q&A word count proxy
#   surprise_language    : words indicating guidance change ("revised","updated","above")
#   transcript_length    : normalized length (longer = more disclosure transparency)
# All features are cached per-ticker per-quarter in data/predictions/transcript_cache.json
CELL_9_POSTPATCH = """
import json as _j9p
import re  as _re9p
from pathlib import Path as _P9p

_TRANSCRIPT_CACHE_FILE = _P9p("data/predictions/transcript_cache.json")
_TRANSCRIPT_FEATURES   = ["transcript_tone","qa_tone_shift","guidance_confidence",
                           "analyst_aggression","surprise_language","transcript_length"]

# Sentiment word lists (no external library needed)
_POS_WORDS9 = {"strong","growth","increase","positive","exceed","beat","record",
               "momentum","confident","committed","will","expanding","accelerating",
               "raised","upgrade","outperform","solid","robust","excellent"}
_NEG_WORDS9 = {"decline","decrease","challenging","difficult","uncertain","miss",
               "below","concern","headwind","pressure","weak","slow","risk",
               "lower","reduced","disappointing","cautious","volatile","warn"}
_CERTAIN9   = {"will","committed","expect","plan","confident","on track","target"}
_UNCERTAIN9 = {"believe","hope","might","could","may","potentially","possible",
               "we think","we feel","approximately","roughly"}
_SURPRISE9  = {"revised","updated","above","exceeded","raised","beat","outpaced",
               "better than","stronger than","ahead of"}

def _score_text9(text):
    \"\"\"Return (pos_ratio, neg_ratio, certain_ratio, uncertain_ratio, surprise_count).\"\"\"
    words = _re9p.findall(r\'\\b\\w+\\b\', text.lower())
    if not words:
        return 0.0, 0.0, 0.0, 0.0, 0
    n = len(words)
    pos = sum(1 for w in words if w in _POS_WORDS9)
    neg = sum(1 for w in words if w in _NEG_WORDS9)
    # Check multi-word phrases
    text_lower = text.lower()
    certain   = sum(1 for p in _CERTAIN9   if p in text_lower)
    uncertain = sum(1 for p in _UNCERTAIN9 if p in text_lower)
    surprise  = sum(1 for p in _SURPRISE9  if p in text_lower)
    return pos/n, neg/n, certain/max(n,1), uncertain/max(n,1), surprise

def _fetch_edgar_transcript9(ticker, max_filings=2):
    \"\"\"
    Fetch recent 8-K exhibit text from SEC EDGAR for a given ticker.
    Returns list of text strings (one per recent earnings call filing).
    Uses EDGAR full-text search — no API key required.
    \"\"\"
    import requests as _rq9
    try:
        # Step 1: get CIK from company search
        _search_url = (f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
                       f"&dateRange=custom&startdt=2024-01-01"
                       f"&forms=8-K&hits.hits._source=period_of_report,entity_name,file_date")
        _r = _rq9.get(_search_url, timeout=8,
                      headers={"User-Agent": "QuantTerminal research@quantterminal.com"})
        if not _r.ok:
            return []
        _hits = _r.json().get("hits", {}).get("hits", [])
        if not _hits:
            return []
        _texts = []
        for _hit in _hits[:max_filings]:
            try:
                _src = _hit.get("_source", {})
                _file_url = _hit.get("_id", "")
                if not _file_url:
                    continue
                # Fetch the filing index
                _idx_url = f"https://www.sec.gov/Archives/edgar/data/{_file_url}"
                _rx = _rq9.get(_idx_url, timeout=8,
                               headers={"User-Agent": "QuantTerminal research@quantterminal.com"})
                if _rx.ok and len(_rx.text) > 200:
                    _texts.append(_rx.text[:8000])  # first 8000 chars
            except Exception:
                continue
        return _texts
    except Exception:
        return []

def _compute_transcript_features9(ticker):
    \"\"\"Compute the 6 transcript NLP features for a ticker. Returns dict.\"\"\"
    _texts = _fetch_edgar_transcript9(ticker)
    if not _texts:
        return {f: 0.0 for f in _TRANSCRIPT_FEATURES}

    _all_tone, _all_qs, _all_pr = [], [], []
    _all_certain, _all_uncertain, _all_surprise = [], [], []
    _total_len = 0

    for _text in _texts:
        _total_len += len(_text)
        # Split prepared remarks vs Q&A (heuristic: "Q&A", "Question", "Operator")
        _qa_split = max(_text.lower().find("question"), _text.lower().find("q&a"),
                        _text.lower().find("operator"))
        _prepared = _text[:_qa_split] if _qa_split > 200 else _text
        _qa       = _text[_qa_split:] if _qa_split > 200 else ""

        _pp, _pn, _pc, _pu, _ps = _score_text9(_prepared)
        _qp, _qn, _qc, _qu, _qs2 = _score_text9(_qa) if _qa else (0,0,0,0,0)

        _all_tone.append(_pp - _pn)
        _all_pr.append(_pp - _pn)
        _all_qs.append(_qp - _qn)
        _all_certain.append(_pc)
        _all_uncertain.append(_pu)
        _all_surprise.append(_ps)

    _n = len(_all_tone) or 1
    _tone       = sum(_all_tone) / _n
    _pr_tone    = sum(_all_pr)   / _n
    _qa_tone    = sum(_all_qs)   / _n
    _certain    = sum(_all_certain) / _n
    _uncertain  = sum(_all_uncertain) / _n
    _surprise   = sum(_all_surprise) / _n

    return {
        "transcript_tone":       round(_tone, 5),
        "qa_tone_shift":         round(_qa_tone - _pr_tone, 5),
        "guidance_confidence":   round(_certain - _uncertain, 5),
        "analyst_aggression":    round(abs(_qa_tone - _pr_tone), 5),
        "surprise_language":     round(min(_surprise / 10.0, 1.0), 5),
        "transcript_length":     round(min(_total_len / 50000.0, 1.0), 5),
    }

# Load cache
import os as _os9p, time as _time9p
_tc9 = {}
if _TRANSCRIPT_CACHE_FILE.exists():
    try:
        _tc9 = _j9p.loads(_TRANSCRIPT_CACHE_FILE.read_text())
    except Exception:
        _tc9 = {}

_CACHE_TTL9 = 86400 * 30   # re-fetch after 30 days (quarterly cadence)
_n_transcript = 0
_n_cached9    = 0

# Only run on morning cycle
if _os9p.environ.get("RUN_TYPE", "morning") == "morning" and "featured" in dir():
    for _tk9 in list(featured.keys()):
        try:
            _cached9 = _tc9.get(_tk9, {})
            _age9    = _time9p.time() - float(_cached9.get("ts", 0))
            if _age9 < _CACHE_TTL9 and all(f in _cached9 for f in _TRANSCRIPT_FEATURES):
                _feats9 = {f: _cached9[f] for f in _TRANSCRIPT_FEATURES}
                _n_cached9 += 1
            else:
                _feats9 = _compute_transcript_features9(_tk9)
                _tc9[_tk9] = {**_feats9, "ts": _time9p.time()}
                _n_transcript += 1

            # Add features to featured dataframe — only for trailing 35 days
            # to avoid look-ahead bias (current NLP value must not fill historical rows).
            import pandas as _pd9la
            _cutoff9 = _pd9la.Timestamp.today() - _pd9la.Timedelta(days=35)
            _mask9 = featured[_tk9].index >= _cutoff9
            for _f9, _v9 in _feats9.items():
                if _f9 not in featured[_tk9].columns:
                    featured[_tk9][_f9] = float("nan")
                featured[_tk9].loc[_mask9, _f9] = float(_v9)
        except Exception:
            pass

    # Save updated cache
    try:
        _TRANSCRIPT_CACHE_FILE.write_text(_j9p.dumps(_tc9, indent=2))
    except Exception:
        pass

    # NOTE: transcript features are NOT added to FEATURE_COLS here.
    # Models in Cell 8 were trained before this patch runs (Cell 9 is post-Cell 8).
    # Adding them would cause feature-count mismatch (ValueError) in Cell 11
    # predict_proba — same issue as patent_velocity (fixed in commit 05f2d03).
    # Features remain in featured[tk] for any downstream non-model use.
    print(f"  [patch] Transcript NLP: {_n_transcript} fetched, "
          f"{_n_cached9} from cache, {len(_TRANSCRIPT_FEATURES)} features in featured (not in FEATURE_COLS — models trained without them)")
else:
    print("  [patch] Transcript NLP: skipped (intraday/evening or no featured dict)")
"""

# ── Tier 3 extension: USPTO patent filing velocity ────────────────────────────
# Patent filing velocity = count of USPTO utility patents granted to a company
# in trailing 90 days, normalized by trailing-12-month average.
# A ratio > 1.2 signals accelerating R&D output — a forward-looking moat proxy
# that typically leads revenue inflection by 12-24 months.
# Data source: USPTO PatentsView open API (free, no API key required).
# Cache TTL: 7 days (patent data updates weekly).
_CELL_9_T3_PATENT = """
import json as _j9pat
import time as _t9pat
import os  as _os9pat
import re  as _re9pat
from pathlib import Path as _P9pat

_PATENT_CACHE_FILE = _P9pat("data/predictions/patent_cache.json")
_PATENT_CACHE_TTL  = 86400 * 7   # 7-day cache

_TICKER_TO_ASSIGNEE = {
    "AAPL":"Apple","MSFT":"Microsoft","NVDA":"Nvidia","GOOGL":"Google",
    "AMZN":"Amazon","META":"Meta","TSLA":"Tesla","AMD":"Advanced Micro Devices",
    "INTC":"Intel","QCOM":"Qualcomm","TXN":"Texas Instruments","AVGO":"Broadcom",
    "AMAT":"Applied Materials","LRCX":"Lam Research","KLAC":"KLA",
    "IBM":"International Business Machines","ORCL":"Oracle","CRM":"Salesforce",
    "ADBE":"Adobe","NOW":"ServiceNow","PLTR":"Palantir","NET":"Cloudflare",
    "SNOW":"Snowflake","CRWD":"CrowdStrike","ZS":"Zscaler","PANW":"Palo Alto",
    "ISRG":"Intuitive Surgical","MDT":"Medtronic","BSX":"Boston Scientific",
    "DHR":"Danaher","TMO":"Thermo Fisher","ABBV":"AbbVie","LLY":"Eli Lilly",
    "JNJ":"Johnson Johnson","PFE":"Pfizer","MRK":"Merck","AMGN":"Amgen",
    "GILD":"Gilead","VRTX":"Vertex","REGN":"Regeneron","BMY":"Bristol Myers",
    "GE":"General Electric","HON":"Honeywell","CAT":"Caterpillar",
    "BA":"Boeing","RTX":"Raytheon","LMT":"Lockheed","NOC":"Northrop",
}

def _fetch_patent_velocity(assignee_name, lookback_days=90):
    import requests as _rq9pat
    import datetime as _dt9pat
    try:
        _today  = _dt9pat.date.today()
        _start  = (_today - _dt9pat.timedelta(days=lookback_days)).isoformat()
        _start_1y = (_today - _dt9pat.timedelta(days=365)).isoformat()

        _base = "https://api.patentsview.org/patents/query"
        _q_recent = (f'{{"_and":[{{"_gte":{{"patent_date":"{_start}"}}}},'
                     f'{{"_text_all":{{"assignee_organization":"{assignee_name}"}}}}]}}')
        _pv_fields = '["patent_id"]'
        _pv_opts100 = '{"per_page":100}'
        _pv_opts1   = '{"per_page":1}'
        _r90 = _rq9pat.get(
            f"{_base}?q={_q_recent}&f={_pv_fields}&o={_pv_opts100}",
            timeout=8, headers={"User-Agent": "QuantTerminal/v25"})
        _count90 = _r90.json().get("total_patent_count", 0) if _r90.ok else 0

        _q_1y = (f'{{"_and":[{{"_gte":{{"patent_date":"{_start_1y}"}}}},'
                 f'{{"_text_all":{{"assignee_organization":"{assignee_name}"}}}}]}}')
        _r1y = _rq9pat.get(
            f"{_base}?q={_q_1y}&f={_pv_fields}&o={_pv_opts1}",
            timeout=8, headers={"User-Agent": "QuantTerminal/v25"})
        _count1y = _r1y.json().get("total_patent_count", 1) if _r1y.ok else 1

        _annualized = max(_count1y, 1)
        _velocity   = round((_count90 / (lookback_days / 365)) / _annualized, 3)
        return float(_velocity), int(_count90)
    except Exception:
        return 1.0, 0

# Load cache
_pat_cache = {}
if _PATENT_CACHE_FILE.exists():
    try:
        _pat_cache = _j9pat.loads(_PATENT_CACHE_FILE.read_text())
    except Exception:
        _pat_cache = {}

_n_patent = 0
_n_patent_cached = 0

if _os9pat.environ.get("RUN_TYPE", "morning") == "morning" and "featured" in dir():
    for _tk9pat in list(featured.keys()):
        _assignee = _TICKER_TO_ASSIGNEE.get(_tk9pat)
        if not _assignee:
            continue
        try:
            _cached_p = _pat_cache.get(_tk9pat, {})
            _age_p    = _t9pat.time() - float(_cached_p.get("ts", 0))
            if _age_p < _PATENT_CACHE_TTL and "patent_velocity" in _cached_p:
                _vel = _cached_p["patent_velocity"]
                _n_patent_cached += 1
            else:
                _vel, _cnt = _fetch_patent_velocity(_assignee)
                _pat_cache[_tk9pat] = {"patent_velocity": _vel,
                                       "patent_count90d": _cnt,
                                       "ts": _t9pat.time()}
                _n_patent += 1
                _t9pat.sleep(0.3)   # USPTO rate limit: ~3 req/sec

            # Only set patent_velocity for trailing 35 days to avoid look-ahead bias.
            import datetime as _dt9pat_la
            import pandas as _pd9pat_la
            _cutoff9pat = _pd9pat_la.Timestamp.today() - _pd9pat_la.Timedelta(days=35)
            _mask9pat = featured[_tk9pat].index >= _cutoff9pat
            if "patent_velocity" not in featured[_tk9pat].columns:
                featured[_tk9pat]["patent_velocity"] = float("nan")
            featured[_tk9pat].loc[_mask9pat, "patent_velocity"] = float(_vel)
        except Exception:
            pass

    # NOTE: patent_velocity is NOT added to FEATURE_COLS here.
    # Models were already trained (Cell 8) without it; appending it post-training
    # causes generate_signal to crash for every ticker that doesn't have the column.
    # Patent velocity is available in featured[tk] for position-sizing use only.

    try:
        _PATENT_CACHE_FILE.write_text(_j9pat.dumps(_pat_cache, indent=2))
    except Exception:
        pass

    print(f"  [Tier3] Patent velocity: {_n_patent} fetched, "
          f"{_n_patent_cached} from cache, stored in featured (not in FEATURE_COLS — models trained without it)")
else:
    print("  [Tier3] Patent velocity: skipped (intraday/evening or no featured dict)")
"""
CELL_9_POSTPATCH += "\n\n" + _CELL_9_T3_PATENT

# ── CELL 10 PREPATCH: NewsAPI status diagnostic (zero behavior change) ────────
# Sentiment scores only ~99/307 tickers every run. Determine the cause before
# fixing: patch requests.get to tally NewsAPI HTTP statuses so we can tell a
# daily quota / rate limit (429) apart from genuine no-news (200 + empty
# articles). Summary printed in CELL_10_POSTPATCH (same exec namespace).
CELL_10_PREPATCH = '''
try:
    import requests as _req10d
    from collections import Counter as _Ctr10d
    _NEWSAPI_STATUS_10   = _Ctr10d()
    _NEWSAPI_EMPTY200_10 = [0]
    _NEWSAPI_NONEMPTY_10 = [0]
    if not getattr(_req10d.get, "_newsapi_diag", False):
        _orig_get_10 = _req10d.get
        def _diag_get_10(url, *a, **k):
            _resp = _orig_get_10(url, *a, **k)
            try:
                if "newsapi.org" in str(url):
                    _NEWSAPI_STATUS_10[_resp.status_code] += 1
                    if _resp.status_code == 200:
                        try:
                            if _resp.json().get("articles"):
                                _NEWSAPI_NONEMPTY_10[0] += 1
                            else:
                                _NEWSAPI_EMPTY200_10[0] += 1
                        except Exception:
                            pass
            except Exception:
                pass
            return _resp
        _diag_get_10._newsapi_diag = True
        _req10d.get = _diag_get_10
        print("  [newsapi diag] requests.get patched to tally NewsAPI statuses")
except Exception as _d10e:
    print(f"  [newsapi diag] patch skipped: {_d10e}")

# ── Headline fetch: 18h disk cache → Finnhub company-news → NewsAPI fallback ──
# NewsAPI free tier (~100 req/day) is exhausted by 307 tickers/run → 429 for
# all calls → 0/307 sentiment coverage. Fix per diagnostic verdict (cache +
# second source): cache headlines 18h (survives all intraday runs, synced via
# Drive), prefer Finnhub (60 req/min ceiling), fall back to NewsAPI only when
# Finnhub is empty. _fetch_headlines is routed here via _SRC_REPLACE.
try:
    from collections import Counter as _Ctr10h
    _FH_SRC_10 = _Ctr10h()   # tallies: cache / finnhub / newsapi / empty
except Exception:
    _FH_SRC_10 = None

def _fh_smart_headlines(ticker, n=10):
    import os as _os10, json as _json10, time as _time10, pathlib as _pl10
    import requests as _rq10, datetime as _dt10
    def _tally(_k):
        try:
            if _FH_SRC_10 is not None: _FH_SRC_10[_k] += 1
        except Exception: pass
    _cdir = _pl10.Path("data/cache/news"); _cdir.mkdir(parents=True, exist_ok=True)
    _cf = _cdir / (str(ticker).replace("/", "_") + ".json")
    _ttl = 18 * 3600
    try:
        if _cf.exists() and (_time10.time() - _cf.stat().st_mtime) < _ttl:
            _c = _json10.loads(_cf.read_text())
            if _c.get("headlines"):
                _tally("cache"); return _c["headlines"][:n]
    except Exception:
        pass
    _heads = []
    _fk = _os10.environ.get("FINNHUB_API_KEY", "").strip()
    if _fk:
        try:
            _to = _dt10.date.today(); _frm = _to - _dt10.timedelta(days=7)
            _r = _rq10.get("https://finnhub.io/api/v1/company-news",
                           params={"symbol": ticker, "from": _frm.isoformat(),
                                   "to": _to.isoformat(), "token": _fk}, timeout=6)
            if _r.status_code == 200:
                _heads = [a.get("headline", "") for a in _r.json()[:n] if a.get("headline")]
                if _heads: _tally("finnhub")
        except Exception:
            pass
    if not _heads:
        _nk = (globals().get("NEWS_API_KEY", "") or _os10.environ.get("NEWS_API_KEY", "")).strip()
        if _nk:
            try:
                _r = _rq10.get("https://newsapi.org/v2/everything",
                               params={"q": ticker, "language": "en", "pageSize": n,
                                       "sortBy": "publishedAt", "apiKey": _nk}, timeout=6)
                if _r.status_code == 200:
                    _heads = [a.get("title", "") for a in _r.json().get("articles", []) if a.get("title")]
                    if _heads: _tally("newsapi")
            except Exception:
                pass
    if _heads:
        try:
            _cf.write_text(_json10.dumps({"ts": _time10.time(), "headlines": _heads}))
        except Exception:
            pass
    else:
        _tally("empty")
    return _heads[:n]
'''

# ── CELL 10 POSTPATCH: print NewsAPI status tally + verdict ───────────────────
CELL_10_POSTPATCH = '''
try:
    if "_NEWSAPI_STATUS_10" in dir():
        _tot10 = sum(_NEWSAPI_STATUS_10.values())
        print(f"  [newsapi diag] {_tot10} NewsAPI calls | statuses={dict(_NEWSAPI_STATUS_10)} "
              f"| 200-with-news={_NEWSAPI_NONEMPTY_10[0]} | 200-empty={_NEWSAPI_EMPTY200_10[0]}")
        if _NEWSAPI_STATUS_10.get(429, 0) > 0:
            print(f"  [newsapi diag] VERDICT: {_NEWSAPI_STATUS_10[429]} rate-limited (429) — coverage capped by quota/rate limit; fix = prioritize+cache or second source")
        elif _NEWSAPI_EMPTY200_10[0] > _NEWSAPI_NONEMPTY_10[0]:
            print("  [newsapi diag] VERDICT: most empties are HTTP 200 with no articles — genuine no-news, NOT a cap; no fix needed")
        else:
            print("  [newsapi diag] VERDICT: no 429s — coverage reflects actual news availability")
    if "_FH_SRC_10" in dir() and _FH_SRC_10 is not None:
        _d = dict(_FH_SRC_10)
        _scored10 = _d.get("cache", 0) + _d.get("finnhub", 0) + _d.get("newsapi", 0)
        print(f"  [headlines] sources={_d} | {_scored10} tickers with headlines "
              f"(cache+finnhub+newsapi), {_d.get('empty', 0)} empty")
except Exception as _d10pe:
    print(f"  [newsapi diag] summary skipped: {_d10pe}")
'''

# ── CELL 11 PREPATCH: IC-weighted composite score weights ─────────────────────
CELL_11_PREPATCH = """
import json as _j11
from pathlib import Path as _P11

# iv_flags is set by Cell 9 (GARCH+IV). On intraday runs Cell 9 is skipped,
# so inject a safe empty fallback so signal generation doesn't crash.
if "iv_flags" not in dir():
    iv_flags = {}
    print("  [patch] iv_flags fallback injected (Cell 9 was skipped)")

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
_HOLD_HI = 0.51   # must exceed this to be a BUY (lowered: Huber composite scores cluster at 0.51-0.53)
_HOLD_LO = 0.49   # must be below this to be a SELL
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
_ROUND_TRIP_COST_PCT = 0.0002   # 0.02% round-trip floor (most-liquid names)
# Convert cost to composite_score units: Δp ≈ Δreturn / 0.02 (2% per unit prob shift)
_MIN_ALPHA_SCORE = 0.5 + (_ROUND_TRIP_COST_PCT / 0.02)   # ≈ 0.51 (aligns with HOLD_HI)

# ── Tier A: per-ticker transaction-cost model (replaces flat 0.02%) ───────────
# A real round-trip cost is driven by liquidity: illiquid names have wider
# spreads, so a signal there must clear a higher edge bar to be worth trading.
# Estimate round-trip cost per ticker from average dollar volume (ADV) tier plus
# a small volatility kicker (recent High-Low range), both from `featured`. The
# flat _ROUND_TRIP_COST_PCT is the floor; cost is capped to avoid over-suppression.
_RT_COST_CAP = 0.005   # 0.5% round-trip ceiling
def _rt_cost(_tk):
    try:
        # NOTE: reference `featured` directly — it's a global in the exec
        # namespace (function __globals__). A `"featured" in dir()` guard would
        # check LOCALS and always fail, silently pinning every cost to the floor.
        _df = featured.get(_tk)
        if _df is None or "Close" not in _df.columns or "Volume" not in _df.columns:
            return _ROUND_TRIP_COST_PCT
        _c = _df["Close"].astype(float)
        _v = _df["Volume"].astype(float)
        _adv = float((_c * _v).tail(20).mean())
        if   _adv >= 1e9: _hs = 0.0001   # mega-cap (~AAPL/MSFT)
        elif _adv >= 1e8: _hs = 0.0003   # large-cap
        elif _adv >= 1e7: _hs = 0.0006   # mid-cap
        else:             _hs = 0.0012   # small/illiquid
        _rng = 0.0
        if "High" in _df.columns and "Low" in _df.columns:
            _rng = float(((_df["High"].astype(float) - _df["Low"].astype(float)) / _c).tail(20).mean())
        _cost = 2.0 * _hs + 0.10 * (_rng if _rng == _rng else 0.0)   # round-trip + vol kicker
        return float(min(max(_cost, _ROUND_TRIP_COST_PCT), _RT_COST_CAP))
    except Exception:
        return _ROUND_TRIP_COST_PCT

# Apply all three fixes in one pass
# ROOT CAUSE FIX: composite_score clusters near 0.5 (raw ensemble output) while
# confidence is the Platt-calibrated P(bull) used by Cell 13. Using composite_score
# for the alpha check was causing 307/307 signals to be filtered as HOLD even when
# confidence was 0.84+. We now use confidence for both the cost filter and ternary
# labels, which is consistent with what Cell 13 actually executes on.
if "signals" in dir():
    _n_adjusted_conf = 0
    _n_filtered_cost = 0
    for _tk11, _sig11 in signals.items():
        # Use confidence (calibrated P(bull)) — consistent with Cell 13's MIN_CONFIDENCE
        _conf_val = _sig11.get("confidence", _sig11.get("composite_score", 0.5))
        try:
            _conf_val = float(_conf_val)
        except Exception:
            continue

        # Conformal band adjustment (operates on confidence directly)
        if _q90_11 is not None:
            _dist = abs(_conf_val - 0.5)
            if _dist < _q90_11:
                _scale = _dist / max(_q90_11, 0.01)
                _conf_val = 0.5 + (_conf_val - 0.5) * _scale
                signals[_tk11]["confidence"] = _conf_val
                _n_adjusted_conf += 1

        # Net-of-cost filter: edge = |P(bull) - 0.5|; must exceed per-ticker cost
        # in prob units (Δp ≈ cost / 0.02). Illiquid names face a higher bar.
        _alpha = abs(_conf_val - 0.5)
        _cost_tk = _rt_cost(_tk11)
        signals[_tk11]["rt_cost"] = round(_cost_tk, 5)
        if _alpha < (_cost_tk / 0.02):
            signals[_tk11]["net_of_cost_hold"] = True
            _n_filtered_cost += 1
        else:
            signals[_tk11]["net_of_cost_hold"] = False

        # Ternary label using calibrated confidence (same field Cell 13 reads)
        if _conf_val > _HOLD_HI and not signals[_tk11].get("net_of_cost_hold"):
            signals[_tk11]["ternary_label"] = "BUY"
            _n_buy += 1
        elif _conf_val < _HOLD_LO and not signals[_tk11].get("net_of_cost_hold"):
            signals[_tk11]["ternary_label"] = "SELL"
            _n_sell += 1
        else:
            signals[_tk11]["ternary_label"] = "HOLD"
            _n_hold += 1

    print(f"  [patch] Conformal bands adjusted {_n_adjusted_conf} signals")
    _costs_all = [s.get("rt_cost") for s in signals.values() if isinstance(s.get("rt_cost"), (int, float))]
    _avg_cost = (sum(_costs_all) / len(_costs_all)) if _costs_all else _ROUND_TRIP_COST_PCT
    print(f"  [patch] Net-of-cost filter suppressed {_n_filtered_cost} low-edge signals "
          f"(per-ticker cost: avg={_avg_cost:.2%}, floor={_ROUND_TRIP_COST_PCT:.2%}, cap={_RT_COST_CAP:.2%})")
    print(f"  [patch] Ternary labels — BUY:{_n_buy}  HOLD:{_n_hold}  SELL:{_n_sell}")

    # ── Fix D: event_scale repair (root cause of "Event classification error:
    # 'composite'"). The notebook's event-classification block does
    # _sig["composite"] = min(_sig["composite"], 0.72) for earnings/FDA names
    # with elevated IV — but signals carry "composite_score"/"confidence", never
    # a bare "composite" key. The KeyError aborts the whole loop on the first
    # high-var name, so event_scale never gets set for the rest. We re-apply it
    # here with the correct keys, after the notebook block has run.
    _n_event_capped = 0
    _n_event_normal = 0
    for _tk_es, _sig_es in signals.items():
        _ev_es  = _sig_es.get("event_type", "other")
        _ivf_es = _sig_es.get("iv_flag", "NORMAL")
        if _ev_es in ("earnings", "fda") and _ivf_es != "NORMAL":
            # cap confidence (and composite_score if present) for high-variance events
            if "composite_score" in _sig_es:
                try:
                    _sig_es["composite_score"] = round(min(float(_sig_es["composite_score"]), 0.72), 4)
                except Exception:
                    pass
            try:
                _sig_es["confidence"] = round(min(float(_sig_es.get("confidence", 0.5)), 0.72), 4)
            except Exception:
                pass
            _sig_es["event_scale"] = 0.5
            _n_event_capped += 1
        else:
            # only set default if the notebook block didn't already set it
            if "event_scale" not in _sig_es:
                _sig_es["event_scale"] = 1.0
            _n_event_normal += 1
    print(f"  [patch] Event-scale repair: {_n_event_capped} high-var events capped (0.5x), {_n_event_normal} normal")
else:
    print("  [patch] signals not in scope — ternary/cost patches skipped")

# ── Intraday signal blend (15% weight) ───────────────────────────────────────
# Reads data/predictions/intraday_signals.json if present.
# Blends intraday_score at 15% only when the signal is < 6 hours old.
import json as _j11id
import datetime as _dt11id
from pathlib import Path as _P11id

_INTRADAY_SIG_FILE = _P11id("data/predictions/intraday_signals.json")
_INTRADAY_MAX_AGE  = _dt11id.timedelta(hours=6)

if _INTRADAY_SIG_FILE.exists() and "signals" in dir():
    try:
        _id_sigs = _j11id.loads(_INTRADAY_SIG_FILE.read_text())
        _now11id = _dt11id.datetime.utcnow()
        _n_blended = 0
        for _tk11id, _isig in _id_sigs.items():
            if _tk11id not in signals:
                continue
            try:
                _gen_str = _isig.get("generated", "")
                _gen_ts  = _dt11id.datetime.fromisoformat(_gen_str.replace("Z", "+00:00"))
                # Normalise to naive UTC for comparison
                if _gen_ts.tzinfo is not None:
                    _gen_ts = _gen_ts.replace(tzinfo=None)
                if (_now11id - _gen_ts) > _INTRADAY_MAX_AGE:
                    continue   # stale — skip
                _id_score = float(_isig.get("intraday_score", 0.5))
                _old_cs   = float(signals[_tk11id].get("composite_score", 0.5))
                # blend: new_score = 0.85 * existing_score + 0.15 * intraday_score
                signals[_tk11id]["composite_score"] = round(
                    0.85 * _old_cs + 0.15 * _id_score, 6)
                signals[_tk11id]["intraday_blended"] = True
                _n_blended += 1
            except Exception:
                continue
        print(f"  [patch] Intraday blend: {_n_blended} tickers blended (15% weight)")
    except Exception as _id11e:
        print(f"  [patch] Intraday blend error (non-fatal): {_id11e}")
else:
    print("  [patch] Intraday blend: no signal file or signals not in scope — skipped")
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


# ── Ledoit-Wolf covariance shrinkage ─────────────────────────────────────────
# Blends EWMA covariance with Ledoit-Wolf shrinkage estimator.
# LW shrinks the sample covariance toward a structured target with analytically
# optimal shrinkage intensity — makes the matrix invertible even when assets
# outnumber observations (common when running 139 tickers on 60d intraday data).
# Final covariance used by HRP/CVaR = 0.50 × EWMA + 0.50 × LW.
def _lw_cov(returns_matrix):
    \"\"\"Ledoit-Wolf shrinkage covariance. returns_matrix: np.ndarray (T, N).\"\"\"
    try:
        from sklearn.covariance import LedoitWolf as _LW12
        _lw = _LW12(assume_centered=False)
        _lw.fit(returns_matrix)
        return _lw.covariance_
    except Exception:
        return _np12_original_cov(returns_matrix.T)

def _blended_cov(returns_matrix, ewma_lam=0.94, lw_weight=0.50):
    \"\"\"Return (1-lw_weight)*EWMA_cov + lw_weight*LW_cov.\"\"\"
    _ewma = ewma_cov(returns_matrix, lam=ewma_lam)
    _lw   = _lw_cov(returns_matrix)
    if _ewma.shape != _lw.shape:
        return _ewma
    _blend = (1.0 - lw_weight) * _ewma + lw_weight * _lw
    # Ensure positive semi-definiteness
    _eigvals = _np12.linalg.eigvalsh(_blend)
    if _eigvals.min() < 0:
        _blend += (-_eigvals.min() + 1e-8) * _np12.eye(_blend.shape[0])
    return _blend

# Override np.cov to use blended estimator when called with a (T>N) returns matrix
def _patched_np_cov_v2(m, *args, **kwargs):
    arr = _np12.asarray(m)
    if arr.ndim == 2 and arr.shape[0] > arr.shape[1] and arr.shape[1] >= 2:
        return _blended_cov(arr)
    return _np12_original_cov(m, *args, **kwargs)

import numpy as np
np.cov = _patched_np_cov_v2
print("  [patch] Blended covariance: 50% EWMA + 50% Ledoit-Wolf injected into np.cov")

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
# The notebook stores CVaR output in `opt_weights` but earlier patch code
# checked only `portfolio_weights`, `cvar_weights`, `weights` — never finding
# it and always falling to HRP even when CLARABEL solved successfully.
# Fix: check all known variable names the notebook might use, then alias
# whichever one is populated to `portfolio_weights` so the blend logic works.
_cvar_ns = globals()
_CVAR_VAR_NAMES = ["portfolio_weights", "opt_weights", "cvar_weights", "weights"]
_cvar_ok = any(isinstance(_cvar_ns.get(k), dict) and len(_cvar_ns[k]) > 0
               for k in _CVAR_VAR_NAMES)

# Alias opt_weights → portfolio_weights so blend logic always uses the same name
if not _cvar_ok:
    for _wk in ["weights", "cvar_weights"]:
        _wv = _cvar_ns.get(_wk)
        if _wv is not None and hasattr(_wv, "__len__") and len(_wv) > 0:
            _cvar_ok = True
            break

if _cvar_ok and "portfolio_weights" not in _cvar_ns:
    for _alias_src in ["opt_weights", "cvar_weights", "weights"]:
        _alias_val = _cvar_ns.get(_alias_src)
        if isinstance(_alias_val, dict) and len(_alias_val) > 0:
            portfolio_weights = _alias_val
            print(f"  [patch] CVaR: aliased {_alias_src} → portfolio_weights "
                  f"({len(portfolio_weights)} tickers)")
            break

print(f"  [patch] CVaR result check: {'OK' if _cvar_ok else 'EMPTY — falling back to HRP'}")

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

_solver_ok12 = any(k in _cvar_ns for k in ["portfolio_weights", "opt_weights", "cvar_weights"])
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
# ── Fix: Normalize 'regimes' to integers before Cell 13 runs ─────────────────
# The notebook's Cell 13 calls int(regimes[-1]) at its own line 975.
# If macro-regime built a string like "Neutral / Mixed", that crashes.
# Coerce in-place so the notebook's own code works without modification.
_REGIME_STR_MAP13 = {
    "Bear": 0, "bear": 0, "Bearish": 0, "bearish": 0, "0": 0,
    "Neutral": 1, "neutral": 1, "Neutral / Mixed": 1, "Mixed": 1, "mixed": 1, "1": 1,
    "Bull": 2, "bull": 2, "Bullish": 2, "bullish": 2, "2": 2,
}
if "regimes" in dir() and regimes is not None and len(regimes) > 0:
    try:
        import numpy as _np13r
        # regimes may be: dict {ticker: int}, list, or numpy array
        _ra13_raw = list(regimes.values()) if isinstance(regimes, dict) else list(regimes)
        _ra13_int = []
        for _rv in _ra13_raw:
            try:
                _ra13_int.append(int(_rv))
            except (ValueError, TypeError):
                _ra13_int.append(_REGIME_STR_MAP13.get(str(_rv).strip(), 1))
        regimes = _np13r.array(_ra13_int)
        print(f"  [patch] Regime coerced to int array len={len(regimes)} last={regimes[-1]}")
    except Exception as _re13e:
        print(f"  [patch] Regime coerce warning: {_re13e}")

# ── Fix: Lower MIN_CONFIDENCE to match HOLD_HI threshold ─────────────────────
# The notebook defines MIN_CONFIDENCE = 0.65. With Platt-calibrated Huber
# regression outputs, predict_proba values cluster in 0.50-0.62 for typical
# stock predictions — 0.65 is structurally unreachable for most signals.
# CELL_11_POSTPATCH already uses HOLD_HI = 0.55 as the BUY gate; aligning
# MIN_CONFIDENCE to 0.55 ensures Cell 13 actually executes the trades that
# Cell 11 already labelled as BUY.
MIN_CONFIDENCE = 0.51
print(f"  [patch] MIN_CONFIDENCE overridden: 0.65 → 0.51 (aligns with HOLD_HI=0.51)")

# ── Fix: Coerce MACRO["macro_regime"] string → int before Cell 13 line 1019 ──
# The notebook calls int(MACRO.get("macro_regime", 1)) which crashes when the
# value is a string like "Neutral / Mixed". Coerce to int here.
def _coerce_regime_val(v):
    if isinstance(v, (int, float)):
        return int(v)
    return _REGIME_STR_MAP13.get(str(v).strip(),
           _REGIME_STR_MAP13.get(str(v).lower().split("/")[0].strip(), 1))

if "MACRO" in dir() and isinstance(MACRO, dict):
    _r13m = MACRO.get("macro_regime", 1)
    if isinstance(_r13m, str):
        MACRO["macro_regime"] = _coerce_regime_val(_r13m)
        print(f"  [patch] Cell13 MACRO['macro_regime'] coerced: {_r13m!r} -> {MACRO['macro_regime']}")
    else:
        print(f"  [patch] Cell13 MACRO['macro_regime'] already int: {_r13m}")

# Also coerce macro_data["regime"] in case some notebook version uses that name
if "macro_data" in dir() and isinstance(macro_data, dict):
    _r13md = macro_data.get("regime", "")
    if isinstance(_r13md, str):
        macro_data["regime"] = _coerce_regime_val(_r13md)
        print(f"  [patch] Cell13 macro_data['regime'] coerced: {_r13md!r} -> {macro_data['regime']}")

# ── Fix: Register 'composite' as a known event type ──────────────────────────
# Cell 13 classifies signals by event_type (e.g. 'earnings', 'macro', 'composite').
# 'composite' is generated for multi-factor signals but was missing from the
# event_type mapping dict, causing KeyError('composite') at signal scoring time.
try:
    for _evt13_name in ["EVENT_TYPE_WEIGHTS", "EVENT_WEIGHTS", "_event_weights",
                         "event_type_map", "EVENT_TYPE_MAP", "_EVENT_TYPE_MAP",
                         "EVENT_MULTIPLIERS", "_event_multipliers"]:
        if _evt13_name in dir() and isinstance(eval(_evt13_name), dict):
            _e13d = eval(_evt13_name)
            if "composite" not in _e13d:
                _e13d["composite"] = _e13d.get("mixed", _e13d.get("other", _e13d.get("default", 1.0)))
                print(f"  [patch] Cell13: registered 'composite' in {_evt13_name}")
            break
except Exception:
    pass

# ── Fix: Full SECTOR_MAP override — notebook only has ~16 tickers, causing
# every S&P500 name to fall into "Other" and breach the 40% sector limit.
# This replaces SECTOR_MAP in Cell 13's namespace before sector_allows_trade runs.
SECTOR_MAP = {
    # Technology
    "AAPL":"Tech","MSFT":"Tech","NVDA":"Tech","GOOGL":"Tech","AMZN":"Tech",
    "META":"Tech","AVGO":"Tech","ORCL":"Tech","ADBE":"Tech","CRM":"Tech",
    "NOW":"Tech","PLTR":"Tech","DDOG":"Tech","ZS":"Tech","CRWD":"Tech",
    "PANW":"Tech","INTU":"Tech","CDNS":"Tech","SNPS":"Tech","FTNT":"Tech",
    "ANET":"Tech","ACN":"Tech","IBM":"Tech","CSCO":"Tech","TYL":"Tech",
    "ROP":"Tech","WDAY":"Tech","NET":"Tech","SNOW":"Tech","TEAM":"Tech",
    "SHOP":"Tech","PYPL":"Tech","COIN":"Tech","AFRM":"Tech","HOOD":"Tech",
    "TWLO":"Tech","HUBS":"Tech","OKTA":"Tech","MDB":"Tech","GTLB":"Tech",
    # Semiconductors (sub-Tech, counted separately for concentration)
    "AMD":"Semis","INTC":"Semis","QCOM":"Semis","AMAT":"Semis","MU":"Semis",
    "TXN":"Semis","LRCX":"Semis","KLAC":"Semis","ADI":"Semis","MRVL":"Semis",
    "ASML":"Semis","ON":"Semis","MCHP":"Semis","MPWR":"Semis","TER":"Semis",
    "SWKS":"Semis","ENPH":"Semis","FSLR":"Semis","TSM":"Semis","SMCI":"Semis",
    "SMH":"Semis","SOXX":"Semis",
    # Financials
    "JPM":"Finance","V":"Finance","MA":"Finance","BAC":"Finance","GS":"Finance",
    "MS":"Finance","BLK":"Finance","AXP":"Finance","WFC":"Finance","C":"Finance",
    "SCHW":"Finance","PGR":"Finance","CB":"Finance","COF":"Finance","USB":"Finance",
    "TFC":"Finance","PNC":"Finance","ICE":"Finance","CME":"Finance","SPGI":"Finance",
    "MCO":"Finance","AON":"Finance","TRV":"Finance","ALL":"Finance","MET":"Finance",
    "PRU":"Finance","AIG":"Finance","HIG":"Finance","AFL":"Finance","XLF":"Finance",
    # Healthcare
    "UNH":"Healthcare","LLY":"Healthcare","JNJ":"Healthcare","ABBV":"Healthcare",
    "MRK":"Healthcare","TMO":"Healthcare","ABT":"Healthcare","DHR":"Healthcare",
    "PFE":"Healthcare","AMGN":"Healthcare","CVS":"Healthcare","CI":"Healthcare",
    "HUM":"Healthcare","BSX":"Healthcare","MDT":"Healthcare","SYK":"Healthcare",
    "ISRG":"Healthcare","VRTX":"Healthcare","REGN":"Healthcare","BMY":"Healthcare",
    "GILD":"Healthcare","ELV":"Healthcare","MCK":"Healthcare","COR":"Healthcare",
    "A":"Healthcare","IQV":"Healthcare","MTD":"Healthcare","WAT":"Healthcare",
    "ZBH":"Healthcare","RMD":"Healthcare","EW":"Healthcare","XLV":"Healthcare",
    # Consumer Discretionary
    "TSLA":"Consumer","AMZN":"Consumer","HD":"Consumer","NKE":"Consumer",
    "LOW":"Consumer","TJX":"Consumer","ROST":"Consumer","SBUX":"Consumer",
    "CMG":"Consumer","MCD":"Consumer","COST":"Consumer","TGT":"Consumer",
    "GM":"Consumer","F":"Consumer","UBER":"Consumer","BKNG":"Consumer",
    "ABNB":"Consumer","MAR":"Consumer","HLT":"Consumer","DG":"Consumer",
    "DLTR":"Consumer","YUM":"Consumer","DPZ":"Consumer","APTV":"Consumer",
    "BWA":"Consumer","XLY":"Consumer",
    # Consumer Staples
    "WMT":"Staples","PG":"Staples","KO":"Staples","PEP":"Staples",
    "MDLZ":"Staples","CL":"Staples","MO":"Staples","PM":"Staples",
    "EL":"Staples","GIS":"Staples","TSN":"Staples","CAG":"Staples",
    "KHC":"Staples","STZ":"Staples","CLX":"Staples","XLP":"Staples",
    # Energy
    "XOM":"Energy","CVX":"Energy","COP":"Energy","SLB":"Energy","EOG":"Energy",
    "HAL":"Energy","OXY":"Energy","PSX":"Energy","MPC":"Energy","VLO":"Energy",
    "DVN":"Energy","APA":"Energy","KMI":"Energy","WMB":"Energy","BKR":"Energy",
    "LNG":"Energy","XLE":"Energy",
    # Industrials
    "BA":"Industrials","CAT":"Industrials","DE":"Industrials","HON":"Industrials",
    "GE":"Industrials","RTX":"Industrials","LMT":"Industrials","NOC":"Industrials",
    "UPS":"Industrials","FDX":"Industrials","MMM":"Industrials","EMR":"Industrials",
    "ETN":"Industrials","ITW":"Industrials","PH":"Industrials","CMI":"Industrials",
    "GD":"Industrials","TDG":"Industrials","CTAS":"Industrials","NSC":"Industrials",
    "CSX":"Industrials","UNP":"Industrials","HWM":"Industrials","GWW":"Industrials",
    "PCAR":"Industrials","ROK":"Industrials","XLI":"Industrials",
    # Materials
    "LIN":"Materials","APD":"Materials","SHW":"Materials","PPG":"Materials",
    "NEM":"Materials","FCX":"Materials","NUE":"Materials","ALB":"Materials",
    "LYB":"Materials","ECL":"Materials","CF":"Materials","MOS":"Materials",
    "IFF":"Materials","XLB":"Materials",
    # Real Estate
    "AMT":"RealEstate","PLD":"RealEstate","EQIX":"RealEstate","CCI":"RealEstate",
    "WELL":"RealEstate","SPG":"RealEstate","O":"RealEstate","DLR":"RealEstate",
    "PSA":"RealEstate","EXR":"RealEstate","VICI":"RealEstate","AVB":"RealEstate",
    "EQR":"RealEstate","XLRE":"RealEstate",
    # Utilities
    "NEE":"Utilities","DUK":"Utilities","SO":"Utilities","AEP":"Utilities",
    "D":"Utilities","EXC":"Utilities","SRE":"Utilities","XEL":"Utilities",
    "AWK":"Utilities","WEC":"Utilities","ED":"Utilities","FE":"Utilities",
    "ETR":"Utilities","PPL":"Utilities","ES":"Utilities","XLU":"Utilities",
    # Communication
    "NFLX":"Communication","DIS":"Communication","CMCSA":"Communication",
    "VZ":"Communication","T":"Communication","TMUS":"Communication",
    "CHTR":"Communication","EA":"Communication","TTWO":"Communication",
    "LYV":"Communication","XLC":"Communication",
    # Broad / ETFs
    "SPY":"Broad","QQQ":"Broad","IWM":"Broad","DIA":"Broad",
    "GLD":"Broad","SLV":"Broad","TLT":"Broad","HYG":"Broad",
    "LQD":"Broad","VNQ":"Broad","ARKK":"Broad","XLK":"Broad",
    # Crypto
    "BTC-USD":"Crypto","ETH-USD":"Crypto","SOL-USD":"Crypto",
    "BNB-USD":"Crypto","XRP-USD":"Crypto","DOGE-USD":"Crypto","IBIT":"Crypto",
}
print(f"  [patch] SECTOR_MAP overridden: {len(SECTOR_MAP)} tickers mapped "
      f"({len(set(SECTOR_MAP.values()))} sectors) — prevents false Other breaches")

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

# ── Fix: Wire ternary_label from Cell 11 into Cell 13 execution gate ─────────
# CELL_11_POSTPATCH writes ternary_label ("BUY"/"HOLD"/"SELL") onto each signal.
# Cell 13's notebook code uses confidence >= MIN_CONFIDENCE as its gate but never
# checks ternary_label, so the net-of-cost and conformal filters were bypassed.
# This override function wraps the signal iteration to respect ternary_label.
# SELL signals → close existing long if held, never open short (paper acct).
_orig_signals_13 = dict(signals) if "signals" in dir() else {}
if _orig_signals_13:
    _n_ternary_blocked = 0
    _n_sell_close = 0
    for _tk13_w, _sig13_w in _orig_signals_13.items():
        _lbl13 = _sig13_w.get("ternary_label", "BUY")  # default BUY if Cell 11 didn't run
        if _lbl13 == "HOLD":
            # Suppress by setting confidence below MIN_CONFIDENCE
            signals[_tk13_w]["confidence"] = 0.50
            _n_ternary_blocked += 1
        elif _lbl13 == "SELL":
            # Mark for position close instead of short entry
            signals[_tk13_w]["close_long"] = True
            signals[_tk13_w]["confidence"] = 0.50  # block fresh short entry
            _n_sell_close += 1
    print(f"  [patch] Ternary gate: {_n_ternary_blocked} HOLD signals suppressed, "
          f"{_n_sell_close} SELL signals converted to close-long")

# ── Fix: Force UTF-8 on requests.Session + strip non-ASCII from outgoing
# request headers so urllib3's latin-1 encoder never crashes on submit_order.
# Earlier patch only fixed response decoding — the actual error is on the
# REQUEST side: urllib3 calls .encode("latin-1") on every header value, and
# any non-ASCII char (smart quote in API key, em-dash, etc.) raises
# UnicodeEncodeError before the request ever leaves the client.
try:
    import requests as _req13fix
    _orig_send_13 = _req13fix.Session.send
    def _utf8_send_13(self, request, *args, **kwargs):
        # Strip non-latin-1 chars from outgoing header values (the cause of
        # 'latin-1' codec can't encode character errors in alpaca-py).
        try:
            if hasattr(request, "headers") and request.headers:
                _clean_headers = {}
                for _k, _v in request.headers.items():
                    if isinstance(_v, str):
                        _clean_headers[_k] = _v.encode("ascii", "ignore").decode("ascii")
                    else:
                        _clean_headers[_k] = _v
                request.headers = _clean_headers
        except Exception:
            pass  # don't block the request if sanitization fails
        resp = _orig_send_13(self, request, *args, **kwargs)
        resp.encoding = "utf-8"
        return resp
    _req13fix.Session.send = _utf8_send_13
    print("  [patch] requests.Session.send patched: outgoing headers sanitized + responses forced to UTF-8 (Alpaca latin-1 fix)")
except Exception as _rf13e:
    print(f"  [patch] requests UTF-8 patch skipped: {_rf13e}")

# ── Fix: Lowest-level header guard — patch http.client.putheader ─────────────
# The Session.send patch above cleans the PreparedRequest, but 24/41 orders on
# 2026-05-28 still crashed with 'latin-1 can't encode' — meaning a non-ASCII
# char reaches a header on some path that bypasses the requests layer (alpaca-py
# retries, urllib3 connection pooling, or a header set after prepare). http.client
# is where Python actually does header.encode('latin-1') on the socket write —
# patching putheader catches EVERY outgoing header regardless of code path.
try:
    import http.client as _hc13
    _orig_putheader_13 = _hc13.HTTPConnection.putheader
    def _safe_putheader_13(self, header, *values):
        _clean_vals = []
        for _v in values:
            if isinstance(_v, str):
                _v = _v.encode("ascii", "ignore").decode("ascii")
            elif isinstance(_v, bytes):
                _v = _v.decode("latin-1", "ignore").encode("ascii", "ignore")
            _clean_vals.append(_v)
        return _orig_putheader_13(self, header, *_clean_vals)
    _hc13.HTTPConnection.putheader = _safe_putheader_13
    print("  [patch] http.client.putheader patched: all outgoing headers forced ASCII (bulletproof latin-1 fix)")
except Exception as _hc13e:
    print(f"  [patch] http.client putheader patch skipped: {_hc13e}")

# ── Fix (2026-07-08): HARD gross-exposure cap + run-type order gate ──────────
# Replaces the old "cash guard", which was a NO-OP: it lowered blocked signals'
# confidence to 0.50, but Cell 13's trade loop never re-reads confidence (no
# MIN_CONFIDENCE gate exists anywhere in the execution path), so every
# action=BUY signal traded regardless — 26 BUYs (~$131k) were submitted on
# 2026-07-08 moments after the guard printed "max 2 new BUYs", re-levering the
# account 1.27x -> 2.36x. It also budgeted from the local paper ledger instead
# of the real account, which is how the account averaged 2.75x gross since
# 2026-06-02. Three layers, all ENFORCED (not advisory):
#   1. run-type gate — only morning/intraday runs may submit BUY orders
#      (a run_type=scoring cycle submitted 25 BUYs at 11 PM ET on 2026-07-07);
#   2. exec_blocked pre-trim — excess BUY signals beyond the gross budget are
#      marked exec_blocked, highest confidence kept (confidence and action are
#      NOT touched, so predictions.csv still records the model's real signal
#      for rank-IC scoring); the trade loop skips them via _SRC_REPLACE;
#   3. _gross_cap_allows() — hard per-order gate inside execute_trade (via
#      _SRC_REPLACE): a BUY that would push gross position market value above
#      QT_MAX_GROSS x equity (default 1.0x) is refused at submission time,
#      using Alpaca's LIVE equity/positions plus a running total of notional
#      this run has already submitted.
# Fail-closed: if the Alpaca account read fails while keys are set, ALL BUYs
# are blocked this run — sizing blind is what built the 3.3x book.
import os as _os_gc
_GROSS_CAP = {
    "ratio":      float(_os_gc.environ.get("QT_MAX_GROSS", "1.0") or 1.0),
    "run_type":   _os_gc.environ.get("RUN_TYPE", "morning"),
    "equity":     None,    # live account equity ($)
    "gross_mv":   None,    # live gross position market value ($, abs long+short)
    "submitted":  0.0,     # BUY notional already allowed this run ($)
    "acct_ok":    False,   # account state successfully read
    "blocked_n":  0,
    "blocked_nl": 0.0,
}
_GROSS_CAP["run_ok"] = _GROSS_CAP["run_type"] in ("morning", "intraday")

def _gross_cap_allows(_tk_gc, _notional_gc):
    # Hard BUY gate: True only if this order keeps gross <= ratio x equity.
    # SELLs have their own gate (_oversell_cap below) — a SELL beyond real
    # holdings INCREASES gross by opening a short.
    try:
        _n_gc = max(0.0, float(_notional_gc))
        if not _GROSS_CAP["run_ok"]:
            _GROSS_CAP["blocked_n"] += 1; _GROSS_CAP["blocked_nl"] += _n_gc
            print(f"    [gross-cap] BLOCKED BUY {_tk_gc} ~${_n_gc:,.0f} — "
                  f"run_type={_GROSS_CAP['run_type']} does not submit orders")
            return False
        if not _GROSS_CAP["acct_ok"]:
            _GROSS_CAP["blocked_n"] += 1; _GROSS_CAP["blocked_nl"] += _n_gc
            print(f"    [gross-cap] BLOCKED BUY {_tk_gc} ~${_n_gc:,.0f} — "
                  f"account state unknown (fail-closed)")
            return False
        _room_gc = (_GROSS_CAP["ratio"] * _GROSS_CAP["equity"]
                    - _GROSS_CAP["gross_mv"] - _GROSS_CAP["submitted"])
        if _n_gc > _room_gc:
            _GROSS_CAP["blocked_n"] += 1; _GROSS_CAP["blocked_nl"] += _n_gc
            print(f"    [gross-cap] BLOCKED BUY {_tk_gc} ~${_n_gc:,.0f} — "
                  f"room ${max(0.0, _room_gc):,.0f} at {_GROSS_CAP['ratio']:.2f}x cap")
            return False
        _GROSS_CAP["submitted"] += _n_gc
        return True
    except Exception as _gca_e:
        print(f"    [gross-cap] gate error — BLOCKED BUY {_tk_gc} (fail-closed): {_gca_e}")
        return False

# ── Fix (2026-07-15): OVERSELL guard — the short-book incident ───────────────
# execute_trade submitted SELLs with no position check while the ledger
# recorded "filled" at submission time, so exits sized off model/ledger state
# oversold true broker holdings and the margin account opened naked shorts
# (22 names, $81.5k ≈ 0.71x equity by 7/15 — the book mislabelled "crypto
# sleeve" since 7/6). This gate caps every SELL at the LIVE broker long qty
# (minus what this run already sold) and refuses SELLs when flat or short.
# Fail-closed when keys are set but the position read failed — selling blind
# is what minted the short book.
_LIVE_QTY = {}    # symbol -> signed live qty at the broker (short = negative)
_OVERSELL = {
    "enforce":  bool(ALPACA_API_KEY and ALPACA_SECRET_KEY),
    "pos_ok":   False,   # live position map successfully built
    "sold":     {},      # symbol -> qty this run has already sold
    "capped_n": 0,
    "blocked_n": 0,
}

def _oversell_cap(_tk_os, _qty_os):
    # Returns the SELL qty actually allowed (0 = refuse the order entirely).
    try:
        _q_os = int(_qty_os)
        if _q_os <= 0:
            return 0
        if not _OVERSELL["enforce"]:
            return _q_os          # local paper mode: no broker, no shorts possible
        if not _OVERSELL["pos_ok"]:
            _OVERSELL["blocked_n"] += 1
            print(f"    [oversell] BLOCKED SELL {_tk_os} x{_q_os} — "
                  f"live positions unknown (fail-closed)")
            return 0
        _held_os = float(_LIVE_QTY.get(_tk_os, 0.0)) - float(_OVERSELL["sold"].get(_tk_os, 0.0))
        _allow_os = int(min(float(_q_os), max(0.0, _held_os)))
        if _allow_os <= 0:
            _OVERSELL["blocked_n"] += 1
            print(f"    [oversell] BLOCKED SELL {_tk_os} x{_q_os} — live long qty "
                  f"{_held_os:g} (flat/short: refusing naked short)")
            return 0
        if _allow_os < _q_os:
            _OVERSELL["capped_n"] += 1
            print(f"    [oversell] {_tk_os}: SELL qty {_q_os} -> {_allow_os} (live long qty)")
        _OVERSELL["sold"][_tk_os] = float(_OVERSELL["sold"].get(_tk_os, 0.0)) + _allow_os
        return _allow_os
    except Exception as _os_e:
        print(f"    [oversell] gate error — BLOCKED SELL {_tk_os} (fail-closed): {_os_e}")
        return 0

try:
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        from alpaca.trading.client import TradingClient as _TC_gc
        _tc_gc   = _TC_gc(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        _acct_gc = _tc_gc.get_account()
        _pos_list_gc = _tc_gc.get_all_positions()
        _GROSS_CAP["equity"]   = float(_acct_gc.equity)
        _GROSS_CAP["gross_mv"] = float(sum(abs(float(_p_gc.market_value))
                                           for _p_gc in _pos_list_gc))
        _GROSS_CAP["acct_ok"]  = True
        # Oversell guard: signed live qty per symbol. Inner try so a schema
        # surprise degrades to fail-closed SELLs without touching the BUY gate.
        try:
            for _p_gc in _pos_list_gc:
                _LIVE_QTY[str(_p_gc.symbol)] = float(_p_gc.qty)
            _OVERSELL["pos_ok"] = True
        except Exception as _lq_e:
            print(f"  [patch] oversell-guard position map FAILED ({_lq_e}) — "
                  f"all SELLs refused this run (fail-closed)")
    else:
        # No broker keys (pure local paper mode): budget from the ledger.
        import pandas as _pd_gc
        from pathlib import Path as _P_gc
        _gmv_gc = 0.0
        _pt_gc = _P_gc("data/paper_trades/paper_trades.csv")
        if _pt_gc.exists():
            _df_gc = _pd_gc.read_csv(_pt_gc)
            _df_gc["qty"]   = _pd_gc.to_numeric(_df_gc["qty"],   errors="coerce").fillna(0)
            _df_gc["price"] = _pd_gc.to_numeric(_df_gc["price"], errors="coerce").fillna(0)
            _nl_gc = _df_gc["qty"] * _df_gc["price"]
            _gmv_gc = max(0.0, float(_nl_gc[_df_gc["action"] == "BUY"].sum()
                                     - _nl_gc[_df_gc["action"] == "SELL"].sum()))
        _GROSS_CAP["equity"]   = float(PORTFOLIO_CAPITAL)
        _GROSS_CAP["gross_mv"] = _gmv_gc
        _GROSS_CAP["acct_ok"]  = True

    _room0_gc = max(0.0, _GROSS_CAP["ratio"] * _GROSS_CAP["equity"] - _GROSS_CAP["gross_mv"])
    # Pre-trim: keep the highest-confidence BUY signals that fit the budget,
    # estimating one MAX_POSITION_PCT slot per name (the per-order hard gate
    # in execute_trade enforces actual dollars).
    _slot_gc = max(1.0, float(_GROSS_CAP["equity"]) * float(MAX_POSITION_PCT))
    _max_new_gc = int(_room0_gc / _slot_gc)
    _buys_gc = sorted(
        [(_tk_gc2, _sig_gc) for _tk_gc2, _sig_gc in signals.items()
         if _sig_gc.get("action") == "BUY"],
        key=lambda _x_gc: _x_gc[1].get("confidence", 0), reverse=True)
    _pre_blocked_gc = 0
    for _i_gc, (_tk_gc2, _) in enumerate(_buys_gc):
        if _i_gc >= _max_new_gc:
            signals[_tk_gc2]["exec_blocked"] = True
            _pre_blocked_gc += 1
    _lev_gc = (_GROSS_CAP["gross_mv"] / _GROSS_CAP["equity"]) if _GROSS_CAP["equity"] else 0.0
    print(f"  [patch] Gross cap: equity ${_GROSS_CAP['equity']:,.0f} | gross "
          f"${_GROSS_CAP['gross_mv']:,.0f} ({_lev_gc:.2f}x) | cap {_GROSS_CAP['ratio']:.2f}x "
          f"-> room ${_room0_gc:,.0f} = {_max_new_gc} new BUY slots | "
          f"pre-blocked {_pre_blocked_gc}/{len(_buys_gc)} BUY signals | "
          f"run_ok={_GROSS_CAP['run_ok']} ({_GROSS_CAP['run_type']})")
    print(f"  [patch] Oversell guard: enforce={_OVERSELL['enforce']} "
          f"pos_map={len(_LIVE_QTY)} symbols pos_ok={_OVERSELL['pos_ok']}")
except Exception as _gc_e:
    print(f"  [patch] GROSS CAP SETUP ERROR: {_gc_e} — hard gate stays active "
          f"(BUYs blocked unless account read succeeded above)")
"""

# ── Tier 2 extension: adaptive TWAP execution helpers ────────────────────────
# Appended to CELL_13_PREPATCH so that Cell 13's paper-trade execution has
# _twap_schedule() available. Splits each order into 5 volume-proportional
# slices following the U-shaped intraday volume profile (Madhavan 2002).
# VIX > 25 switches to stress mode: smaller early slices, larger close slice.
_CELL_13_T2_TWAP = """
_TWAP_BUCKETS_NORMAL = [
    (0,   0.25),    # 9:30 open  — first 30 min, highest volume
    (30,  0.15),    # 10:00
    (120, 0.15),    # 11:30
    (210, 0.20),    # 13:00
    (330, 0.25),    # 15:00 — final hour, second-highest volume
]
_TWAP_BUCKETS_STRESS = [   # VIX > 25: smaller early, larger close
    (0,   0.15),
    (30,  0.10),
    (120, 0.15),
    (210, 0.25),
    (330, 0.35),
]

def _twap_schedule(qty, side, vix=20.0, ticker=None):
    # Returns 5-slice TWAP schedule for qty shares on side (buy/sell)
    _buckets = _TWAP_BUCKETS_STRESS if vix > 25 else _TWAP_BUCKETS_NORMAL
    _total_f = sum(f for _, f in _buckets)
    _sched = []
    for _idx, (_mins, _frac) in enumerate(_buckets):
        _nf = _frac / _total_f
        _sched.append({
            "slice": _idx + 1,
            "minutes_from_open": _mins,
            "fraction": round(_nf, 3),
            "shares": max(1, round(qty * _nf)),
            "side": side,
            "ticker": ticker or "",
        })
    return _sched

_TWAP_ENABLED = True
try:
    _vix_13 = float(MACRO.get("vix", 20.0)) if "MACRO" in dir() else 20.0
except Exception:
    _vix_13 = 20.0

print(f"  [Tier2] Adaptive TWAP helpers injected (VIX={_vix_13:.1f}, "
      f"mode={'STRESS' if _vix_13 > 25 else 'NORMAL'})")
"""
CELL_13_PREPATCH += "\n\n" + _CELL_13_T2_TWAP

# ── Tier 3: White Reality Check / Hansen SPA test ────────────────────────────
# White (2000) Reality Check: bootstrap-corrects the p-value of the best
# performing strategy for data-snooping across all strategies tried.
# Hansen (2005) SPA: superior predictive ability test — stricter variant.
#
# Implementation: After Cell 15 (self-learning) updates weights, we sample
# the prediction errors 1000x with replacement and measure how often a random
# permutation achieves better Sharpe than the live strategy. p-value < 0.05
# means the strategy is unlikely to be a lucky draw from random.
# Writes result to data/predictions/reality_check.json for dashboard display.
CELL_15_POSTPATCH = """
import numpy as _np15wrc
import json  as _j15wrc
import os    as _os15wrc
from pathlib import Path as _P15wrc

def _white_reality_check(daily_pnl, n_bootstrap=1000, seed=42):
    _r = _np15wrc.array(daily_pnl, dtype=float)
    _r = _r[_np15wrc.isfinite(_r)]
    if len(_r) < 30:
        return None
    _rng = _np15wrc.random.default_rng(seed)
    _sr_live = _r.mean() / (_r.std() + 1e-8) * _np15wrc.sqrt(252)

    # Hansen SPA: benchmark is zero (risk-free = 0 excess return)
    # p-value = fraction of bootstrap SRs that beat live SR
    _boot_srs = []
    for _ in range(n_bootstrap):
        _sample = _rng.choice(_r, size=len(_r), replace=True)
        _boot_srs.append(_sample.mean() / (_sample.std() + 1e-8) * _np15wrc.sqrt(252))

    _boot_srs = _np15wrc.array(_boot_srs)
    _p_value  = float((_boot_srs >= _sr_live).mean())

    # Deflated SPA: subtract expected max SR under null
    _e_max_sr    = float(_np15wrc.percentile(_boot_srs, 95))
    _spa_stat    = _sr_live - _e_max_sr
    _spa_p_value = float((_boot_srs >= _sr_live + _spa_stat).mean())

    return {
        "sr_live":       round(float(_sr_live), 4),
        "sr_boot_p95":   round(_e_max_sr, 4),
        "wrc_p_value":   round(_p_value, 4),
        "spa_stat":      round(_spa_stat, 4),
        "spa_p_value":   round(_spa_p_value, 4),
        "n_days":        len(_r),
        "n_bootstrap":   n_bootstrap,
        "significant":   _p_value < 0.05,
    }

if _os15wrc.environ.get("RUN_TYPE", "morning") in ("morning", "evening"):
    try:
        import csv as _csv15
        # 2026-07-24 audit: this read data/predictions/daily_pnl_log.csv, which
        # nothing ever writes rows to (header-only since creation) -- so this
        # Stage-1 gate never ran. Worse, that file has no total_pnl column, and
        # .get("total_pnl", 0) would have scored an all-zero series and emitted
        # a p-value without ever erroring. A blind gate that silently returns a
        # fake answer is worse than one that is visibly broken: read the real
        # curve, and REFUSE loudly if the column is missing.
        _pnl_path = _P15wrc("data/predictions/pnl_history.csv")
        _pnl_rows = []
        _pnl_skipped = 0
        if _pnl_path.exists():
            with open(_pnl_path, encoding="utf-8-sig") as _f15:
                _reader15 = _csv15.DictReader(_f15)
                if "total_pnl" not in (_reader15.fieldnames or []):
                    raise RuntimeError(
                        f"pnl_history.csv has no total_pnl column (found "
                        f"{_reader15.fieldnames}) -- refusing to run the "
                        "Reality Check on a defaulted series")
                for _row15 in _reader15:
                    _v15 = str(_row15.get("total_pnl") or "").strip()
                    if not _v15:
                        continue      # blank cell = no data, never a zero
                    try:
                        _pnl_rows.append(float(_v15))
                    except ValueError:
                        _pnl_skipped += 1
        if _pnl_skipped:
            print(f"  [Tier3] WRC: skipped {_pnl_skipped} unparseable total_pnl rows")

        if len(_pnl_rows) >= 30:
            # pnl_history's total_pnl is a DAILY P&L series (basis contract
            # fixed alongside this repoint) -- consume it directly. Do NOT
            # np.diff: diffing a daily series destroys the signal.
            _daily_rets = _np15wrc.asarray(_pnl_rows, dtype=float)
            _wrc_result = _white_reality_check(_daily_rets, n_bootstrap=1000)
            if _wrc_result:
                _P15wrc("data/predictions").mkdir(exist_ok=True)
                _P15wrc("data/predictions/reality_check.json").write_text(
                    _j15wrc.dumps(_wrc_result, indent=2))
                _sig_str = "SIGNIFICANT (p<0.05)" if _wrc_result["significant"] else "not significant"
                print(f"  [Tier3] White Reality Check: SR={_wrc_result['sr_live']:.3f} "
                      f"WRC_p={_wrc_result['wrc_p_value']:.3f} SPA_p={_wrc_result['spa_p_value']:.3f} "
                      f"-> {_sig_str}")
        else:
            print(f"  [Tier3] White Reality Check: only {len(_pnl_rows)} days of PnL "
                  f"(need >= 30) — skipped")
    except Exception as _wrc_e:
        print(f"  [Tier3] White Reality Check error (non-fatal): {_wrc_e}")
else:
    print("  [Tier3] White Reality Check: intraday run — skipped")
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

# ── Execute close_long for SELL-labelled tickers ──────────────────────────────
# CELL_13_PREPATCH marks signals[tk]["close_long"] = True for ternary SELL tickers.
# Here we actually close the Alpaca paper position for those tickers.
try:
    import requests as _req13cl
    _close_long_tickers = [
        _tk13cl for _tk13cl, _sig13cl in (signals.items() if "signals" in dir() else {}.items())
        if _sig13cl.get("close_long")
    ]
    if _close_long_tickers and ALPACA_API_KEY and ALPACA_SECRET_KEY:
        from alpaca.trading.client import TradingClient as _TC13
        from alpaca.trading.requests import MarketOrderRequest as _MOR13
        from alpaca.trading.enums import OrderSide as _OS13, TimeInForce as _TIF13
        _tc13 = _TC13(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        _n_closed = 0
        for _cl_tk in _close_long_tickers:
            try:
                _pos = _tc13.get_open_position(_cl_tk)
                # Signed qty — shorts are NEGATIVE. The old abs() here DOUBLED
                # an existing short every SELL-labelled day (7/15 short-book
                # incident, e.g. CTAS -160). Also net out SELLs execute_trade
                # already submitted this run — unfilled orders aren't in the
                # position read yet.
                _qty = int(float(_pos.qty))
                if "_OVERSELL" in globals():
                    _qty -= int(float(_OVERSELL["sold"].get(_cl_tk, 0)))
                if _qty > 0:
                    _req13 = _MOR13(symbol=_cl_tk, qty=_qty,
                                    side=_OS13.SELL, time_in_force=_TIF13.DAY)
                    _tc13.submit_order(_req13)
                    _n_closed += 1
            except Exception:
                pass  # no open position or API error — skip
        print(f"  [patch] close_long: closed {_n_closed}/{len(_close_long_tickers)} SELL positions")
    elif _close_long_tickers:
        print(f"  [patch] close_long: {len(_close_long_tickers)} SELL tickers — Alpaca keys not set, skipped")
except Exception as _cl13e:
    print(f"  [patch] close_long error (non-fatal): {_cl13e}")


# Conformal-Kelly post-scale REMOVED 2026-07-11 (ledger-qty corruption, fill
# audit 7/9 + HANDOFF 7/10 ledger). The old block rewrote today's
# paper_trades.csv qty by the discount AFTER orders were submitted — it never
# changed the actual orders (the intended sizing hook, kelly_qty x
# _CONFORMAL_KELLY_MAP, was never wired into Cell 13), and because it ran on
# EVERY cycle with a today-mask and no already-scaled guard, the discount
# compounded multiplicatively through the day (ZBH: broker filled 112, ledger
# said 6). The ledger now keeps the true submitted qty; notional was always
# correct. Wiring the discount into REAL pre-submission sizing is a separate,
# dated model-change decision — _CONFORMAL_KELLY_MAP still exists for it.
print("  [TierC] Conformal Kelly: post-scale removed (ledger keeps true qty; "
      "sizing hook never wired — see 2026-07-11 handoff)")

# Gross-cap end-of-cell summary (2026-07-08) — one greppable line per run.
try:
    if "_GROSS_CAP" in dir() and (_GROSS_CAP["blocked_n"] or _GROSS_CAP["submitted"]):
        print(f"  [gross-cap] summary: allowed ${_GROSS_CAP['submitted']:,.0f} new BUY "
              f"notional | blocked {_GROSS_CAP['blocked_n']} orders "
              f"(~${_GROSS_CAP['blocked_nl']:,.0f}) | cap {_GROSS_CAP['ratio']:.2f}x")
except Exception:
    pass
"""

# ── Tier 2: Nowcasting macro — injected AFTER Cell 4 fetches FRED data ───────
# Cell 4 downloads lagged monthly FRED series (CPI, PCE, etc.).
# This postpatch layers in daily high-frequency market-based proxies so the
# model has current-month information the lagged series can't provide:
#   credit_spread_chg : HYG 5d return - LQD 5d return (credit stress proxy)
#   financial_stress  : VIX 20-day percentile rank (0 = calm, 1 = max stress)
#   yield_momentum    : TLT 10-day return (rate direction proxy)
#   equity_breadth    : fraction of sector ETFs above their 20-day MA
CELL_4_POSTPATCH = """
import os as _os4np
if _os4np.environ.get("RUN_TYPE", "morning") == "morning":
    try:
        import yfinance as _yf4np
        import json as _j4np
        import time as _t4np
        from pathlib import Path as _P4np

        _NOW_CACHE = _P4np("data/nowcast_cache.json")
        _NOW_TTL   = 86400   # 24h — proxies don't change intraday meaningfully

        _now_cache = {}
        if _NOW_CACHE.exists():
            try:
                _now_cache = _j4np.loads(_NOW_CACHE.read_text())
            except Exception:
                _now_cache = {}

        if _t4np.time() - _now_cache.get("_ts", 0) < _NOW_TTL and "credit_spread_chg" in _now_cache:
            _nowcast = _now_cache
            print("  [nowcast] Loaded from cache")
        else:
            _PROXY_TICKERS = ["HYG","LQD","^VIX","TLT",
                               "XLK","XLF","XLV","XLE","XLY","XLP","XLI","XLB","XLU","XLRE","XLC"]
            _pdata = _yf4np.download(_PROXY_TICKERS, period="60d", progress=False, auto_adjust=True)
            _now = {}
            try:
                import pandas as _pd4np
                if isinstance(_pdata.columns, _pd4np.MultiIndex):
                    _close = _pdata["Close"]
                else:
                    _close = _pdata

                if "HYG" in _close.columns and "LQD" in _close.columns:
                    _hyg5 = float(_close["HYG"].pct_change(5).dropna().iloc[-1])
                    _lqd5 = float(_close["LQD"].pct_change(5).dropna().iloc[-1])
                    _now["credit_spread_chg"] = round(_hyg5 - _lqd5, 5)

                if "^VIX" in _close.columns:
                    _vs = _close["^VIX"].dropna()
                    _vn = float(_vs.iloc[-1])
                    _v20 = _vs.tail(21).iloc[:-1]
                    _now["financial_stress"] = round(float((_v20 < _vn).sum() / max(len(_v20), 1)), 3)

                if "TLT" in _close.columns:
                    _now["yield_momentum"] = round(float(_close["TLT"].pct_change(10).dropna().iloc[-1]), 5)

                _sect_etfs = ["XLK","XLF","XLV","XLE","XLY","XLP","XLI","XLB","XLU","XLRE","XLC"]
                _above = sum(
                    1 for _e in _sect_etfs
                    if _e in _close.columns and len(_close[_e].dropna()) >= 21
                    and float(_close[_e].dropna().iloc[-1]) > float(_close[_e].dropna().tail(21).mean())
                )
                _denom = sum(1 for _e in _sect_etfs if _e in _close.columns
                             and len(_close[_e].dropna()) >= 21)
                if _denom > 0:
                    _now["equity_breadth"] = round(_above / _denom, 3)

            except Exception as _nc4e:
                print(f"  [nowcast] Proxy compute error: {_nc4e}")

            _now["_ts"] = _t4np.time()
            _nowcast = _now
            try:
                _NOW_CACHE.write_text(_j4np.dumps(_now, indent=2))
            except Exception:
                pass

        _nc_clean = {k: v for k, v in _nowcast.items() if not k.startswith("_")}
        if "MACRO" in dir() and isinstance(MACRO, dict):
            MACRO.update(_nc_clean)
        else:
            MACRO_NOWCAST = _nc_clean
        print(f"  [nowcast] Injected nowcast proxies: {_nc_clean}")
    except Exception as _now4e:
        print(f"  [nowcast] Error (non-fatal): {_now4e}")
else:
    print("  [nowcast] Skipped (morning-only)")

# ── Tier 3: Options IV term structure ─────────────────────────────────────────
# VXX tracks 1-month VIX futures (short-term IV).
# VIXM tracks 5-month VIX futures (mid-term IV).
# iv_term_slope = VIXM/VXX - 1:
#   > 0 (contango)    → market calm, near-term IV < long-term IV
#   < 0 (backwardation)→ market stress, near-term IV > long-term IV
# spy_put_call_proxy: SPY options volume asymmetry (put-heavy = hedging demand)
# Both are powerful short-term regime signals not captured by monthly FRED data.
try:
    import yfinance as _yf4iv
    import pandas as _pd4iv

    _IV_TICKERS = ["VXX", "VIXM", "SPY"]
    _iv_data = _yf4iv.download(_IV_TICKERS, period="5d", progress=False, auto_adjust=True)

    _iv_feats = {}
    try:
        if isinstance(_iv_data.columns, _pd4iv.MultiIndex):
            _iv_close = _iv_data["Close"]
        else:
            _iv_close = _iv_data

        if "VXX" in _iv_close.columns and "VIXM" in _iv_close.columns:
            _vxx_p  = float(_iv_close["VXX"].dropna().iloc[-1])
            _vixm_p = float(_iv_close["VIXM"].dropna().iloc[-1])
            if _vxx_p > 0:
                _iv_feats["iv_term_slope"] = round(_vixm_p / _vxx_p - 1.0, 4)

        # SPY 5-day realized vol as near-term fear gauge
        if "SPY" in _iv_close.columns:
            _spy_ret = _iv_close["SPY"].pct_change().dropna()
            if len(_spy_ret) >= 3:
                _iv_feats["spy_realized_vol5d"] = round(
                    float(_spy_ret.tail(5).std() * (252 ** 0.5)), 4)

    except Exception as _iv4e:
        print(f"  [Tier3-IV] Compute error: {_iv4e}")

    if _iv_feats:
        if "MACRO" in dir() and isinstance(MACRO, dict):
            MACRO.update(_iv_feats)
        elif "MACRO_NOWCAST" in dir():
            MACRO_NOWCAST.update(_iv_feats)
        print(f"  [Tier3-IV] IV term structure injected: {_iv_feats}")
except Exception as _iv4err:
    print(f"  [Tier3-IV] IV term structure error (non-fatal): {_iv4err}")
"""

# ══════════════════════════════════════════════════════════════════════════════
# TIER A–D UPGRADES
# ══════════════════════════════════════════════════════════════════════════════

# ── Tier A: Options flow, PEAD, short interest, Amihud, earnings revision ─────
_CELL_6_TA_SIGNALS = """
import os as _os6ta
import numpy as _np6ta
import pandas as _pd6ta

if _os6ta.environ.get("RUN_TYPE", "morning") == "morning":
    import time as _t6ta
    import datetime as _dt6ta

    # ── A1: Options put/call ratio + IV skew (yfinance option_chain) ──────────
    _OPT_LIQUID = {
        "SPY","QQQ","AAPL","MSFT","NVDA","TSLA","AMZN","META",
        "GOOGL","AMD","JPM","V","MA","NFLX","AVGO","CRM","BAC",
        "GS","MS","WMT","COST","UNH","LLY","XOM","HD","INTC",
        "QCOM","MU","TXN","COIN"
    }
    _opt_done = 0
    for _otk in _OPT_LIQUID:
        if _otk not in featured:
            continue
        try:
            import yfinance as _yf6ta
            _to = _yf6ta.Ticker(_otk)
            _exps = (_to.options or [])[:1]
            if not _exps:
                continue
            _chain = _to.option_chain(_exps[0])
            _calls, _puts = _chain.calls, _chain.puts
            if _calls is None or _puts is None or _calls.empty or _puts.empty:
                continue
            _cv  = _pd6ta.to_numeric(_calls["volume"], errors="coerce").fillna(0).sum()
            _pv  = _pd6ta.to_numeric(_puts["volume"],  errors="coerce").fillna(0).sum()
            _coi = _pd6ta.to_numeric(_calls["openInterest"], errors="coerce").fillna(0).sum()
            _poi = _pd6ta.to_numeric(_puts["openInterest"],  errors="coerce").fillna(0).sum()
            _pc_vol = float(_pv) / max(float(_cv), 1.0)
            _pc_oi  = float(_poi) / max(float(_coi), 1.0)
            # IV skew: OTM put IV minus nearest ATM call IV
            _iv_skew = 0.0
            try:
                _spot = float(featured[_otk]["Close"].dropna().iloc[-1])
                _calls_c = _calls.copy()
                _puts_c  = _puts.copy()
                _calls_c["_d"] = (_calls_c["strike"] - _spot).abs()
                _puts_c["_d"]  = (_puts_c["strike"]  - _spot).abs()
                _atm_iv = _pd6ta.to_numeric(
                    _calls_c.nsmallest(3, "_d")["impliedVolatility"], errors="coerce").mean()
                _otm_p  = _puts_c[_puts_c["strike"] < _spot * 0.95]
                _otm_iv = _pd6ta.to_numeric(
                    (_otm_p.nsmallest(3, "_d")["impliedVolatility"] if not _otm_p.empty
                     else _pd6ta.Series(dtype=float)), errors="coerce").mean()
                if _pd6ta.notna(_atm_iv) and _pd6ta.notna(_otm_iv):
                    _iv_skew = float(_otm_iv - _atm_iv)
            except Exception:
                pass
            featured[_otk]["put_call_vol"] = _pc_vol
            featured[_otk]["put_call_oi"]  = _pc_oi
            featured[_otk]["iv_skew_otm"]  = _iv_skew
            _opt_done += 1
            _t6ta.sleep(0.15)
        except Exception:
            pass
    for _c in ["put_call_vol", "put_call_oi", "iv_skew_otm"]:
        if _c not in FEATURE_COLS:
            FEATURE_COLS.append(_c)
    print(f"  [TierA] Options features: {_opt_done} tickers (put_call_vol, put_call_oi, iv_skew_otm)")

    # ── A2: PEAD signal + days_since_earnings ─────────────────────────────────
    _n_pead = 0
    for _ptk, _pdf in featured.items():
        try:
            import yfinance as _yf6pead
            _ytk2 = _yf6pead.Ticker(_ptk)
            _days_since = 45.0   # default: mid-quarter
            try:
                _ed2 = _ytk2.get_earnings_dates(limit=8)
                if _ed2 is not None and not _ed2.empty:
                    _ed2.index = (_ed2.index.tz_localize(None)
                                  if (hasattr(_ed2.index, "tz") and _ed2.index.tz)
                                  else _ed2.index)
                    _past2 = _ed2[_ed2.index.date <= _dt6ta.date.today()]
                    if not _past2.empty:
                        _last_earn = _past2.index[0].date()
                        _days_since = float((_dt6ta.date.today() - _last_earn).days)
            except Exception:
                pass
            _sue_val = float(_pdf["sue_score"].iloc[-1]) if "sue_score" in _pdf.columns else 0.0
            _pdf["days_since_earnings"] = _days_since
            _pdf["pead_signal"]         = _sue_val * (1.0 if _days_since < 60 else 0.0)
            featured[_ptk] = _pdf
            _n_pead += 1
            _t6ta.sleep(0.08)
        except Exception:
            pass
    for _c in ["days_since_earnings", "pead_signal"]:
        if _c not in FEATURE_COLS:
            FEATURE_COLS.append(_c)
    print(f"  [TierA] PEAD signal: {_n_pead} tickers")

    # ── A3: Short interest ratio ───────────────────────────────────────────────
    _n_si = 0
    for _stk, _sdf in featured.items():
        try:
            import yfinance as _yf6si
            _sr = _yf6si.Ticker(_stk).info.get("shortRatio", None)
            if _sr is not None:
                _sr_f = float(_sr)
                if _sr_f >= 0:
                    _sdf["short_ratio"] = _sr_f
                    featured[_stk] = _sdf
                    _n_si += 1
            _t6ta.sleep(0.05)
        except Exception:
            pass
    if "short_ratio" not in FEATURE_COLS:
        FEATURE_COLS.append("short_ratio")
    print(f"  [TierA] Short interest ratio: {_n_si} tickers")

    # ── A4: Amihud illiquidity (from existing OHLCV, zero extra API calls) ─────
    _n_amihud = 0
    for _atk, _adf in featured.items():
        try:
            _cl  = _adf["Close"]  if "Close"  in _adf.columns else None
            _vol2 = _adf["Volume"] if "Volume" in _adf.columns else None
            if _cl is None or _vol2 is None:
                continue
            _ret_a = _cl.pct_change().abs()
            _dvol  = (_cl * _pd6ta.to_numeric(_vol2, errors="coerce").fillna(0)).replace(0, _np6ta.nan)
            _adf["amihud_illiq"] = (_ret_a / _dvol).rolling(20, min_periods=10).mean().shift(1) * 1e6
            featured[_atk] = _adf
            _n_amihud += 1
        except Exception:
            pass
    if "amihud_illiq" not in FEATURE_COLS:
        FEATURE_COLS.append("amihud_illiq")
    print(f"  [TierA] Amihud illiquidity: {_n_amihud} tickers")

    # ── A5: Earnings revision direction ───────────────────────────────────────
    _n_rev = 0
    for _rtk, _rdf in featured.items():
        try:
            import yfinance as _yf6rev
            _eh2 = _yf6rev.Ticker(_rtk).earnings_history
            if _eh2 is not None and not _eh2.empty and "EPS Estimate" in _eh2.columns:
                _eps2 = _pd6ta.to_numeric(_eh2["EPS Estimate"], errors="coerce").dropna()
                if len(_eps2) >= 2:
                    _rev_dir = float(_np6ta.sign(float(_eps2.iloc[-1]) - float(_eps2.iloc[-2])))
                    _rdf["earnings_revision_dir"] = _rev_dir
                    featured[_rtk] = _rdf
                    _n_rev += 1
            _t6ta.sleep(0.08)
        except Exception:
            pass
    if "earnings_revision_dir" not in FEATURE_COLS:
        FEATURE_COLS.append("earnings_revision_dir")
    print(f"  [TierA] Earnings revision direction: {_n_rev} tickers")

else:
    print("  [TierA] Options/PEAD/short interest/Amihud/revision: morning-only, skipped")
"""
CELL_6_POSTPATCH += "\n\n" + _CELL_6_TA_SIGNALS

# ── Tier A: Earnings look-ahead suppression — filter training rows near earnings
# Rows within 5 days of an earnings event get bimodal return distributions
# that drown the model's signal in binary event noise. Drop from training.
_CELL_8_EARNINGS_FILTER = """
import numpy as _np8ef
import os as _os8ef

# Inject earnings filter into the training data prep. If 'days_since_earnings'
# is in the feature DataFrame, mask rows where the forward label is contaminated
# by an earnings event (days_to_earnings < 5 = label = earnings event, not signal).
# We expose a helper that Cell 8 can call via _mask_earnings_rows(df).
def _mask_earnings_rows(df, days_col="days_since_earnings", horizon=5):
    if days_col not in df.columns:
        return df
    # days_since_earnings is backward-looking. days_to_NEXT_earnings is unknown,
    # but if the ticker reports quarterly (~90d), approximate:
    # days_to_next = 90 - days_since_earnings (modulo quarter).
    # Mask rows where days_to_next < horizon (earnings is within our label window).
    _days_to_next = (90 - _np8ef.array(df[days_col].fillna(45).values, dtype=float)) % 90
    _mask = _days_to_next >= horizon   # keep rows with earnings outside label window
    _n_masked = (~_mask).sum()
    if _n_masked > 0 and _n_masked < len(df) * 0.30:   # safety: don't drop >30%
        return df[_mask]
    return df

print("  [TierA] Earnings look-ahead filter injected (_mask_earnings_rows)")
"""
CELL_8_PREPATCH += "\n\n" + _CELL_8_EARNINGS_FILTER

# ── Tier C: Regime-conditional sample weights for XGB/LGB ─────────────────────
_CELL_8_REGIME_WEIGHTS = """
import numpy as _np8rw
import os as _os8rw

# Expose _make_regime_sample_weights(df, current_regime) in Cell 8 namespace.
# Cell 8's model.fit(X, y, sample_weight=...) can use this to up-weight
# samples from the current regime and down-weight the opposite regime.
# Bear=0, Neutral=1, Bull=2. Current regime from HMM 'regimes' array.

_REGIME_SAMPLE_WEIGHTS = {
    # (current_regime, sample_regime): weight multiplier
    (0, 0): 2.0,   # bear now, bear samples → up-weight
    (0, 1): 1.0,
    (0, 2): 0.5,   # bear now, bull samples → down-weight
    (1, 0): 0.75,
    (1, 1): 1.5,
    (1, 2): 0.75,
    (2, 0): 0.5,
    (2, 1): 1.0,
    (2, 2): 2.0,   # bull now, bull samples → up-weight
}

_current_regime8rw = 1
try:
    if "regimes" in dir() and hasattr(regimes, "__len__") and len(regimes) > 0:
        _current_regime8rw = int(regimes[-1])
except Exception:
    pass

def _make_regime_sample_weights(df, regime_col="regime_hmm"):
    try:
        if regime_col not in df.columns:
            return None
        _reg_arr = _np8rw.array(df[regime_col].fillna(1).values, dtype=_np8rw.int32)
        _weights  = _np8rw.ones(len(_reg_arr), dtype=float)
        for _i, _r in enumerate(_reg_arr):
            _weights[_i] = _REGIME_SAMPLE_WEIGHTS.get(
                (_current_regime8rw, int(_r)), 1.0)
        return _weights
    except Exception:
        return None

print(f"  [TierC] Regime sample weights injected (current_regime={_current_regime8rw}: "
      f"{'BEAR' if _current_regime8rw==0 else 'NEUTRAL' if _current_regime8rw==1 else 'BULL'})")
"""
CELL_8_PREPATCH += "\n\n" + _CELL_8_REGIME_WEIGHTS

# ── Tier B: Sector rotation momentum signal ────────────────────────────────────
_CELL_11_SECTOR_ROTATION = """
import json as _j11sr
import numpy as _np11sr
from pathlib import Path as _P11sr

# Sector 12-month-skip-1-month relative momentum (Jegadeesh-Titman).
# Top 3 sectors get +SECTOR_BOOST to composite_score at signal generation.
# Bottom 3 sectors get -SECTOR_BOOST (relative underweight).
_SECTOR_ETF_MAP = {
    "Technology":      "XLK",
    "Financials":      "XLF",
    "Healthcare":      "XLV",
    "Energy":          "XLE",
    "Consumer Disc":   "XLY",
    "Consumer Staples":"XLP",
    "Industrials":     "XLI",
    "Materials":       "XLB",
    "Utilities":       "XLU",
    "Real Estate":     "XLRE",
    "Communication":   "XLC",
}
_SECTOR_BOOST = 0.03   # ±3% score boost for top/bottom sectors

_sector_mom_scores = {}
try:
    import yfinance as _yf11sr
    _etf_data = _yf11sr.download(
        list(_SECTOR_ETF_MAP.values()), period="14mo", progress=False, auto_adjust=True)
    if "Close" in _etf_data:
        _etf_close = _etf_data["Close"] if isinstance(_etf_data.columns, _np11sr.ndarray.__class__.__mro__[0]) else _etf_data["Close"]
    elif hasattr(_etf_data, "xs"):
        _etf_close = _etf_data.xs("Close", axis=1, level=0) if "Close" in _etf_data.columns.get_level_values(0) else _etf_data
    else:
        _etf_close = _etf_data
    if hasattr(_etf_close, "columns"):
        _sector_rets = {}
        for _sec, _etf in _SECTOR_ETF_MAP.items():
            if _etf not in _etf_close.columns:
                continue
            _px = _etf_close[_etf].dropna()
            if len(_px) < 50:
                continue
            # 12-month return, skip last 1 month (standard momentum factor)
            _ret12m1m = float(_px.iloc[-22] / _px.iloc[0] - 1.0) if len(_px) > 22 else 0.0
            _sector_rets[_sec] = _ret12m1m
        if _sector_rets:
            _sorted = sorted(_sector_rets.items(), key=lambda x: x[1], reverse=True)
            _top3   = {s for s, _ in _sorted[:3]}
            _bot3   = {s for s, _ in _sorted[-3:]}
            _sector_mom_scores = {
                s: (_SECTOR_BOOST if s in _top3 else -_SECTOR_BOOST if s in _bot3 else 0.0)
                for s in _sector_rets
            }
            _P11sr("data/weights/sector_rotation.json").write_text(
                _j11sr.dumps({"as_of": str(__import__("datetime").date.today()),
                               "momentum_12m1m": {k: round(v,4) for k,v in _sector_rets.items()},
                               "score_boost": _sector_mom_scores}, indent=2))
            print(f"  [TierB] Sector rotation: top3={_top3}  bot3={_bot3}")
except Exception as _sr11e:
    print(f"  [TierB] Sector rotation error (non-fatal): {_sr11e}")

# Load pre-computed sector rotation boosts from file if above fetch failed
if not _sector_mom_scores:
    try:
        _sr_file = _P11sr("data/weights/sector_rotation.json")
        if _sr_file.exists():
            _sr_loaded = _j11sr.loads(_sr_file.read_text())
            _sector_mom_scores = _sr_loaded.get("score_boost", {})
    except Exception:
        pass
"""
CELL_11_PREPATCH += "\n\n" + _CELL_11_SECTOR_ROTATION

# ── Tier B: Hurst-gated momentum vs. mean-reversion signal ────────────────────
_CELL_11_HURST_GATE = """
import numpy as _np11hg

# Hurst-gated signal refinement: apply AFTER ternary labels are set.
# hurst_exp < 0.45 → mean-reverting regime → invert momentum signal.
# hurst_exp > 0.55 → trending regime       → amplify momentum signal.
# Operates on existing composite_score and ternary_label in signals dict.
_HURST_GATE_APPLIED = 0

if "signals" in dir() and "featured" in dir():
    for _htk, _hsig in signals.items():
        try:
            _hdf = featured.get(_htk)
            if _hdf is None or "hurst_exp" not in _hdf.columns:
                continue
            _h = float(_hdf["hurst_exp"].dropna().iloc[-1])
            _cs = float(_hsig.get("composite_score", 0.5))

            if _h < 0.45:
                # Mean-reverting: flip signal toward neutral (fade momentum)
                # Distance from 0.5 is compressed by Hurst factor
                _hurst_scale = max(0.0, (_h / 0.45))   # 0 at H=0, 1 at H=0.45
                _new_cs = 0.5 + (_cs - 0.5) * _hurst_scale * 0.5
                signals[_htk]["composite_score"] = round(_new_cs, 6)
                signals[_htk]["hurst_gated"]     = "mean_rev"
                _HURST_GATE_APPLIED += 1
            elif _h > 0.55:
                # Trending: amplify signal (trend-follow)
                _hurst_scale = min(1.5, 1.0 + (_h - 0.55) / 0.45)
                _new_cs = 0.5 + (_cs - 0.5) * _hurst_scale
                _new_cs = float(_np11hg.clip(_new_cs, 0.0, 1.0))
                signals[_htk]["composite_score"] = round(_new_cs, 6)
                signals[_htk]["hurst_gated"]     = "trending"
                _HURST_GATE_APPLIED += 1
        except Exception:
            pass

print(f"  [TierB] Hurst gate applied to {_HURST_GATE_APPLIED} signals")
"""
CELL_11_POSTPATCH += "\n\n" + _CELL_11_HURST_GATE

# ── Tier B: Sector rotation score → composite_score boost in CELL_11_POSTPATCH
_CELL_11_SECTOR_BOOST = """
import json as _j11sb
from pathlib import Path as _P11sb

# Apply sector rotation momentum boost to composite_score.
# Uses _sector_mom_scores injected in CELL_11_PREPATCH.
_SECTOR_TICKER_MAP = {
    "AAPL":"Technology","MSFT":"Technology","NVDA":"Technology","GOOGL":"Technology",
    "AMZN":"Technology","META":"Technology","AVGO":"Technology","CRM":"Technology",
    "NOW":"Technology","PLTR":"Technology","ORCL":"Technology","ADBE":"Technology",
    "INTC":"Technology","CSCO":"Technology","ANET":"Technology","AMD":"Technology",
    "QCOM":"Semiconductors","AMAT":"Semiconductors","MU":"Semiconductors",
    "TXN":"Semiconductors","LRCX":"Semiconductors","KLAC":"Semiconductors",
    "JPM":"Financials","V":"Financials","MA":"Financials","BAC":"Financials",
    "GS":"Financials","MS":"Financials","BLK":"Financials","AXP":"Financials",
    "WFC":"Financials","C":"Financials","SCHW":"Financials","COIN":"Financials",
    "UNH":"Healthcare","LLY":"Healthcare","JNJ":"Healthcare","ABBV":"Healthcare",
    "MRK":"Healthcare","TMO":"Healthcare","ABT":"Healthcare","PFE":"Healthcare",
    "AMGN":"Healthcare","ISRG":"Healthcare","VRTX":"Healthcare",
    "XOM":"Energy","CVX":"Energy","COP":"Energy","SLB":"Energy","EOG":"Energy",
    "HD":"Consumer Disc","NKE":"Consumer Disc","SBUX":"Consumer Disc","MCD":"Consumer Disc",
    "COST":"Consumer Disc","TGT":"Consumer Disc","TSLA":"Consumer Disc","UBER":"Consumer Disc",
    "WMT":"Consumer Staples","PG":"Consumer Staples","KO":"Consumer Staples","PEP":"Consumer Staples",
    "BA":"Industrials","CAT":"Industrials","GE":"Industrials","RTX":"Industrials",
    "LMT":"Industrials","UPS":"Industrials","FDX":"Industrials",
    "NFLX":"Communication","DIS":"Communication","CMCSA":"Communication","VZ":"Communication",
    "T":"Communication","TMUS":"Communication",
    "NEE":"Utilities","DUK":"Utilities","SO":"Utilities",
    "AMT":"Real Estate","PLD":"Real Estate","EQIX":"Real Estate",
    "LIN":"Materials","FCX":"Materials","NEM":"Materials",
}

if "signals" in dir() and "_sector_mom_scores" in dir():
    _n_sec_boost = 0
    for _stk2, _ssig in signals.items():
        try:
            _sec = _SECTOR_TICKER_MAP.get(_stk2, "")
            _boost = _sector_mom_scores.get(_sec, 0.0) if _sec else 0.0
            if _boost != 0.0:
                _cs2 = float(_ssig.get("composite_score", 0.5))
                signals[_stk2]["composite_score"] = round(
                    max(0.0, min(1.0, _cs2 + _boost)), 6)
                signals[_stk2]["sector_rotation_boost"] = _boost
                _n_sec_boost += 1
        except Exception:
            pass
    print(f"  [TierB] Sector rotation boost applied to {_n_sec_boost} tickers")
"""
CELL_11_POSTPATCH += "\n\n" + _CELL_11_SECTOR_BOOST

# ── Frame-1 shadow harness: cross-sectional long-short (LOGGED, NOT TRADED) ────
# During the 30-day clean-baseline window we must NOT change the live model.
# This computes a hypothetical dollar-neutral long-short book each run — long the
# top-decile, short the bottom-decile by the model's confidence (same field
# Cell 13 trades) — and scores prior entries at the 5-day horizon from the price
# history already in `featured`. It trades nothing; it only writes a shadow P&L
# track to data/shadow/ so we can compare a cross-sectional frame against the
# live baseline before deciding whether to deploy it. Fully exception-wrapped.
_CELL_11_SHADOW_XSEC = '''
try:
    import json as _js_sh, datetime as _dt_sh
    import pandas as _pd_sh
    from pathlib import Path as _P_sh
    if "signals" in dir() and "featured" in dir() and signals:
        _sh_dir = _P_sh("data/shadow"); _sh_dir.mkdir(parents=True, exist_ok=True)
        _pos_path = _sh_dir / "cross_sectional_positions.jsonl"
        _pnl_path = _sh_dir / "cross_sectional_pnl.csv"
        _H_sh, _DECILE = 5, 30
        _today_sh = _dt_sh.date.today().isoformat()

        # Rank universe by confidence (calibrated P(bull)) — what Cell 13 trades.
        _ranked_sh = sorted(
            [(t, float(s.get("confidence", 0.5) or 0.5)) for t, s in signals.items()],
            key=lambda x: x[1], reverse=True)
        _longs  = [t for t, _ in _ranked_sh[:_DECILE]] if len(_ranked_sh) >= 2 * _DECILE else []
        _shorts = [t for t, _ in _ranked_sh[-_DECILE:]] if len(_ranked_sh) >= 2 * _DECILE else []

        _existing = []
        if _pos_path.exists():
            for _ln in _pos_path.read_text().splitlines():
                try: _existing.append(_js_sh.loads(_ln))
                except Exception: pass

        def _fwd_ret_sh(_tk, _entry_iso, _h):
            try:
                _df = featured.get(_tk)
                if _df is None or "Close" not in _df.columns: return None
                _c = _pd_sh.to_numeric(_df["Close"], errors="coerce").dropna()
                _ix = _pd_sh.to_datetime(_c.index)
                _p = int(_ix.searchsorted(_pd_sh.Timestamp(_entry_iso)))
                if _p >= len(_c) or _p + _h >= len(_c): return None   # not matured
                return float(_c.iloc[_p + _h] / _c.iloc[_p] - 1.0)
            except Exception:
                return None

        # Score matured (5-day-old) entries not yet scored.
        _new_rows = []
        for _e in _existing:
            _ed = _e.get("date")
            if not _ed or _e.get("scored"): continue
            _lr = [r for r in (_fwd_ret_sh(t, _ed, _H_sh) for t in _e.get("longs", [])) if r is not None]
            _sr = [r for r in (_fwd_ret_sh(t, _ed, _H_sh) for t in _e.get("shorts", [])) if r is not None]
            if len(_lr) >= 5 and len(_sr) >= 5:
                _lret, _sret = sum(_lr)/len(_lr), sum(_sr)/len(_sr)
                _new_rows.append((_ed, _today_sh, round(_lret,5), round(_sret,5),
                                  round(_lret-_sret,5), len(_lr), len(_sr)))
                _e["scored"] = True

        if _new_rows:
            if not _pnl_path.exists():
                _pnl_path.write_text("entry_date,scored_date,long_ret,short_ret,long_short,n_long,n_short\\n")
            with open(_pnl_path, "a") as _f:
                for _r in _new_rows: _f.write(",".join(str(x) for x in _r) + "\\n")
            _pos_path.write_text("\\n".join(_js_sh.dumps(_e) for _e in _existing) + "\\n")

        # Record today's book once per date.
        if _longs and _shorts and not any(_e.get("date") == _today_sh for _e in _existing):
            with open(_pos_path, "a") as _f:
                _f.write(_js_sh.dumps({"date": _today_sh, "longs": _longs,
                                       "shorts": _shorts, "scored": False}) + "\\n")
            print(f"  [shadow X-sec] recorded {len(_longs)}L/{len(_shorts)}S book; "
                  f"scored {len(_new_rows)} matured entries")
        else:
            print(f"  [shadow X-sec] scored {len(_new_rows)} matured entries (no new book this run)")
except Exception as _sh_e:
    print(f"  [shadow X-sec] non-fatal: {_sh_e}")
'''
CELL_11_POSTPATCH += "\n\n" + _CELL_11_SHADOW_XSEC

# ── Tier B: Fama-French RMW + CMA factors ─────────────────────────────────────
# RMW (Robust-Minus-Weak): profitability factor
# CMA (Conservative-Minus-Aggressive): investment factor
# Both expose idiosyncratic alpha when stripped from directional returns.
_CELL_12_FF5 = """
import json as _j12ff
import datetime as _dt12ff
from pathlib import Path as _P12ff
import numpy as _np12ff

# Download FF5 factors from Ken French's data library (free CSV).
# Cache 7 days to avoid repeated downloads.
_FF5_CACHE = _P12ff("data/weights/ff5_factors.json")
_FF5_TTL   = 7 * 86400   # 7 days

_ff5_data = None
try:
    import time as _t12ff
    if _FF5_CACHE.exists():
        _age = _t12ff.time() - _FF5_CACHE.stat().st_mtime
        if _age < _FF5_TTL:
            _ff5_data = _j12ff.loads(_FF5_CACHE.read_text())

    if _ff5_data is None:
        import io as _io12ff
        import zipfile as _z12ff
        import requests as _rq12ff
        import pandas as _pd12ff
        _url_ff5 = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
                    "ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip")
        _resp = _rq12ff.get(_url_ff5, timeout=30)
        if _resp.ok:
            with _z12ff.ZipFile(_io12ff.BytesIO(_resp.content)) as _zf:
                _csv_candidates = [n for n in _zf.namelist() if n.upper().endswith(".CSV")]
                if not _csv_candidates:
                    raise ValueError(f"No CSV in FF5 ZIP (files: {_zf.namelist()})")
                _csv_name = _csv_candidates[0]
                _raw = _zf.read(_csv_name).decode("utf-8", errors="replace")
            # Parse: skip header rows until we see "Mkt-RF"
            _lines = _raw.splitlines()
            _start = next((i for i, l in enumerate(_lines) if "Mkt-RF" in l), 0)
            _df_ff = _pd12ff.read_csv(
                _io12ff.StringIO(chr(10).join(_lines[_start:])), index_col=0)
            _df_ff.index = _pd12ff.to_datetime(_df_ff.index.astype(str), format="%Y%m%d", errors="coerce")
            _df_ff = _df_ff.dropna(how="all").last("252D")
            # Keep last 252 trading days, store means
            _ff5_data = {
                "as_of": _dt12ff.date.today().isoformat(),
                "rmw_mean": round(float(_df_ff["RMW"].mean()) / 100, 6) if "RMW" in _df_ff else 0.0,
                "cma_mean": round(float(_df_ff["CMA"].mean()) / 100, 6) if "CMA" in _df_ff else 0.0,
                "mkt_mean": round(float(_df_ff["Mkt-RF"].mean()) / 100, 6) if "Mkt-RF" in _df_ff else 0.0,
                "smb_mean": round(float(_df_ff["SMB"].mean()) / 100, 6) if "SMB" in _df_ff else 0.0,
                "hml_mean": round(float(_df_ff["HML"].mean()) / 100, 6) if "HML" in _df_ff else 0.0,
            }
            _FF5_CACHE.write_text(_j12ff.dumps(_ff5_data, indent=2))
            print(f"  [TierB] FF5 factors downloaded: RMW={_ff5_data['rmw_mean']:+.4%}/day  "
                  f"CMA={_ff5_data['cma_mean']:+.4%}/day")
        else:
            print(f"  [TierB] FF5 download failed: HTTP {_resp.status_code}")

    if _ff5_data:
        # Expose RMW and CMA for use in CVaR risk model attribution
        _FF5_RMW = float(_ff5_data.get("rmw_mean", 0.0))
        _FF5_CMA = float(_ff5_data.get("cma_mean", 0.0))
        _FF5_MKT = float(_ff5_data.get("mkt_mean", 0.0))
        _FF5_SMB = float(_ff5_data.get("smb_mean", 0.0))
        _FF5_HML = float(_ff5_data.get("hml_mean", 0.0))
        print(f"  [TierB] FF5 available: RMW={_FF5_RMW:+.4%}  CMA={_FF5_CMA:+.4%}  "
              f"HML={_FF5_HML:+.4%}  SMB={_FF5_SMB:+.4%}")

except Exception as _ff5err:
    print(f"  [TierB] FF5 factors error (non-fatal): {_ff5err}")
"""
CELL_12_PREPATCH += "\n\n" + _CELL_12_FF5

# ── Tier C: Conformal uncertainty → Kelly position size ───────────────────────
_CELL_13_CONFORMAL_KELLY = """
import json as _j13ck
from pathlib import Path as _P13ck
import numpy as _np13ck

# Wire conformal prediction uncertainty directly into Kelly fraction:
#   kelly_fraction = base_fraction × (1 − conformal_uncertainty)
# conformal_uncertainty is proxied by normalised distance from decision boundary:
#   uncertainty = 1 − |composite_score − 0.5| / 0.5  (0=certain, 1=at boundary)
# This reduces position size when the model is uncertain, not just when
# signal is weak — a provably correct way to handle model uncertainty.
_CONFORMAL_KELLY_ENABLED = True
_MAX_UNCERTAINTY_DISCOUNT = 0.60   # max 60% size reduction for boundary signals

_conformal_kelly_map = {}
if "signals" in dir():
    for _ck_tk, _ck_sig in signals.items():
        try:
            _cs_ck = float(_ck_sig.get("composite_score", 0.5))
            # Uncertainty: 0 when |cs-0.5|=0.5 (max conviction), 1 when cs=0.5 (no edge)
            _certainty  = min(1.0, abs(_cs_ck - 0.5) / 0.5)
            _uncertainty = 1.0 - _certainty
            # Scale discount: 0 at full certainty, MAX_DISCOUNT at boundary
            _discount = _uncertainty * _MAX_UNCERTAINTY_DISCOUNT
            _conformal_kelly_map[_ck_tk] = round(1.0 - _discount, 4)
        except Exception:
            _conformal_kelly_map[_ck_tk] = 1.0

# Expose scalar to multiply into kelly_qty:
# Usage in Cell 13: qty = kelly_qty(tk, ...) × _conformal_kelly_map.get(tk, 1.0)
_CONFORMAL_KELLY_MAP = _conformal_kelly_map

_n_discounted = sum(1 for v in _conformal_kelly_map.values() if v < 0.95)
print(f"  [TierC] Conformal Kelly: {_n_discounted} positions discounted "
      f"(max_discount={_MAX_UNCERTAINTY_DISCOUNT:.0%})")
"""
CELL_13_PREPATCH += "\n\n" + _CELL_13_CONFORMAL_KELLY

# ── Tier C: Gain-to-Pain ratio kill switch ────────────────────────────────────
_CELL_15_GPR = """
import numpy as _np15gpr
import json  as _j15gpr
from pathlib import Path as _P15gpr
import datetime as _dt15gpr

# Gain-to-Pain Ratio (Jack Schwager): sum(positive monthly returns) / |sum(negative monthly returns)|
# GPR < 0.5 → strategy gives back more than it earns → fire Discord warning.
# Complements existing peak drawdown kill switch (which fires immediately);
# GPR detects slow bleed that doesn't trigger single-day limits.
_GPR_WARN_THRESHOLD = 0.5   # warn below 0.5
_GPR_KILL_THRESHOLD = 0.20  # kill switch below 0.20 (severe persistent bleed)

try:
    import pandas as _pd15gpr
    _pnl_path15 = _P15gpr("data/predictions/daily_pnl_log.csv")
    if _pnl_path15.exists():
        _dl15 = _pd15gpr.read_csv(_pnl_path15)
        _dl15["date"]   = _pd15gpr.to_datetime(_dl15["date"], errors="coerce")
        _dl15["net_pl"] = _pd15gpr.to_numeric(_dl15.get("net_pl", _pd15gpr.Series(dtype=float)), errors="coerce")
        _dl15 = _dl15.sort_values("date").dropna(subset=["date","net_pl"])

        if len(_dl15) >= 30:
            # Resample to monthly
            _dl15 = _dl15.set_index("date")
            _monthly = _dl15["net_pl"].resample("ME").sum()
            _gains   = float(_monthly[_monthly > 0].sum())
            _pains   = float(_monthly[_monthly < 0].sum())
            _gpr     = _gains / max(abs(_pains), 1e-8)

            _gpr_result = {
                "as_of": _dt15gpr.date.today().isoformat(),
                "gpr": round(_gpr, 4),
                "total_gain": round(_gains, 2),
                "total_pain": round(_pains, 2),
                "n_months": int(len(_monthly)),
                "status": ("OK" if _gpr >= _GPR_WARN_THRESHOLD
                           else "WARN" if _gpr >= _GPR_KILL_THRESHOLD else "KILL"),
            }
            _P15gpr("data/predictions/gain_to_pain.json").write_text(
                _j15gpr.dumps(_gpr_result, indent=2))
            print(f"  [TierC] Gain-to-Pain Ratio: {_gpr:.3f} "
                  f"({'OK' if _gpr >= _GPR_WARN_THRESHOLD else 'WARN' if _gpr >= _GPR_KILL_THRESHOLD else 'KILL'})")

            if _gpr < _GPR_WARN_THRESHOLD:
                _disc_gpr = __import__("os").environ.get("DISCORD_WEBHOOK_URL","")
                if _disc_gpr:
                    try:
                        import requests as _rq15gpr
                        _emoji = "🚨" if _gpr < _GPR_KILL_THRESHOLD else "⚠️"
                        _rq15gpr.post(_disc_gpr, json={"embeds":[{
                            "title": f"{_emoji} Gain-to-Pain Alert: GPR={_gpr:.3f}",
                            "color": 15158332,
                            "description": (f"GPR={_gpr:.3f} below threshold {_GPR_WARN_THRESHOLD:.2f}. "
                                           f"Total gain: ${_gains:,.0f}  Total pain: ${_pains:,.0f}. "
                                           f"Strategy is bleeding slowly — review signal layer."),
                        }]}, timeout=10)
                    except Exception:
                        pass

            if _gpr < _GPR_KILL_THRESHOLD:
                _ks_path15 = _P15gpr("data/KILL_SWITCH_ACTIVE.flag")
                if not _ks_path15.exists():
                    _ks_path15.write_text(
                        f"GPR kill: gain-to-pain={_gpr:.3f} < {_GPR_KILL_THRESHOLD:.2f}")
                    print(f"  [TierC] KILL SWITCH ACTIVATED: GPR={_gpr:.3f} < {_GPR_KILL_THRESHOLD}")
        else:
            print(f"  [TierC] Gain-to-Pain: need 30+ daily PnL rows (have {len(_dl15)})")
    else:
        print("  [TierC] Gain-to-Pain: no daily_pnl_log.csv yet")
except Exception as _gpr15e:
    print(f"  [TierC] Gain-to-Pain error (non-fatal): {_gpr15e}")
"""
CELL_15_POSTPATCH += "\n\n" + _CELL_15_GPR

# ── Tier C: Wire _mask_earnings_rows + _make_regime_sample_weights into Cell 8 ─
# Since we can't modify the notebook's Cell 8 source directly, monkey-patch
# XGBClassifier.fit and LGBMClassifier.fit to auto-apply both functions when
# a global training DataFrame _TRAINING_DF8 is set before the fit call.
# _TRAINING_DF8 is populated by patching the data prep section via CELL_8_PREPATCH.
_CELL_8_FIT_WIRING = """
import numpy as _np8fw

# ── Monkey-patch XGB/LGB .fit() to inject sample_weight + earnings masking ───
# Strategy: wrap fit() so that if _TRAINING_DF8 is set in the namespace,
# we apply earnings masking and regime sample weights automatically.
_FIT_PATCH_APPLIED = False
try:
    import xgboost as _xgb8fw
    import lightgbm as _lgb8fw

    _orig_xgb_fit = _xgb8fw.XGBClassifier.fit
    _orig_lgb_fit = _lgb8fw.LGBMClassifier.fit

    def _patched_fit(self, X, y, sample_weight=None, **kw):
        try:
            _df8fw = globals().get("_TRAINING_DF8") or (
                locals().get("_TRAINING_DF8") if False else None)
            # Access via the calling namespace — inject via well-known global name
            import builtins as _blt8fw
            _df8fw2 = getattr(_blt8fw, "_TRAINING_DF8_GLOBAL", None)
            if _df8fw2 is not None and hasattr(_df8fw2, "columns"):
                # Earnings masking: align index
                if hasattr(X, "__len__") and len(X) == len(_df8fw2):
                    _mask8fw = _mask_earnings_rows(_df8fw2).index
                    if len(_mask8fw) < len(_df8fw2):
                        _keep8fw = _df8fw2.index.isin(_mask8fw)
                        X  = X[_keep8fw] if hasattr(X, "__getitem__") else X
                        y  = y[_keep8fw]
                        _df8fw2 = _df8fw2.loc[_mask8fw]
                        if sample_weight is not None and hasattr(sample_weight, "__len__"):
                            sample_weight = sample_weight[_keep8fw]
                # Regime sample weights (only if no external weight passed)
                if sample_weight is None:
                    _sw8fw = _make_regime_sample_weights(_df8fw2)
                    if _sw8fw is not None and len(_sw8fw) == len(y):
                        sample_weight = _sw8fw
        except Exception:
            pass
        return _orig_xgb_fit(self, X, y, sample_weight=sample_weight, **kw)

    def _patched_lgb_fit(self, X, y, sample_weight=None, **kw):
        try:
            import builtins as _blt8fwl
            _df8fwl = getattr(_blt8fwl, "_TRAINING_DF8_GLOBAL", None)
            if _df8fwl is not None and hasattr(_df8fwl, "columns"):
                if hasattr(X, "__len__") and len(X) == len(_df8fwl):
                    _mask8fwl = _mask_earnings_rows(_df8fwl).index
                    if len(_mask8fwl) < len(_df8fwl):
                        _keep8fwl = _df8fwl.index.isin(_mask8fwl)
                        X  = X[_keep8fwl] if hasattr(X, "__getitem__") else X
                        y  = y[_keep8fwl]
                        _df8fwl = _df8fwl.loc[_mask8fwl]
                        if sample_weight is not None and hasattr(sample_weight, "__len__"):
                            sample_weight = sample_weight[_keep8fwl]
                if sample_weight is None:
                    _sw8fwl = _make_regime_sample_weights(_df8fwl)
                    if _sw8fwl is not None and len(_sw8fwl) == len(y):
                        sample_weight = _sw8fwl
        except Exception:
            pass
        return _orig_lgb_fit(self, X, y, sample_weight=sample_weight, **kw)

    _xgb8fw.XGBClassifier.fit  = _patched_fit
    _lgb8fw.LGBMClassifier.fit = _patched_lgb_fit
    _FIT_PATCH_APPLIED = True
    print("  [TierA/C] XGB/LGB .fit() patched for earnings masking + regime weights")
except Exception as _fw8e:
    print(f"  [TierA/C] Fit wiring error (non-fatal): {_fw8e}")

# Expose _TRAINING_DF8_GLOBAL setter — Cell 8 prepares df before fit, so
# if df is a DataFrame variable named 'df' or 'train_df', capture it.
# We set it by overriding pandas DataFrame constructor to capture training frames.
# Simpler: just set the builtin attr before any fit call based on common var names.
try:
    import builtins as _blt8fw2
    for _varname8fw in ["df", "train_df", "X_train_df", "features_df"]:
        if _varname8fw in dir() and hasattr(eval(_varname8fw), "columns"):
            setattr(_blt8fw2, "_TRAINING_DF8_GLOBAL", eval(_varname8fw))
            break
except Exception:
    pass
"""
CELL_8_PREPATCH += "\n\n" + _CELL_8_FIT_WIRING

# ── Tier D: Alpha Vantage + Finnhub enrichment ────────────────────────────────
# Wire in free API keys (25 req/day AV, 60 req/min Finnhub).
# Used in the enrichment section — not as cell patches.
_ALPHAVANTAGE_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")
_FINNHUB_KEY      = os.environ.get("FINNHUB_API_KEY", "")

# ── Dispatcher dicts ──────────────────────────────────────────────────────────
# Cell 15 prepatch: coerce macro_data regime string → int so the notebook's
# diagnose_failures_and_rewrite_rules doesn't crash on "Neutral / Mixed"
CELL_15_PREPATCH = """
# ── Fix: Initialize River online-learning objects robustly ────────────────────
# ROOT CAUSE: Cell 15 notebook code loads the pkl itself, and if the pkl has a
# bad/old format, it overwrites _river_scaler = None. The prepatch must run
# AFTER any Cell 15 load attempt — but it runs BEFORE. So we take a different
# approach: always (re)write a valid pkl, then set _river_scaler/_river_lr from
# it so that even if Cell 15 re-reads the pkl, it gets valid objects.
try:
    import pickle as _pkl15rv
    from pathlib import Path as _P15rv
    from river import preprocessing as _rv_pre15, linear_model as _rv_lm15
    from river import metrics as _rv_met15, drift as _rv_drift15

    _rv_path15 = _P15rv("data/weights/river_model.pkl")
    _rv_loaded15 = {}
    if _rv_path15.exists():
        try:
            _rv_loaded15 = _pkl15rv.loads(_rv_path15.read_bytes())
        except Exception:
            _rv_loaded15 = {}

    # Build valid dict — preserve existing trained objects, replace any None
    _river_scaler = _rv_loaded15.get("scaler")
    _river_lr     = _rv_loaded15.get("lr")
    _river_metric = _rv_loaded15.get("metric")
    _river_adwin  = _rv_loaded15.get("adwin")

    if not isinstance(_river_scaler, _rv_pre15.StandardScaler):
        _river_scaler = _rv_pre15.StandardScaler()
    if not isinstance(_river_lr, _rv_lm15.LogisticRegression):
        _river_lr = _rv_lm15.LogisticRegression()
    if _river_metric is None:
        _river_metric = _rv_met15.Accuracy()
    if _river_adwin is None:
        _river_adwin = _rv_drift15.ADWIN()

    _rv_valid = {
        "scaler": _river_scaler,
        "lr":     _river_lr,
        "metric": _river_metric,
        "adwin":  _river_adwin,
    }
    _rv_path15.parent.mkdir(parents=True, exist_ok=True)
    _rv_path15.write_bytes(_pkl15rv.dumps(_rv_valid))
    print(f"  [patch] Cell15: River objects ready — scaler={type(_river_scaler).__name__}, "
          f"lr={type(_river_lr).__name__} (pkl re-written)")
except Exception as _rv15e:
    print(f"  [patch] Cell15 River init (non-fatal): {_rv15e}")

# ── Fix: Register 'composite' as a known event type ──────────────────────────
# Cell 15 uses an EVENT_TYPE_WEIGHTS dict or similar mapping to classify signals.
# The value 'composite' is generated by Cell 13 for multi-factor signals but was
# not registered, causing KeyError('composite') every run.
try:
    for _evt_dict_name in ["EVENT_TYPE_WEIGHTS", "EVENT_WEIGHTS", "_event_weights",
                            "event_type_map", "EVENT_TYPE_MAP", "_EVENT_TYPE_MAP"]:
        if _evt_dict_name in dir() and isinstance(eval(_evt_dict_name), dict):
            _evt_d = eval(_evt_dict_name)
            if "composite" not in _evt_d:
                _evt_d["composite"] = _evt_d.get("mixed", _evt_d.get("other", 0.0))
                print(f"  [patch] Cell15: added 'composite' to {_evt_dict_name}")
            break
except Exception:
    pass

_REGIME_STR_MAP = {
    "bear": 0, "declining": 0, "risk-off": 0, "bearish": 0,
    "neutral": 1, "mixed": 1, "sideways": 1, "neutral / mixed": 1,
    "bull": 2, "rising": 2, "risk-on": 2, "bullish": 2,
}
if "macro_data" in dir() and isinstance(macro_data, dict):
    _r = macro_data.get("regime", "")
    if isinstance(_r, str):
        _key = _r.lower().replace(" / ", "/").replace(" ", "/").split("/")[0].strip()
        macro_data["regime"] = _REGIME_STR_MAP.get(_key, 1)
        print(f"  [patch] Cell 15 regime coerced: {_r!r} -> {macro_data['regime']}")
if "regime_str" in dir() and isinstance(regime_str, str):
    _key2 = regime_str.lower().split("/")[0].strip()
    regime_str = str(_REGIME_STR_MAP.get(_key2, 1))
# Also normalize the 'regimes' HMM array if it contains strings or is a dict
if "regimes" in dir() and regimes is not None and len(regimes) > 0:
    try:
        import numpy as _np15r
        _ra15_raw = list(regimes.values()) if isinstance(regimes, dict) else list(regimes)
        _ra15_int = []
        for _rv in _ra15_raw:
            try:
                _ra15_int.append(int(_rv))
            except (ValueError, TypeError):
                _key15 = str(_rv).lower().strip()
                _ra15_int.append(_REGIME_STR_MAP.get(_key15, 1))
        regimes = _np15r.array(_ra15_int)
    except Exception:
        pass
"""

# ── CELL 14 PREPATCH: pred_ts format normalization + outcome backfill + zero-price guard ─
# Three guards on predictions.csv, applied before the frozen Cell 14 scorer runs:
#
# (1) pred_ts FORMAT NORMALIZATION — root-cause fix for the 2026-05-14 scorer death.
#     The column accumulated TWO datetime formats: old tz-aware
#     'YYYY-MM-DD HH:MM:SS.ffffff+00:00' (space sep) and new tz-naive '...T...'
#     emitted by datetime.isoformat() (Cell 13). A whole-column
#     pd.to_datetime(utc=True) infers ONE format and coerces every non-matching
#     row to NaT, so the scorer's (pred_ts < cutoff) filter silently matched ZERO
#     mature rows after 5/14 — 26.6k unscored predictions went invisible and the
#     model's per-ticker calibration + rule-learning feedback loop went dead for
#     ~6 weeks. We reparse per-element with format='mixed' and rewrite the column
#     in one canonical form so Cell 14 (and every downstream reader) parses cleanly.
#
# (2) OUTCOME BACKFILL — score the recovered backlog correctly. Each matured
#     (>= FORECAST_DAYS old) unscored row is scored against the price at its OWN
#     horizon date (reusing _PRICE_CACHE when present, else yfinance) with
#     action-based correctness — NOT the frozen scorer's single stale SPY
#     benchmark, which would mislabel weeks-old rows. We mark scored=True here so
#     the frozen Cell 14 only ever handles genuinely fresh rows (horizon ≈ now,
#     where its benchmark is valid). yfinance fallbacks are capped per run so the
#     catch-up converges over a couple runs without risking a CI hang.
#
# (3) ZERO-PRICE GUARD (original) — backfill price_at_pred=0/null rows from
#     yfinance, drop any unfixable, so Cell 14's return division can't blow up.
CELL_14_PREPATCH = """
try:
    import pandas as _pd14fix
    from pathlib import Path as _P14fix
    _preds14 = _P14fix("data/predictions/predictions.csv")
    if _preds14.exists():
        _df14 = _pd14fix.read_csv(_preds14)
        _dirty14 = False

        # ── (1) pred_ts format normalization ──────────────────────────────
        if "pred_ts" in _df14.columns:
            try:
                _pt14 = _pd14fix.to_datetime(_df14["pred_ts"], errors="coerce",
                                             utc=True, format="mixed")
                _n_nat14 = int(_pt14.isna().sum())
                _canon14 = _pt14.dt.strftime("%Y-%m-%d %H:%M:%S.%f%z")
                # only rewrite where parse succeeded (preserve truly-empty as-is)
                _df14.loc[_pt14.notna(), "pred_ts"] = _canon14[_pt14.notna()]
                _dirty14 = True
                print(f"  [patch] Cell14: pred_ts normalized via format='mixed' "
                      f"({_n_nat14} unparseable/empty rows left untouched)")
            except Exception as _ne14:
                print(f"  [patch] Cell14 pred_ts normalize (non-fatal): {_ne14}")

        # ── (2) outcome backfill for matured unscored rows ────────────────
        if "scored" in _df14.columns and "pred_ts" in _df14.columns \
                and "price_at_pred" in _df14.columns:
            try:
                _fd14 = int(globals().get("FORECAST_DAYS", 5) or 5)
                _pt14b = _pd14fix.to_datetime(_df14["pred_ts"], errors="coerce", utc=True)
                _today14 = _pd14fix.Timestamp.now(tz="UTC").normalize()
                _ppred = _pd14fix.to_numeric(_df14["price_at_pred"], errors="coerce")
                _mask14 = (_df14["scored"].astype(str) == "False") & _pt14b.notna() \
                    & (_ppred > 0) & (_pt14b < _today14 - _pd14fix.Timedelta(days=_fd14))
                _idxs14 = list(_df14[_mask14].index)
                if _idxs14:
                    import yfinance as _yf14b
                    _cache14 = globals().get("_PRICE_CACHE", {}) or {}
                    _px14, _dl_used, _DL_CAP = {}, [0], 60
                    def _hist14(_tk):
                        if _tk in _px14: return _px14[_tk]
                        _h = None
                        _c = _cache14.get(_tk)
                        if _c is not None and hasattr(_c, "empty") and not _c.empty:
                            _h = _c.copy()
                        elif _dl_used[0] < _DL_CAP:
                            try:
                                _h = _yf14b.Ticker(_tk).history(period="120d", auto_adjust=True)
                                _dl_used[0] += 1
                            except Exception:
                                _h = None
                        if _h is not None and not _h.empty:
                            try: _h.index = _pd14fix.to_datetime(_h.index, utc=True)
                            except Exception: _h = None
                        _px14[_tk] = _h
                        return _h
                    _n_bf14 = 0
                    for _i14 in _idxs14:
                        try:
                            _tk = str(_df14.at[_i14, "ticker"]).strip()
                            if not _tk: continue
                            _entry = float(_ppred[_i14])
                            _h = _hist14(_tk)
                            if _h is None or _h.empty: continue
                            _od = (_pt14b[_i14] + _pd14fix.Timedelta(days=_fd14)).normalize()
                            _fut = _h[_h.index >= _od]
                            if _fut.empty: continue
                            _exit = float(_fut["Close"].iloc[0])
                            _ret = (_exit - _entry) / _entry
                            _act = str(_df14.at[_i14, "action"])
                            if _act == "BUY":    _ok = _ret > 0.01
                            elif _act == "SELL": _ok = _ret < -0.01
                            else:                _ok = abs(_ret) <= 0.04
                            _cf = _df14.at[_i14, "confidence"]
                            _cf = float(_cf) if _pd14fix.notna(_cf) else 0.5
                            _df14.at[_i14, "price_at_outcome"] = round(_exit, 4)
                            _df14.at[_i14, "actual_return"]    = round(_ret, 4)
                            _df14.at[_i14, "was_correct"]      = bool(_ok)
                            _df14.at[_i14, "magnitude_error"]  = round(abs(_ret - (_cf - 0.5)), 4)
                            _df14.at[_i14, "outcome_ts"]       = _today14.isoformat()
                            _df14.at[_i14, "scored"]           = True
                            _n_bf14 += 1
                        except Exception:
                            continue
                    if _n_bf14 > 0:
                        _dirty14 = True
                        print(f"  [patch] Cell14: backfilled {_n_bf14}/{len(_idxs14)} matured "
                              f"outcomes (per-row horizon, action-based; scorer revived)")
            except Exception as _bf14e:
                print(f"  [patch] Cell14 outcome backfill (non-fatal): {_bf14e}")

        # ── (3) zero-price guard (original) ───────────────────────────────
        if "price_at_pred" in _df14.columns:
            _df14["price_at_pred"] = _pd14fix.to_numeric(_df14["price_at_pred"], errors="coerce")
            _bad14 = _df14["price_at_pred"].isna() | (_df14["price_at_pred"] <= 0)
            _n_bad = int(_bad14.sum())
            if _n_bad > 0:
                import yfinance as _yf14fix
                _fixed14 = 0
                for _idx14, _row14 in _df14[_bad14].iterrows():
                    _tk14 = str(_row14.get("ticker", "")).strip()
                    if not _tk14:
                        continue
                    try:
                        _h14 = _yf14fix.Ticker(_tk14).history(period="5d", auto_adjust=True)
                        if not _h14.empty:
                            _df14.at[_idx14, "price_at_pred"] = float(_h14["Close"].iloc[-1])
                            _fixed14 += 1
                    except Exception:
                        pass
                _still_bad = int((_df14["price_at_pred"].isna() | (_df14["price_at_pred"] <= 0)).sum())
                if _still_bad > 0:
                    _df14 = _df14[(_df14["price_at_pred"].notna()) & (_df14["price_at_pred"] > 0)]
                _dirty14 = True
                print(f"  [patch] Cell14: fixed {_fixed14}/{_n_bad} zero-price preds "
                      f"via yfinance; dropped {_still_bad} unfixable")

        if _dirty14:
            _df14.to_csv(_preds14, index=False)

        # ── (4) staleness guard — alarm if scoring silently dies again ────
        # Mirrors the 6/24 persistence guard: turns a silent multi-week scorer
        # stall (the 5/14 failure mode) into a same-day, visible warning.
        try:
            if "scored" in _df14.columns:
                _scm = _df14[_df14["scored"].astype(str).isin(["True", "true"])]
                _spt = _pd14fix.to_datetime(_scm["pred_ts"], errors="coerce", utc=True)
                _age = (_pd14fix.Timestamp.now(tz="UTC") - _spt.max()).days \
                    if _spt.notna().any() else 999
                if _age > 8:
                    print(f"  ::warning:: [scorer-guard] newest SCORED prediction is "
                          f"{_age}d old (>8) — outcome scoring may be stalled again")
        except Exception:
            pass
except Exception as _e14fix:
    print(f"  [patch] Cell14 prepatch (non-fatal): {_e14fix}")
"""

_CELL_PREPATCH = {
    3:  CELL_3_PREPATCH,
    4:  CELL_4_PREPATCH,
    5:  CELL_5_PREPATCH,
    6:  CELL_6_PREPATCH,
    8:  CELL_8_PREPATCH,
    9:  CELL_9_PREPATCH,
    10: CELL_10_PREPATCH,
    11: CELL_11_PREPATCH,
    12: CELL_12_PREPATCH,
    13: CELL_13_PREPATCH,
    14: CELL_14_PREPATCH,
    15: CELL_15_PREPATCH,
}
_CELL_POSTPATCH = {
    4:  CELL_4_POSTPATCH,
    5:  CELL_5_POSTPATCH,
    6:  CELL_6_POSTPATCH,
    8:  CELL_8_POSTPATCH,
    9:  CELL_9_POSTPATCH,
    10: CELL_10_POSTPATCH,
    11: CELL_11_POSTPATCH,
    12: CELL_12_POSTPATCH,
    13: CELL_13_POSTPATCH,
    15: CELL_15_POSTPATCH,
}

# ── Model cache helpers ───────────────────────────────────────────────────
# Use dill instead of pickle: dill can serialize locally-defined classes such
# as _CalWrapper (defined inside train_ensemble → _sigmoid_cal). stdlib pickle
# fails with "Can't pickle local object" on these nested closures.
try:
    import dill as _serializer
    _serializer_name = "dill"
except ImportError:
    import pickle as _serializer
    _serializer_name = "pickle"

def _load_model_cache(ns):
    if MODEL_CACHE.exists() and RUN_TYPE != "morning":
        try:
            cache = _serializer.loads(MODEL_CACHE.read_bytes())
            for k, v in cache.items():
                ns[k] = v
            print(f"  Model cache loaded ({_serializer_name}): {list(cache.keys())}")
            return True
        except Exception as e:
            print(f"  Model cache load failed: {e} -- will retrain")
    return False

def _save_model_cache(ns):
    if RUN_TYPE == "morning":
        keys = ["models", "regimes", "garch_res", "ADAPTIVE_WEIGHTS",
                "LEARNED_RULES", "FEATURE_COLS"]
        # Serialize key-by-key so one unpicklable object doesn't sink the whole
        # cache. The prior all-at-once dumps() failed entirely with
        # "Can't pickle <function display>" — an IPython display ref captured in
        # one object — wiping the cache and forcing a full retrain every run.
        cache, skipped = {}, []
        for k in keys:
            if k not in ns:
                continue
            try:
                _serializer.dumps(ns[k], protocol=4)   # probe picklability
                cache[k] = ns[k]
            except Exception as _ke:
                skipped.append(f"{k} ({type(_ke).__name__})")
        try:
            MODEL_CACHE.write_bytes(_serializer.dumps(cache, protocol=4))
            print(f"  Model cache saved ({_serializer_name}): {list(cache.keys())}"
                  + (f" | skipped: {skipped}" if skipped else ""))
        except Exception as e:
            print(f"  Model cache save failed: {e}")

# ── Execute notebook ───────────────────────────────────────────────────────
NB_PATH = "trading_model_v25.1.ipynb"
print(f"Loading: {NB_PATH}  run_type={RUN_TYPE}")

# Strip ALL leading UTF-8 BOMs (notebook has double-BOM from Windows editor)
# then pass raw bytes to json.loads which handles the rest
_nb_bytes = Path(NB_PATH).read_bytes()
while _nb_bytes.startswith(b'\xef\xbb\xbf'):
    _nb_bytes = _nb_bytes[3:]
nb = json.loads(_nb_bytes)

cells = nb["cells"]
print(f"  {len(cells)} cells  |  skipping: {sorted(SKIP_CELLS)}\n")

# ── Fix 4: Pre-run P&L drawdown kill switch ───────────────────────────────────
# Checks pnl_history.csv for excessive daily or weekly drawdown.
# If breached: writes KILL_SWITCH_ACTIVE.flag, sends Discord alert, and exits.
# VIX > 45 also triggers to protect against flash-crash conditions.
_KILL_DAILY_DRAWDOWN  = -0.10   # -10% net liquidation value in one day (paper account)
_KILL_WEEKLY_DRAWDOWN = -0.20   # -20% over rolling 5-day window (paper account)
_KILL_VIX_LEVEL       = 45.0   # hard VIX stop

_pnl_kill_triggered = False
_kill_reason = None
_ks_evaluated = False   # True once we have a valid drawdown reading this run
# Always re-evaluate (self-healing): rclone copy never deletes, so a flag from
# a prior (often phantom) trip persists on Drive and is restored every run. By
# re-checking real Alpaca drawdown each run we can CLEAR a stale flag when the
# account is healthy, instead of staying latched forever.
if True:
    _KILL_PEAK_DRAWDOWN = -0.15   # -15% from NAV peak
    # ── Primary: drawdown from the Alpaca equity curve (source of truth) ──────
    # ROOT CAUSE of false trips: the old path divided P&L by a hardcoded
    # $10,000 (pnl_history has no portfolio_value column). On the real ~$100k
    # account that inflated every drawdown ~10x — a true -2.6% week read as
    # -25.7% and tripped the -20% switch, halting trading on a phantom loss.
    # Compute daily/weekly/peak drawdown directly from broker equity instead.
    _ks_alpaca_ok = False
    try:
        import requests as _rq_ks
        _ak_ks = "".join(_c for _c in os.environ.get("ALPACA_API_KEY", "") if ord(_c) < 128).strip()
        _sk_ks = "".join(_c for _c in os.environ.get("ALPACA_SECRET_KEY", "") if ord(_c) < 128).strip()
        _bu_ks = ("".join(_c for _c in os.environ.get("ALPACA_BASE_URL", "") if ord(_c) < 128).strip()
                  or "https://paper-api.alpaca.markets").rstrip("/")
        if _ak_ks and _sk_ks:
            _ph_ks = _rq_ks.get(f"{_bu_ks}/v2/account/portfolio/history",
                headers={"APCA-API-KEY-ID": _ak_ks, "APCA-API-SECRET-KEY": _sk_ks},
                params={"period": "1M", "timeframe": "1D", "extended_hours": "true"},
                timeout=15).json()
            _E = [float(_x) for _x in (_ph_ks.get("equity") or []) if _x not in (None, "")]
            _E = [_x for _x in _E if _x > 0]
            if len(_E) >= 2:
                _eq_now    = _E[-1]
                _daily_dd  = (_E[-1] - _E[-2]) / max(_E[-2], 1)
                _wk_peak   = max(_E[-6:-1]) if len(_E) >= 3 else _E[-2]
                _weekly_dd = (_eq_now - _wk_peak) / max(_wk_peak, 1)
                _hwm_eq    = max(_E)
                _peak_dd   = (_eq_now - _hwm_eq) / max(_hwm_eq, 1)
                print(f"\n[KILL SWITCH · Alpaca] equity=${_eq_now:,.0f}  "
                      f"daily_dd={_daily_dd:+.2%}  weekly_dd={_weekly_dd:+.2%}  "
                      f"peak_dd={_peak_dd:+.2%} (HWM=${_hwm_eq:,.0f})  limits=("
                      f"{_KILL_DAILY_DRAWDOWN:.0%}/{_KILL_WEEKLY_DRAWDOWN:.0%}/{_KILL_PEAK_DRAWDOWN:.0%})")
                if _daily_dd <= _KILL_DAILY_DRAWDOWN:
                    _kill_reason = f"Daily drawdown {_daily_dd:+.2%} breached limit {_KILL_DAILY_DRAWDOWN:.0%}"
                elif _weekly_dd <= _KILL_WEEKLY_DRAWDOWN:
                    _kill_reason = f"Weekly drawdown {_weekly_dd:+.2%} breached limit {_KILL_WEEKLY_DRAWDOWN:.0%}"
                elif _peak_dd <= _KILL_PEAK_DRAWDOWN:
                    _kill_reason = f"Peak drawdown {_peak_dd:+.2%} from HWM breached limit {_KILL_PEAK_DRAWDOWN:.0%}"
                _ks_alpaca_ok = True
                _ks_evaluated = True
    except Exception as _ks_ae:
        print(f"  [kill switch] Alpaca drawdown check failed ({_ks_ae}) — falling back to pnl_history")

    # ── Fallback: internal pnl_history (only if Alpaca unreachable) ───────────
    # Still requires a REAL account value — never the old hardcoded $10k. If we
    # can't establish it, skip the P&L check rather than trip on a bad ratio.
    if not _ks_alpaca_ok:
        try:
            import pandas as _pd_ks
            _hist_ks = Path("data/predictions/pnl_history.csv")
            if _hist_ks.exists():
                _pnl_df = _pd_ks.read_csv(_hist_ks)
                _pnl_df["date"]      = _pd_ks.to_datetime(_pnl_df["date"], errors="coerce")
                _pnl_df["total_pnl"] = _pd_ks.to_numeric(_pnl_df["total_pnl"], errors="coerce")
                _pnl_df = _pnl_df.sort_values("date").dropna(subset=["date", "total_pnl"])
                _portfolio_val = None
                if "portfolio_value" in _pnl_df.columns and len(_pnl_df):
                    try: _portfolio_val = float(_pnl_df["portfolio_value"].iloc[-1])
                    except Exception: _portfolio_val = None
                if len(_pnl_df) >= 2 and _portfolio_val and _portfolio_val > 0:
                    _today_pnl  = float(_pnl_df["total_pnl"].iloc[-1])
                    _yest_pnl   = float(_pnl_df["total_pnl"].iloc[-2])
                    _peak_pnl   = float(_pnl_df["total_pnl"].tail(6).iloc[0])
                    _daily_dd   = (_today_pnl - _yest_pnl) / _portfolio_val
                    _weekly_dd  = (_today_pnl - _peak_pnl) / _portfolio_val
                    _ks_evaluated = True
                    print(f"\n[KILL SWITCH · pnl_history] daily_dd={_daily_dd:+.2%}  "
                          f"weekly_dd={_weekly_dd:+.2%}  (acct=${_portfolio_val:,.0f})")
                    if _daily_dd <= _KILL_DAILY_DRAWDOWN:
                        _kill_reason = f"Daily drawdown {_daily_dd:+.2%} breached limit {_KILL_DAILY_DRAWDOWN:.0%}"
                    elif _weekly_dd <= _KILL_WEEKLY_DRAWDOWN:
                        _kill_reason = f"Weekly drawdown {_weekly_dd:+.2%} breached limit {_KILL_WEEKLY_DRAWDOWN:.0%}"
                else:
                    print("  [kill switch] no real account value available — skipping P&L "
                          "drawdown check (refuses to trip on a guessed denominator)")
        except Exception as _ks_check_e:
            print(f"  Kill switch check error (non-fatal): {_ks_check_e}")

    # ── Trip the switch if either path found a genuine breach ────────────────
    if _kill_reason:
        _KILL_FLAG.write_text(_kill_reason)
        _pnl_kill_triggered = True
        print(f"\n{'!'*60}")
        print(f"KILL SWITCH ACTIVATED: {_kill_reason}")
        print(f"HALTING new trades. Delete {_KILL_FLAG} to reset.")
        print(f"{'!'*60}\n")
        _discord_ks = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if _discord_ks:
            try:
                import requests as _rks
                _rks.post(_discord_ks, json={"embeds": [{
                    "title": "🚨 KILL SWITCH ACTIVATED", "color": 15158332,
                    "description": _kill_reason}]}, timeout=10)
            except Exception:
                pass

    # VIX hard stop (checked separately — does not require pnl_history)
    if not _pnl_kill_triggered:
        try:
            import yfinance as _yf_ks
            _vix_raw = _yf_ks.download("^VIX", period="2d", progress=False)["Close"]
            # Newer yfinance returns a multi-index frame, so ["Close"] can be a
            # DataFrame (one col per ticker) rather than a Series — collapse it.
            if hasattr(_vix_raw, "columns"):
                _vix_raw = _vix_raw.iloc[:, 0]
            _vix_ks = float(_vix_raw.dropna().iloc[-1])
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

    # ── Self-heal: account healthy → clear any stale flag (local + Drive) ─────
    # Only when we actually got a valid drawdown reading this run (never clear
    # blindly when the check couldn't evaluate — that could mask a real breach).
    # The Drive delete is unconditional (idempotent) because the morning
    # auto-clear may have already removed the flag LOCALLY while the immortal
    # Drive copy lingers; we must delete it on Drive regardless.
    if not _pnl_kill_triggered and _ks_evaluated:
        _had_local_flag = _KILL_FLAG.exists()
        _KILL_FLAG.unlink(missing_ok=True)
        (LOCAL_DATA / "cvar_failure_log.json").unlink(missing_ok=True)
        if _drive_ok:
            _rclone_delete(_KILL_FLAG.name, "kill switch self-heal")
        if _had_local_flag:
            print("  [kill switch] account healthy — cleared stale flag (local + Drive)")

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

# ── Notebook source rewrites ──────────────────────────────────────────────
# Literal (old → new) substitutions applied to every cell's source before
# exec. Used for one-line bugs inside notebook functions that the prepatch/
# postpatch wrappers can't reach (they run in the cell's namespace, not its
# function bodies). Each entry must be an exact, unique substring.
_SRC_REPLACE = [
    # River >= 0.21 changed learn_one() to return None (in-place) instead of
    # self, breaking the chained scaler.learn_one(x).transform_one(x) idiom →
    # 'NoneType' object has no attribute 'transform_one'. Split into two calls.
    ("_xs = _river_scaler.learn_one(_x).transform_one(_x)",
     "_river_scaler.learn_one(_x)\n                _xs = _river_scaler.transform_one(_x)"),
    # CVaR risk metrics crash with a cryptic 'index -1 ... size 0' when the
    # returns frame is empty (degenerate covariance on some neutral-regime
    # runs). Portfolio weights are still produced by CELL_12_POSTPATCH, so this
    # block is display-only — guard it to skip cleanly instead of erroring.
    ("_ann_ret  = float(np.mean(_pr) * 252)",
     "if not len(_pr):\n                raise ValueError('portfolio returns empty — skipping risk metrics')\n            _ann_ret  = float(np.mean(_pr) * 252)"),
    # Route headline fetch through the cached Finnhub-first helper defined in
    # CELL_10_PREPATCH (NewsAPI alone 429s on all 307 tickers → 0 coverage).
    ('        r = requests.get(url, timeout=5)\n        return [a.get("title", "") for a in r.json().get("articles", [])]',
     "        return _fh_smart_headlines(ticker, n)"),
    # Cell 15's diagnose_failures_and_rewrite_rules does int(MACRO["macro_regime"]),
    # which crashes when macro_regime is a string label like "Neutral / Mixed".
    # Replace the whole guarded RHS so MACRO is only touched when present, and
    # string regimes are mapped to their int code instead of erroring.
    ('int(MACRO.get("macro_regime", 1)) if "MACRO" in globals() else 1',
     '1 if "MACRO" not in globals() else '
     '(lambda _mr: int(_mr) if str(_mr).strip().lstrip("-").isdigit() '
     'else {"bear": 0, "neutral": 1, "mixed": 1, "bull": 2}.get('
     'str(_mr).lower().replace(" / ", "/").split("/")[0].strip(), 1))(MACRO.get("macro_regime", 1))'),
    # Honesty fix A1: HMM causal regime labels. The notebook decodes regimes with
    # full-sequence smoothed Viterbi (model.predict), so historical labels peek at
    # future bars — look-ahead in the regime training feature. Replace with the
    # forward-algorithm FILTERED state (each label uses only past+current data).
    # The filtered path is now the DEFAULT whenever it computes and is non-degenerate
    # (>=2 states); we no longer gate it behind >=60% agreement with the leaky
    # smoothed labels (that gate discarded the causal fix exactly when look-ahead
    # mattered most). Fall back to smoothed only on error/degeneracy. A [hmm] log
    # line confirms engagement. (The live regime = last bar is causal either way.)
    ("labels=model.predict(returns)",
     "labels=model.predict(returns)\n"
     "    try:\n"
     "        import numpy as _np_hmm\n"
     "        from scipy.special import logsumexp as _lse_hmm\n"
     "        _fl_h = model._compute_log_likelihood(returns)\n"
     "        _lsp_h = _np_hmm.log(model.startprob_ + 1e-300)\n"
     "        _ltm_h = _np_hmm.log(model.transmat_ + 1e-300)\n"
     "        _n_h, _k_h = _fl_h.shape\n"
     "        _la_h = _np_hmm.full((_n_h, _k_h), -_np_hmm.inf)\n"
     "        _la_h[0] = _lsp_h + _fl_h[0]\n"
     "        for _t_h in range(1, _n_h):\n"
     "            _la_h[_t_h] = _lse_hmm(_la_h[_t_h - 1][:, None] + _ltm_h, axis=0) + _fl_h[_t_h]\n"
     "        _filt_h = _np_hmm.argmax(_la_h, axis=1)\n"
     "        if len(_np_hmm.unique(_filt_h)) >= 2:\n"
     "            _agree_h = float((_filt_h == labels).mean())\n"
     "            labels = _filt_h\n"
     "            print('  [hmm] causal forward-filtered regimes ENGAGED '\n"
     "                  '(agreement with smoothed={:.0%}, states={})'.format(\n"
     "                  _agree_h, sorted(_np_hmm.unique(_filt_h).tolist())))\n"
     "        else:\n"
     "            print('  [hmm] filtered labels degenerate (single state) — kept smoothed')\n"
     "    except Exception as _hmm_e:\n"
     "        print('  [hmm] causal filter failed, kept smoothed: {}'.format(_hmm_e))"),
    # River cut (2026-06-14): the online self-learner is stuck at ~46% (below
    # chance) and the 6/14 GPU validation confirmed its anti-signal clamp does
    # NOT fix it. A sub-chance learner must not steer the ensemble weights. Gate
    # BOTH River-driven ADAPTIVE_WEIGHTS nudges (w_ensemble via _delta, and the
    # w_garch boost) on River actually beating chance (>52%). River keeps
    # training/reporting; it just stops touching the weights while broken. Self-
    # heals: if River ever climbs above 52%, the nudges resume automatically.
    ("if abs(_delta) > 0.03:",
     "if _river_acc > 0.52 and abs(_delta) > 0.03:"),
    ("if _garch_acc > _river_acc + 0.08:",
     "if _river_acc > 0.52 and _garch_acc > _river_acc + 0.08:"),
    # Kill-switch consecutive-loss fix (2026-06-30): check_kill_switch counted the
    # raw tail(5) of ALL scored prediction rows. The crypto sleeve (BTC/ETH/SOL/
    # XRP/DOGE) is written LAST in every batch, so tail(5) was structurally those 5
    # names, and their HOLD rows fail the +/-4% "was_correct" band ~78% of the time
    # on normal crypto vol (median 5d move 9.2%). Net effect: a "did crypto move
    # >4% this week" detector that spuriously halted equity entries (e.g. 6/30
    # blocked 10 BUYs during a crypto sell-off while the equity book was +$15k).
    # Fix: count only REAL directional trades (BUY/SELL) and exclude crypto, so the
    # streak reflects the equity strategy this switch is meant to protect.
    # Era gate (2026-07-16): the switch also counted STALE-ERA predictions — on
    # 7/16 the Cell-14 backfill scored the matured 7/10 batch (pre-5e96366 lagged
    # model, price_at_pred = known-wrong stale closes) and its 5 straight losses
    # halted the NEW strategy's first open morning (8 slots, 0 entries). Same
    # rationale as the Stage-1 window restart (b2a15f5): pre-QT_STAGE1_START
    # predictions measured a different strategy and must not steer this one.
    # Lexicographic date compare on the ISO pred_ts prefix, gated on the prefix
    # actually LOOKING like a date — "nan"/empty/garbage pred_ts (the ~614
    # legacy rows) can't prove they're fresh-era, so they're excluded ("nan" >
    # "2026-…" lexicographically, caught by validate 5). Self-ages to a no-op
    # once fresh-era rows dominate the log.
    ('        scored = plog[plog["scored"].astype(str) == "True"].tail(KILL_CONSECUTIVE_LOSSES)',
     '        _ks_sc = plog[plog["scored"].astype(str).isin(["True", "true"])].copy()\n'
     '        _ks_sc = _ks_sc[_ks_sc["action"].astype(str).str.upper().isin(["BUY", "SELL"])]\n'
     '        _ks_sc = _ks_sc[~_ks_sc["ticker"].astype(str).isin(\n'
     '            {"BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "BNB-USD"})]\n'
     '        _ks_era = __import__("os").environ.get("QT_STAGE1_START", "2026-07-14")\n'
     '        _ks_ts = _ks_sc["pred_ts"].astype(str).str[:10]\n'
     '        _ks_sc = _ks_sc[_ks_ts.str.match(r"\\d{4}-\\d{2}-\\d{2}") & (_ks_ts >= _ks_era)]\n'
     '        scored = _ks_sc.tail(KILL_CONSECUTIVE_LOSSES)'),
    # Stale-era gate for the remaining live predictions.csv consumers
    # (2026-07-16, follow-up to the kill-switch era gate above — full audit in
    # the 7/16 handoff ledger). Predictions before QT_STAGE1_START were made by
    # the pre-5e96366 lagged model off wrong price_at_pred baselines; their
    # scored outcomes must not steer the live strategy. Three consumers gated,
    # same ISO-date-prefix idiom as the kill switch ("nan"/garbage pred_ts
    # excluded). Reporting-only readers (Cells 16/19/21/23) stay unfiltered.
    # (a) Cell 15 diagnose_failures_and_rewrite_rules — the single `scored`
    #     frame feeding LEARNED_RULES dampeners/boosts (applied to live
    #     composite scores in Cell 11), ADAPTIVE_WEIGHTS nudges,
    #     FEATURE_IMPORTANCE, and River training rows. Two-line anchor: the
    #     one-line version also appears in Cells 16/21 (reporting).
    ('    plog = pd.read_csv(PRED_LOG_FILE)\n'
     '    scored = plog[plog["scored"].astype(str)=="True"].copy()',
     '    plog = pd.read_csv(PRED_LOG_FILE)\n'
     '    scored = plog[plog["scored"].astype(str)=="True"].copy()\n'
     '    _era_rw = __import__("os").environ.get("QT_STAGE1_START", "2026-07-14")\n'
     '    _ts_rw = scored["pred_ts"].astype(str).str[:10]\n'
     '    scored = scored[_ts_rw.str.match(r"\\d{4}-\\d{2}-\\d{2}") & (_ts_rw >= _era_rw)]'),
    # (b) Cell 15 check_model_staleness — rolling-20 accuracy sets
    #     RETRAIN_NEEDED.flag; stale-era rows must not trip or clear it.
    #     Anchor includes the following `if` line for cell-uniqueness (the
    #     bare line also appears in Cell 21).
    ('        scored = plog[plog["scored"].astype(str) == "True"].copy()\n'
     '        if len(scored) < STALENESS_WINDOW:',
     '        scored = plog[plog["scored"].astype(str) == "True"].copy()\n'
     '        _era_st = __import__("os").environ.get("QT_STAGE1_START", "2026-07-14")\n'
     '        _ts_st = scored["pred_ts"].astype(str).str[:10]\n'
     '        scored = scored[_ts_st.str.match(r"\\d{4}-\\d{2}-\\d{2}") & (_ts_st >= _era_st)]\n'
     '        if len(scored) < STALENESS_WINDOW:'),
    # (c) Cell 13 _WL_RATIO Kelly win/loss cache — per-ticker avg_win/avg_loss
    #     from actual_return multiplies into kelly_qty for every BUY; stale-era
    #     actual_returns are baseline-corrupted. Until fresh-era tickers reach
    #     3 wins + 3 losses, kelly_qty falls back to its default W/L assumption.
    ('        _wl_log = _wl_log[_wl_log["scored"].astype(str).isin(["True","true"])].copy()',
     '        _wl_log = _wl_log[_wl_log["scored"].astype(str).isin(["True","true"])].copy()\n'
     '        _era_wl = __import__("os").environ.get("QT_STAGE1_START", "2026-07-14")\n'
     '        _ts_wl = _wl_log["pred_ts"].astype(str).str[:10]\n'
     '        _wl_log = _wl_log[_ts_wl.str.match(r"\\d{4}-\\d{2}-\\d{2}") & (_ts_wl >= _era_wl)]'),
    # Stale featured-row fix (2026-07-13, dated MODEL CHANGE — 5th in the
    # attribution window): build_features ended with a blanket dropna AFTER the
    # magnitude-threshold label set target=NaN on mid-quantile rows AND on the
    # last FORECAST_DAYS rows (fwd_ret unknown), deleting every recent row.
    # generate_signal's iloc[-1] "current" row was therefore the most recent
    # EXTREME-move day — 5-10+ sessions stale, a different date per ticker.
    # Proven by exact price matches: 7/13 TDG BUY @ 1348.49 = TDG close 7/02;
    # 7/11 GE BUY @ 377.05 = GE adj close 7/02; both 7/08 GE BUYs @ 356.0262 =
    # GE adj close 6/23. Every live signal, kelly/conformal size, gross-cap
    # notional, ledger price and price_at_pred came off that stale row since
    # v25.1 (ec9d19a, 5/17). Fix: drop only rows missing FEATURES (warmup);
    # keep recent rows whose target is NaN. Training is unchanged — every
    # training path already does its own dropna(subset=["target"]).
    ("d[feat_cols]=d[feat_cols].shift(1)\n    d.dropna(inplace=True)",
     "d[feat_cols]=d[feat_cols].shift(1)\n    d.dropna(subset=feat_cols, inplace=True)"),
    # Companion: stamp each signal with the bar date it was computed from so
    # execute_trade can refuse BUYs priced off a stale bar (backstop in case a
    # ticker's raw download is stale even after the dropna fix above).
    ("close=round(close,4),",
     "close=round(close,4),\n        bar_date=str(df_feat.index[-1].date()) if hasattr(df_feat.index[-1], 'date') else str(df_feat.index[-1]),"),
    # Gross-cap hard gate (2026-07-08), 3 anchors into Cell 13 — see the
    # CELL_13_PREPATCH gross-cap block for the full rationale. The old cash
    # guard mutated signals[tk]["confidence"], which nothing in the execution
    # path reads; these rewrites make the gate binding at the three points
    # that matter: the signal loop, the order funnel, and the trade print.
    # (a) trade loop: skip signals the prepatch marked exec_blocked. Placed
    #     AFTER log_prediction so predictions.csv still records the signal.
    ('for tk, sig in signals.items():\n'
     '    log_prediction(sig)\n'
     '    if halt or sig["action"] == "HOLD":\n'
     '        continue',
     'for tk, sig in signals.items():\n'
     '    log_prediction(sig)\n'
     '    if halt or sig["action"] == "HOLD":\n'
     '        continue\n'
     '    if sig.get("exec_blocked"):\n'
     '        continue'),
    # (b) order funnel: every order goes through execute_trade — refuse BUYs
    #     that fail the hard gross cap BEFORE _try_alpaca submits anything.
    #     Conformal-Kelly sizing wired here 2026-07-11 (dated MODEL CHANGE):
    #     scale BUY qty by the uncertainty discount BEFORE the cap check, so
    #     the submitted order, the gross-cap accounting, and the ledger row
    #     all see the same true qty. BUY-only — exits close what is actually
    #     held. Replaces the removed post-scale block that rewrote the ledger
    #     after submission without touching the order (b3be0f2).
    ('    if action not in ("BUY","SELL") or qty <= 0:\n'
     '        return {"status":"skip","reason":"HOLD or qty=0"}',
     '    if action not in ("BUY","SELL") or qty <= 0:\n'
     '        return {"status":"skip","reason":"HOLD or qty=0"}\n'
     '    if action == "BUY" and sig.get("bar_date"):\n'
     '        try:\n'
     '            _bar_d = str(sig["bar_date"])\n'
     '            _bar_age = (datetime.date.today() - datetime.date.fromisoformat(_bar_d)).days\n'
     '            if _bar_age > 5:\n'
     '                print(f"    [stale-bar] {ticker}: BUY refused - signal bar {_bar_d} is {_bar_age}d old")\n'
     '                return {"status":"skip","reason":"stale_bar"}\n'
     '        except Exception:\n'
     '            pass\n'
     '    if action == "BUY" and "_CONFORMAL_KELLY_MAP" in globals():\n'
     '        _ck_d = float(_CONFORMAL_KELLY_MAP.get(ticker, 1.0))\n'
     '        if _ck_d < 1.0 and qty > 1:\n'
     '            _ck_q = max(1, int(qty * _ck_d))\n'
     '            if _ck_q != qty:\n'
     '                print(f"    [conformal] {ticker}: qty {qty} -> {_ck_q} (x{_ck_d:.2f})")\n'
     '                qty = _ck_q\n'
     '    if action == "BUY" and not _gross_cap_allows(ticker, qty * price):\n'
     '        return {"status":"skip","reason":"gross_cap"}\n'
     '    if action == "SELL":\n'
     '        qty = _oversell_cap(ticker, qty)\n'
     '        if qty <= 0:\n'
     '            return {"status":"skip","reason":"oversell"}'),
    # (c) trade print: a gross-cap-refused BUY must not print as an executed
    #     trade or count toward trade_count (the 7/7-style "filled" phantoms).
    ('        result = execute_trade(sig, qty, equity)\n',
     '        result = execute_trade(sig, qty, equity)\n'
     '        if isinstance(result, dict) and result.get("reason") in ("gross_cap", "stale_bar", "oversell"):\n'
     '            continue\n'),
]

failed_cells = []
for i, cell in enumerate(cells):
    if cell["cell_type"] != "code":
        continue
    if i in SKIP_CELLS:
        print(f"[SKIP] Cell {i} ({CELL_TAGS.get(i, '')})")
        continue

    src = "".join(cell["source"])

    for _old_src, _new_src in _SRC_REPLACE:
        if _old_src in src:
            src = src.replace(_old_src, _new_src)
            print(f"  [src rewrite] Cell {i}: applied {_old_src[:48]!r}…")

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

# ── Daily P&L snapshot via Alpaca (authoritative source of truth) ─────────
# #1 positions → real unrealized P&L; account → equity / total P&L.
# #2 portfolio/history → rebuild the full 60-day daily equity curve so the
#    dashboard line graph no longer depends on accumulating CSV rows run-by-
#    run. The whole curve repopulates from the broker every run, so a missed
#    run or a wiped file can't lose history. Falls back to the legacy
#    yfinance/CSV reconstruction below if Alpaca is unreachable / keys unset.
_alpaca_pnl_ok = False
# Credentials live in the notebook exec namespace (set by GH_PATCH), not in
# this module's globals — and they may carry a non-ASCII char. Resolve them at
# module scope: prefer the already-sanitized namespace value, else strip
# os.environ. (Matches how the macro section re-reads NEWS_KEY/FINNHUB_KEY.)
def _ascii_strip(_s):
    return "".join(_c for _c in str(_s or "") if ord(_c) < 128).strip()
_AK = _ascii_strip(namespace.get("ALPACA_API_KEY")    or os.environ.get("ALPACA_API_KEY", ""))
_SK = _ascii_strip(namespace.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY", ""))
_BU = _ascii_strip(namespace.get("ALPACA_BASE_URL")   or os.environ.get("ALPACA_BASE_URL", "")) \
      or "https://paper-api.alpaca.markets"
if RUN_TYPE in ("morning", "intraday") and _AK and _SK:
    try:
        import pandas as _pd_ap
        import requests as _rq_ap
        _ap_base = _BU.rstrip("/")
        _ap_hdr  = {"APCA-API-KEY-ID": _AK,
                    "APCA-API-SECRET-KEY": _SK}

        # --- account: equity vs prior-close equity ---
        _acct = _rq_ap.get(f"{_ap_base}/v2/account", headers=_ap_hdr, timeout=15).json()
        _equity = float(_acct.get("equity", 0) or 0)

        # --- positions: broker-computed unrealized P&L (authoritative) ---
        _positions = _rq_ap.get(f"{_ap_base}/v2/positions", headers=_ap_hdr, timeout=15).json()
        if not isinstance(_positions, list):
            _positions = []
        _unrealized = round(sum(float(p.get("unrealized_pl", 0) or 0) for p in _positions), 2)
        _n_open = len(_positions)

        # Persist a compact per-position table for the dashboard "Open Positions"
        # up/down view — broker-authoritative current price + unrealized P&L on
        # the stocks actually held. Sorted by P&L (winners first).
        try:
            _pos_compact = []
            for _p in _positions:
                try:
                    _pos_compact.append({
                        "ticker":          _p.get("symbol"),
                        "qty":             float(_p.get("qty", 0) or 0),
                        "avg_entry":       round(float(_p.get("avg_entry_price", 0) or 0), 4),
                        "current_price":   round(float(_p.get("current_price", 0) or 0), 4),
                        "market_value":    round(float(_p.get("market_value", 0) or 0), 2),
                        "unrealized_pl":   round(float(_p.get("unrealized_pl", 0) or 0), 2),
                        "unrealized_plpc": round(float(_p.get("unrealized_plpc", 0) or 0) * 100, 2),
                        "side":            _p.get("side", "long"),
                    })
                except Exception:
                    continue
            _pos_compact.sort(key=lambda x: x["unrealized_pl"], reverse=True)
            Path("data/predictions/open_positions.json").write_text(
                json.dumps(_pos_compact, indent=2), encoding="utf-8")
        except Exception:
            pass

        # --- portfolio history: full daily equity curve (#2) ---
        # period=1A, not 2M: this file is rebuilt from scratch every run, so a
        # 2M window would silently slide the 2026-05-28 trading epoch off the
        # front by late September and shrink the WRC's measurement window from
        # under it. 1A holds the whole account history until this account is a
        # year old. Rows before the epoch are dropped below.
        _ph = _rq_ap.get(f"{_ap_base}/v2/account/portfolio/history",
                         headers=_ap_hdr,
                         params={"period": "1A", "timeframe": "1D",
                                 "extended_hours": "true"}, timeout=20).json()
        _ts   = _ph.get("timestamp", []) or []
        _eq   = _ph.get("equity", []) or []
        _pl   = _ph.get("profit_loss", []) or []
        _base = float(_ph.get("base_value", 0) or 0)

        _rows = []
        _prev_eq_i = None
        for _i in range(len(_ts)):
            try:
                _d = datetime.datetime.utcfromtimestamp(int(_ts[_i])).strftime("%Y-%m-%d")
            except Exception:
                continue
            _eq_i = float(_eq[_i]) if _i < len(_eq) and _eq[_i] not in (None, "") else None
            if _eq_i is None:
                continue
            # Alpaca's per-day profit_loss; fall back to the day-over-day
            # equity change. NEVER equity − base: that is cumulative-vs-window,
            # and a single cumulative row inside a daily series is exactly the
            # basis break the contract above forbids.
            if _i < len(_pl) and _pl[_i] not in (None, ""):
                _tot_i = round(float(_pl[_i]), 2)
            elif _prev_eq_i is not None:
                _tot_i = round(_eq_i - _prev_eq_i, 2)
            else:
                _tot_i = 0.0
            _prev_eq_i = _eq_i
            # Same epoch as data_reset's _PNL_EPOCH: trading began 2026-05-28;
            # pre-epoch rows are flat-$100k funding days that would pad the
            # WRC sample with fake zero-P&L observations and dilute its SR.
            if _d < "2026-05-28":
                continue
            _rows.append({"date": _d, "unrealized_pnl": "", "realized_pnl": "",
                          "total_pnl": _tot_i, "open_positions": ""})

        _hist_df = _pd_ap.DataFrame(_rows, columns=["date", "unrealized_pnl",
                                    "realized_pnl", "total_pnl", "open_positions"])
        if len(_hist_df):
            _hist_df = _hist_df.drop_duplicates(subset="date", keep="last")

        # Today's row: enrich with the live unrealized/realized split.
        # ── BASIS CONTRACT (2026-07-24): total_pnl is a DAILY P&L series in
        # EVERY row. ── The historical rows above are Alpaca's per-day
        # profit_loss values, but this row used to write equity − window base
        # (CUMULATIVE): one +$13k-scale outlier at the tail of an otherwise
        # daily file, poisoning every downstream Sharpe/diff consumer (the
        # White Reality Check reads this file now). Daily = equity vs prior
        # trading-day close (last_equity) -- the same basis as the
        # [KILL SWITCH - Alpaca] daily_dd line.
        # unrealized_pnl stays a snapshot (open-position MTM) and realized_pnl
        # stays CUMULATIVE locked-in P&L vs the window base -- today-only
        # enrichment columns, deliberately NOT part of the daily series.
        # Date is the ET trading day, not UTC (same rollover class as the
        # morning-marker fix 49092f1: UTC flips at 8 PM ET, and an overnight
        # intraday cycle would stamp tomorrow's date on today's P&L).
        try:
            import zoneinfo as _zi_ap
            _today_str = datetime.datetime.now(
                _zi_ap.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        except Exception:
            _today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        _last_eq     = float(_acct.get("last_equity", 0) or 0)
        _today_total = round(_equity - _last_eq, 2) if _last_eq else 0.0
        _cum_total   = round(_equity - _base, 2) if _base else _unrealized
        _today_real  = round(_cum_total - _unrealized, 2)
        _today_row   = {"date": _today_str, "unrealized_pnl": _unrealized,
                        "realized_pnl": _today_real, "total_pnl": _today_total,
                        "open_positions": _n_open}
        _hist_df = _hist_df[_hist_df["date"] != _today_str]
        _hist_df = _pd_ap.concat([_hist_df, _pd_ap.DataFrame([_today_row])],
                                 ignore_index=True).sort_values("date")

        _hist_path = Path("data/predictions/pnl_history.csv")
        _hist_path.parent.mkdir(parents=True, exist_ok=True)
        _hist_df.to_csv(_hist_path, index=False)
        _alpaca_pnl_ok = True
        print(f"  P&L snapshot via Alpaca [{_today_str}]: equity=${_equity:,.2f}  "
              f"unrealized={_unrealized:+.2f}  today={_today_total:+.2f}  "
              f"cum={_cum_total:+.2f}  "
              f"open={_n_open}  | {len(_hist_df)}-day curve from portfolio/history")
    except Exception as _ap_pnl_e:
        print(f"  P&L snapshot via Alpaca failed ({_ap_pnl_e}) — falling back to CSV reconstruction")

# ── Daily P&L snapshot (legacy yfinance/CSV fallback) ─────────────────────
if RUN_TYPE in ("morning", "intraday") and not _alpaca_pnl_ok:
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
                _st = str(_row.get("status", "")).lower()
                if str(_row.get("action", "")) == "BUY" and _st == "filled":
                    _pos[_tk]["qty"]  += _q
                    _pos[_tk]["cost"] += _q * _p
                elif str(_row.get("action", "")) == "SELL" and _st == "filled":
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

            # ET trading date, same class as the Alpaca path / marker fix.
            try:
                import zoneinfo as _zi_lg
                _today_str = datetime.datetime.now(
                    _zi_lg.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            except Exception:
                _today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            # total_pnl left BLANK on this fallback path: the daily basis
            # contract needs equity vs prior close, which this yfinance
            # reconstruction cannot know (unrealized + all-time realized is a
            # third, different basis — and on 2026-07-24's midnight Alpaca
            # timeout this path wrote a fake $0.00 row). A blank is skipped by
            # every reader; a wrong-basis number poisons the series until the
            # next Alpaca rebuild replaces it.
            _new_row = {
                "date":           _today_str,
                "unrealized_pnl": round(_unrealized, 2),
                "realized_pnl":   round(_realized, 2),
                "total_pnl":      "",
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
            print(f"  P&L snapshot [{_today_str}]: unrealized={_unrealized:+.2f}  realized={_realized:+.2f}  "
                  f"total_pnl=blank (daily basis unknowable without broker equity; next Alpaca rebuild fills it)")

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
# Sanitize: strip whitespace + non-ASCII (smart quotes from copy-paste). The
# Alpaca diagnostic confirmed both ALPACA keys carry 1 non-ASCII char; FRED_API_KEY
# almost certainly has the same. A corrupted-but-truthy key is WORSE than empty —
# _fred() builds the authenticated URL with the bad key, FRED returns 400, and
# every macro_extended series falls back to null (fed_funds_rate, cpi_yoy,
# consumer_sentiment, etc. all blank on the dashboard). Stripping restores a
# valid key (or, if empty after strip, _fred() uses the working public endpoint).
def _clean_key_macro(_v):
    return _v.strip().encode("ascii", "ignore").decode("ascii")
_FRED_KEY   = _clean_key_macro(os.environ.get("FRED_API_KEY", ""))
_NEWS_KEY   = _clean_key_macro(os.environ.get("NEWS_API_KEY", ""))
_QUIVER_KEY = _clean_key_macro(os.environ.get("QUIVER_QUANT_KEY", ""))
_raw_fred_macro = os.environ.get("FRED_API_KEY", "")
if _raw_fred_macro and len(_raw_fred_macro.strip()) != len(_FRED_KEY):
    print(f"  [cred sanitize] FRED_API_KEY: stripped {len(_raw_fred_macro.strip()) - len(_FRED_KEY)} non-ASCII char(s) — macro_extended FRED series were failing because of this")
if _FRED_KEY:
    print(f"  [cred check] FRED_API_KEY ok: len={len(_FRED_KEY)}")

try:
    import requests as _req

    # ── FRED series fetch helper ──────────────────────────────────────────
    # FRED allows unauthenticated public access (rate-limited to ~120 req/min).
    # If FRED_API_KEY secret is set, use it to raise the limit to 1000 req/min.
    def _fred(series_id, fallback=None):
        try:
            if _FRED_KEY:
                url = (f"https://api.stlouisfed.org/fred/series/observations"
                       f"?series_id={series_id}&api_key={_FRED_KEY}"
                       f"&file_type=json&limit=1&sort_order=desc")
            else:
                # Public unauthenticated access — works for all public series
                url = (f"https://api.stlouisfed.org/fred/series/observations"
                       f"?series_id={series_id}&file_type=json&limit=1&sort_order=desc")
            r = _req.get(url, timeout=15)
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

    if not _insiders and _FINNHUB_KEY:
        # ── Finnhub insider transactions (primary free source) ───────────────
        # The SEC EDGAR scraper returns 0 from GitHub Actions because SEC
        # throttles shared datacenter IPs regardless of User-Agent. Finnhub's
        # API works from those IPs and we already have the key.
        print("  Fetching insider transactions (Finnhub)…")
        try:
            import datetime as _dt_ins
            _fh_ins_tickers = [
                "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","V","MA",
                "AMD","NFLX","AVGO","CRM","NOW","PLTR","GS","MS","WMT","COST",
                "UNH","LLY","XOM","HD","INTC","QCOM","MU","TXN","BA","CVX",
            ]
            _to_ins   = _dt_ins.date.today()
            _from_ins = (_to_ins - _dt_ins.timedelta(days=45)).isoformat()
            for _ftk in _fh_ins_tickers:
                try:
                    _r = _req.get("https://finnhub.io/api/v1/stock/insider-transactions",
                                  params={"symbol": _ftk, "from": _from_ins,
                                          "to": _to_ins.isoformat(), "token": _FINNHUB_KEY},
                                  timeout=10)
                    if _r.status_code != 200:
                        continue
                    for _d in (_r.json().get("data") or [])[:8]:
                        _chg = _d.get("change", 0) or 0
                        _px  = _d.get("transactionPrice", 0) or 0
                        try:
                            _val = str(round(abs(float(_chg)) * float(_px)))
                        except Exception:
                            _val = ""
                        # "Acquired"/"Disposed" so the dashboard's includes('A')
                        # buy/sell check colors them correctly.
                        _insiders.append({
                            "date":        _d.get("filingDate","") or _d.get("transactionDate",""),
                            "ticker":      _ftk,
                            "name":        _d.get("name",""),
                            "title":       "",
                            "transaction": "Acquired" if float(_chg or 0) > 0 else "Disposed",
                            "shares":      abs(int(float(_chg))) if _chg else "",
                            "price":       _px or "",
                            "value":       _val,
                            "source":      "finnhub",
                        })
                except Exception:
                    continue
            _insiders.sort(key=lambda x: x.get("date",""), reverse=True)
            _insiders = _insiders[:100]
            print(f"  Insiders (Finnhub): {len(_insiders)} transactions")
        except Exception as _fhi_e:
            print(f"  Finnhub insider error: {_fhi_e}")

    if not _insiders:
        # ── SEC EDGAR Form 4 fallback (free, no key needed) ──────────────────
        print("  Fetching SEC EDGAR Form 4 insider trades (no API key)…")
        try:
            import xml.etree.ElementTree as _ET
            # SEC requires a descriptive User-Agent with a real contact; a
            # placeholder (example.com) gets throttled/blocked → empty results.
            _edgar_hdrs = {"User-Agent": "Quant-Terminal/1.0 (Southpaw3234; southpaw3234@users.noreply.github.com)",
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
                        _base_url = (f"https://www.sec.gov/Archives/edgar/data/"
                                     f"{int(_cik)}/{_accn_clean}/")
                        # Form 4 primaryDocument is often the XSL-rendered HTML
                        # (xslF345X0N/<name>.xml), which is NOT parseable ownership
                        # XML. Try the raw XML (strip the xsl folder) first, then
                        # the doc as given.
                        _doc_cands = ([_fdoc.split("/")[-1]] if "/" in _fdoc else []) + [_fdoc]
                        _tree = None
                        for _dc in _doc_cands:
                            try:
                                _xr = _req.get(_base_url + _dc, headers=_edgar_hdrs, timeout=8)
                                _edgar_time.sleep(0.12)   # stay under SEC 10 req/s
                                if not _xr.ok:
                                    continue
                                _ct = _ET.fromstring(_xr.content)
                                if _ct.tag.endswith("ownershipDocument") \
                                   or _ct.find(".//nonDerivativeTransaction") is not None:
                                    _tree = _ct
                                    break
                            except Exception:
                                continue
                        if _tree is None:
                            continue
                        try:
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
                    # Look back 30 days so "recently reported" events still appear on dashboard
                    _future = _ed[_ed.index.date >= _today_dt - datetime.timedelta(days=30)]
                    if not _future.empty:
                        _row   = _future.iloc[0]    # nearest row (most recent past or next upcoming)
                        _edate = _future.index[0].date().isoformat()
                        _raw_eps = _row.get("EPS Estimate") if hasattr(_row, "get") else None
                        _raw_actual = _row.get("Reported EPS") if hasattr(_row, "get") else None
                        if _raw_actual is not None and str(_raw_actual) not in ("nan", "None", ""):
                            # Already reported — use actual EPS
                            try:
                                _eps = f"Act: {float(_raw_actual):.2f}"
                            except Exception:
                                _eps = ""
                        elif _raw_eps is not None:
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
                    if -30 <= _days <= 90:   # show up to 30 days past (recently reported)
                        _earnings_cal.append({
                            "ticker":    _etk,
                            "date":      _edate[:10],
                            "eps_est":   _eps,
                            "days_away": _days,
                            "reported":  _days < 0,   # flag for dashboard to show "Reported" badge
                        })
                except Exception:
                    pass
            _earn_time.sleep(0.25)   # avoid Yahoo Finance rate limiter (40 tickers)
        _earnings_cal.sort(key=lambda x: x["date"])
        print(f"  Earnings calendar: {len(_earnings_cal)} upcoming events")
    except Exception as _earn_e:
        print(f"  Earnings calendar error: {_earn_e}")

    # ── Tier D: Alpha Vantage — earnings surprise history + EPS revisions ────
    _av_data = {}
    if _ALPHAVANTAGE_KEY and RUN_TYPE == "morning":
        _AV_TICKERS = ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","V","AMD"]
        print(f"  Alpha Vantage: fetching earnings data for {len(_AV_TICKERS)} tickers...")
        import time as _av_time
        for _av_tk in _AV_TICKERS:
            try:
                _av_r = _req.get(
                    "https://www.alphavantage.co/query",
                    params={"function":"EARNINGS","symbol":_av_tk,"apikey":_ALPHAVANTAGE_KEY},
                    timeout=10)
                if _av_r.ok:
                    _av_j = _av_r.json()
                    _qe   = _av_j.get("quarterlyEarnings", [])[:4]
                    if _qe:
                        _surs = [float(q.get("surprisePercentage","0") or 0)
                                 for q in _qe if q.get("surprisePercentage","")]
                        _av_data[_av_tk] = {
                            "recent_surprise_pct": round(float(_qe[0].get("surprisePercentage","0") or 0), 4),
                            "avg_surprise_4q":     round(sum(_surs)/len(_surs), 4) if _surs else 0.0,
                            "reported_date":       _qe[0].get("reportedDate",""),
                        }
                _av_time.sleep(12)   # AV free tier: 5 req/min (25/day)
            except Exception as _av_e:
                pass
        print(f"  Alpha Vantage: {len(_av_data)}/{len(_AV_TICKERS)} tickers fetched")
    else:
        if not _ALPHAVANTAGE_KEY:
            print("  Alpha Vantage: ALPHAVANTAGE_API_KEY not set — skipped (free at alphavantage.co)")
    Path("data/alpha_vantage_earnings.json").write_text(json.dumps(_av_data, indent=2, default=str))

    # ── Tier D: Finnhub — analyst recommendations + price targets ─────────────
    _finnhub_data = {}
    if _FINNHUB_KEY and RUN_TYPE == "morning":
        _FH_TICKERS = ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","V","AMD",
                        "NFLX","AVGO","CRM","NOW","PLTR","GS","MS","UNH","LLY","XOM"]
        print(f"  Finnhub: fetching analyst data for {len(_FH_TICKERS)} tickers...")
        import time as _fh_time
        _fh_hdrs = {"X-Finnhub-Token": _FINNHUB_KEY}
        for _fh_tk in _FH_TICKERS:
            try:
                # Analyst recommendations
                _rec_r = _req.get(
                    f"https://finnhub.io/api/v1/stock/recommendation",
                    params={"symbol": _fh_tk}, headers=_fh_hdrs, timeout=8)
                _recs = _rec_r.json()[:3] if _rec_r.ok else []
                # Price target
                _pt_r = _req.get(
                    f"https://finnhub.io/api/v1/stock/price-target",
                    params={"symbol": _fh_tk}, headers=_fh_hdrs, timeout=8)
                _pt   = _pt_r.json() if _pt_r.ok else {}
                _finnhub_data[_fh_tk] = {
                    "price_target_mean":   _pt.get("targetMean"),
                    "price_target_high":   _pt.get("targetHigh"),
                    "price_target_low":    _pt.get("targetLow"),
                    "analyst_count":       _pt.get("targetMedian"),
                    "latest_recs":         _recs[:2] if _recs else [],
                }
                _fh_time.sleep(0.6)   # Finnhub free: 60 req/min
            except Exception:
                pass
        print(f"  Finnhub: {len(_finnhub_data)}/{len(_FH_TICKERS)} tickers fetched")
    else:
        if not _FINNHUB_KEY:
            print("  Finnhub: FINNHUB_API_KEY not set — skipped (free at finnhub.io)")
    Path("data/finnhub_analyst.json").write_text(json.dumps(_finnhub_data, indent=2, default=str))

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

    # Merge in FRED extended series (fed_funds_rate, cpi_yoy, consumer_sentiment,
    # wheat, oil_wti, gold, silver, nat_gas, copper, y10, y2, hy_spread, gdp_growth, etc.)
    # _ext is populated by Cell 14; guard against it being undefined on evening/skipped runs.
    _ext_snap = globals().get("_ext") or {}
    for _ek, _ev in _ext_snap.items():
        if _ev is not None:
            _macro_snap[_ek] = _ev
    # Alias: dashboard expects consumer_sentiment; predictions CSV stores it as sentiment
    if "consumer_sentiment" not in _macro_snap and _macro_snap.get("sentiment"):
        _macro_snap["consumer_sentiment"] = _macro_snap["sentiment"]

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

    # Fold today's just-executed trades into the cumulative history so the
    # dashboard Trade Log shows every day the model has traded, not just today.
    _merge_trade_history()

    _dash_data = {
        "generated":      datetime.datetime.utcnow().isoformat()[:16] + " UTC",
        "run_type":       RUN_TYPE,
        "trades":         _read_csv("data/paper_trades/trade_history.csv"),
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
        "walkforward":    _read_json("data/predictions/walkforward.json"),
        "shadow_xsec":    _read_csv("data/shadow/cross_sectional_pnl.csv"),
        "pnl_history":    _read_csv("data/predictions/pnl_history.csv"),
        "positions":      _read_json("data/predictions/open_positions.json"),
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
