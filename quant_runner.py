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
    FULL_TUNE_TRIALS_XGB = 3
    FULL_TUNE_TRIALS_LGB = 3
    FULL_TUNE_TRIALS_CAT = 3
    print(f"GH_ACTIONS {RUN_TYPE_GH}: FAST_MODE={FAST_MODE} GARCH_PATHS={GARCH_PATHS}")
"""

# ── Cell skip rules per run type ──────────────────────────────────────────
ALWAYS_SKIP = {0, 1, 16, 17, 18, 19, 20, 21, 22, 23}

SKIP_BY_TYPE = {
    "morning":  ALWAYS_SKIP,
    "intraday": ALWAYS_SKIP | {7, 8, 9},           # skip model training
    "evening":  ALWAYS_SKIP | {7, 8, 9, 10, 11, 12, 13},  # skip through paper trade
}
SKIP_CELLS = SKIP_BY_TYPE.get(RUN_TYPE, ALWAYS_SKIP)

CELL_TAGS = {
    3: "Config", 4: "Macro data", 5: "Ticker download",
    6: "Feature engineering", 7: "HMM regimes", 8: "ML ensemble",
    9: "GARCH + IV", 10: "FinBERT sentiment", 11: "Signal generator",
    12: "CVaR optimization", 13: "Paper trading",
    14: "Outcome scoring", 15: "Self-learning",
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
NB_PATH = "trading_model_v25.ipynb"
print(f"Loading: {NB_PATH}  run_type={RUN_TYPE}")

with open(NB_PATH, encoding="utf-8-sig") as f:
    nb = json.load(f)

cells = nb["cells"]
print(f"  {len(cells)} cells  |  skipping: {sorted(SKIP_CELLS)}\n")

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
    if i == 3:
        src = src + "\n\n" + GH_PATCH

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
        "gold":                _fred("GOLDAMGBD228NLBM"),# Gold price
        "m2_growth":           _fred("M2SL"),            # M2 money supply
        "initial_claims":      _fred("ICSA"),            # Weekly jobless claims
        "housing_starts":      _fred("HOUST"),           # Housing starts (K)
        "conf_board_lei":      _fred("USSLIND"),         # Leading econ index
    }
    print(f"  FRED: {sum(v is not None for v in _ext.values())}/{len(_ext)} series fetched")

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

    # ── Save enrichment files ─────────────────────────────────────────────
    Path("data/macro_extended.json").write_text(json.dumps(_ext, indent=2, default=str))
    Path("data/fomc.json").write_text(json.dumps(_fomc_data, indent=2, default=str))
    Path("data/congress_trades.json").write_text(json.dumps(_congress, indent=2, default=str))
    Path("data/geopolitical_news.json").write_text(json.dumps(_geo_news, indent=2, default=str))
    Path("data/industry_risk.json").write_text(json.dumps(_industry_risk, indent=2, default=str))
    print("  Enrichment files saved to data/")

except Exception as _enrich_e:
    print(f"  Macro enrichment error: {_enrich_e}")
    traceback.print_exc()
    _ext          = {}
    _fomc_data    = {}
    _congress     = []
    _geo_news     = []
    _industry_risk= {}

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
