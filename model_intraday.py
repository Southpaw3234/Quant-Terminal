#!/usr/bin/env python3
"""
Intraday Model — Level 3 Phase 1
==================================
Separate training loop for a 2-hour resolution signal blended into the v25.1
ensemble at signal generation time (Cell 11).

Prediction target
-----------------
next_intraday_mom : the following day's open-to-close move (intraday_mom shifted
  back by 1 row in the history table).  This is a 24-hour-ahead prediction of
  whether tomorrow's intraday session will be bullish or bearish — distinct from
  v25.1's 5-day forward-return label and complementary to it.

Training data
-------------
Reads accumulated daily snapshots from data/intraday_history/YYYYMMDD.csv
(written every morning by CELL_6_POSTPATCH + _CELL_6_L3_HISTORY extension).
Minimum 30 rows per ticker before that ticker participates in training.

Model
-----
XGBoost + LightGBM with Huber regression objectives (matching v25.1 Tier 1).
EmbargoTimeSeriesSplit with 2-day embargo (appropriate for 1-day horizon).
Optuna tunes on the first 62.5% of each ticker's history.

Output
------
data/models/intraday_model.pkl     — trained models per ticker
data/predictions/intraday_signals.json — scores for current session
  {ticker: {score, signal, confidence, n_train_days}}

Run modes
---------
  RUN_TYPE=morning  : train + predict (full cycle)
  RUN_TYPE=intraday : predict-only using cached models
  RUN_TYPE=evening  : skip entirely
"""

import os
import sys
import json
import pickle
import datetime
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

RUN_TYPE = os.environ.get("RUN_TYPE", "morning").lower()
IS_GH    = bool(os.environ.get("GH_ACTIONS"))

print(f"\n{'='*55}")
print(f"INTRADAY MODEL — {RUN_TYPE.upper()} RUN")
print(f"Started: {datetime.datetime.utcnow():%Y-%m-%d %H:%M UTC}")
print(f"{'='*55}\n")

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR       = Path("data")
HIST_DIR       = DATA_DIR / "intraday_history"
MODEL_FILE     = DATA_DIR / "models" / "intraday_model.pkl"
SIGNALS_FILE   = DATA_DIR / "predictions" / "intraday_signals.json"
METRICS_FILE   = DATA_DIR / "predictions" / "intraday_metrics.json"

for _p in [HIST_DIR, DATA_DIR / "models", DATA_DIR / "predictions"]:
    _p.mkdir(parents=True, exist_ok=True)

# ── Evening: nothing to do ─────────────────────────────────────────────────────
if RUN_TYPE == "evening":
    print("Evening run — intraday model skipped.")
    sys.exit(0)

# ── Load history ───────────────────────────────────────────────────────────────
print("Loading intraday history snapshots...")
try:
    import pandas as pd
    import numpy as np
except ImportError as e:
    print(f"Missing dependency: {e}")
    sys.exit(1)

_csv_files = sorted(HIST_DIR.glob("*.csv"))
print(f"  Found {len(_csv_files)} daily snapshot files")

if len(_csv_files) < 5:
    print(f"  Insufficient history ({len(_csv_files)} days < 5 minimum). "
          f"Run morning cycles to accumulate data. Exiting.")
    sys.exit(0)

_frames = []
for _f in _csv_files:
    try:
        _df = pd.read_csv(_f)
        _frames.append(_df)
    except Exception as _e:
        print(f"  Warning: could not read {_f.name}: {_e}")

if not _frames:
    print("  No readable snapshots found. Exiting.")
    sys.exit(0)

history = pd.concat(_frames, ignore_index=True)
history["date"] = pd.to_datetime(history["date"], errors="coerce")
history = history.sort_values(["ticker", "date"]).reset_index(drop=True)
print(f"  History loaded: {len(history)} rows, "
      f"{history['ticker'].nunique()} tickers, "
      f"{history['date'].min().date()} → {history['date'].max().date()}")

# ── Feature columns ────────────────────────────────────────────────────────────
INTRADAY_FEATURES = [
    "intraday_mom", "overnight_gap", "vwap_dev",
    "intraday_range", "close_to_high",
    "attn_ret20", "attn_rsi20", "attn_vol20",
    "xs_mom_5d", "insider_net_buy", "patent_velocity",
]
# Only use columns that actually exist in the history
FEATURE_COLS = [c for c in INTRADAY_FEATURES if c in history.columns]
print(f"  Feature columns available: {len(FEATURE_COLS)} / {len(INTRADAY_FEATURES)}")
if len(FEATURE_COLS) < 3:
    print("  Too few features to train. Exiting.")
    sys.exit(0)

# ── Build label: next day's intraday_mom (shifted back 1 within each ticker) ──
# Label for day T = intraday_mom at day T+1.
# Intuition: predict whether tomorrow's session will close above open.
# This is orthogonal to v25.1's 5-day label — different horizon, same features.
if "intraday_mom" not in history.columns:
    print("  'intraday_mom' not in history — cannot build label. Exiting.")
    sys.exit(0)

history["next_intraday_mom"] = (
    history.groupby("ticker")["intraday_mom"].shift(-1)
)
# Binary label: 1 if next session is up, 0 if down
history["label"] = (history["next_intraday_mom"] > 0).astype(int)

# ── Training (morning only) ────────────────────────────────────────────────────
trained_models = {}
oos_metrics    = {}

MIN_ROWS      = 30
EMBARGO_DAYS  = 2
OPTUNA_FRAC   = 0.625
N_SPLITS      = 4
OPTUNA_TRIALS = 12 if IS_GH else 8

if RUN_TYPE == "morning":
    print(f"\nTraining intraday models (Optuna trials={OPTUNA_TRIALS})...")
    try:
        import xgboost  as xgb
        import lightgbm as lgb
        import optuna
        from sklearn.model_selection import TimeSeriesSplit as _BaseTSS
        from sklearn.preprocessing   import StandardScaler
        from sklearn.metrics          import roc_auc_score

        optuna.logging.set_verbosity(optuna.logging.WARNING)

    except ImportError as _ie:
        print(f"  Missing ML dependency: {_ie}")
        sys.exit(1)

    # ── EmbargoTimeSeriesSplit (mirrors v25.1 CELL_8_PREPATCH) ────────────────
    class EmbargoTSS:
        def __init__(self, n_splits=4, embargo=2, optuna_frac=0.625):
            self._n    = n_splits
            self._emb  = embargo
            self._frac = optuna_frac

        def split(self, X):
            n      = len(X)
            n_tune = max(int(n * self._frac), self._n * 8)
            inner  = _BaseTSS(n_splits=self._n)
            for tr, te in inner.split(X[:n_tune]):
                if len(tr) == 0:
                    continue
                emb_end = int(tr[-1]) + self._emb
                te_emb  = te[te > emb_end]
                if len(te_emb) >= 3:
                    yield tr, te_emb

        def val_idx(self, n):
            s = int(n * self._frac)
            e = int(n * (self._frac + 0.125))
            return np.arange(s, min(e, n))

        def test_idx(self, n):
            s = int(n * (self._frac + 0.125))
            e = int(n * (self._frac + 0.25))
            return np.arange(s, min(e, n))

    tss = EmbargoTSS(n_splits=N_SPLITS, embargo=EMBARGO_DAYS,
                     optuna_frac=OPTUNA_FRAC)

    tickers = history["ticker"].unique()
    print(f"  Tickers with history: {len(tickers)}")

    n_trained = 0
    n_skipped = 0

    for tk in tickers:
        try:
            _df = (history[history["ticker"] == tk]
                   .dropna(subset=FEATURE_COLS + ["label"])
                   .sort_values("date")
                   .reset_index(drop=True))

            if len(_df) < MIN_ROWS:
                n_skipped += 1
                continue

            X = _df[FEATURE_COLS].values.astype(float)
            y = _df["label"].values.astype(int)

            # Impute any remaining NaN with column median
            for _ci in range(X.shape[1]):
                _col = X[:, _ci]
                _nan = ~np.isfinite(_col)
                if _nan.any():
                    _med = np.nanmedian(_col)
                    X[_nan, _ci] = _med if np.isfinite(_med) else 0.0

            scaler = StandardScaler()
            X_sc   = scaler.fit_transform(X)

            # Hold out last 25% for OOS eval — never seen during Optuna
            _test_i = tss.test_idx(len(X))
            if len(_test_i) < 5:
                _test_i = np.arange(max(0, len(X) - 10), len(X))
            _train_i = np.arange(0, _test_i[0])
            X_tr, y_tr = X_sc[_train_i], y[_train_i]
            X_te, y_te = X_sc[_test_i],  y[_test_i]

            if len(np.unique(y_tr)) < 2:
                n_skipped += 1
                continue

            # ── Optuna XGB objective ──────────────────────────────────────────
            def _xgb_objective(trial):
                _p = {
                    "n_estimators":     trial.suggest_int("n_est", 50, 300),
                    "max_depth":        trial.suggest_int("depth", 2, 6),
                    "learning_rate":    trial.suggest_float("lr", 0.01, 0.2, log=True),
                    "subsample":        trial.suggest_float("sub", 0.5, 1.0),
                    "colsample_bytree": trial.suggest_float("col", 0.5, 1.0),
                    "reg_alpha":        trial.suggest_float("a", 1e-4, 10.0, log=True),
                    "reg_lambda":       trial.suggest_float("l", 1e-4, 10.0, log=True),
                    "objective":        "binary:logistic",
                    "eval_metric":      "auc",
                    "verbosity":        0,
                    "use_label_encoder": False,
                }
                _scores = []
                for _tr_i, _va_i in tss.split(X_tr):
                    if len(np.unique(y_tr[_va_i])) < 2:
                        continue
                    _m = xgb.XGBClassifier(**_p, random_state=42)
                    _m.fit(X_tr[_tr_i], y_tr[_tr_i],
                           eval_set=[(X_tr[_va_i], y_tr[_va_i])],
                           verbose=False)
                    _prob = _m.predict_proba(X_tr[_va_i])[:, 1]
                    _scores.append(roc_auc_score(y_tr[_va_i], _prob))
                return float(np.mean(_scores)) if _scores else 0.5

            _study_xgb = optuna.create_study(direction="maximize",
                                              sampler=optuna.samplers.TPESampler(seed=42))
            _study_xgb.optimize(_xgb_objective, n_trials=OPTUNA_TRIALS,
                                timeout=60, show_progress_bar=False)
            _best_xgb = _study_xgb.best_params
            _best_xgb.update({"objective": "binary:logistic", "eval_metric": "auc",
                               "verbosity": 0, "use_label_encoder": False,
                               "n_estimators": _best_xgb.pop("n_est", 100),
                               "max_depth":    _best_xgb.pop("depth", 3)})
            model_xgb = xgb.XGBClassifier(**_best_xgb, random_state=42)
            model_xgb.fit(X_tr, y_tr)

            # ── LGB ──────────────────────────────────────────────────────────
            def _lgb_objective(trial):
                _p = {
                    "n_estimators":   trial.suggest_int("n_est", 50, 300),
                    "num_leaves":     trial.suggest_int("leaves", 8, 64),
                    "learning_rate":  trial.suggest_float("lr", 0.01, 0.2, log=True),
                    "feature_fraction": trial.suggest_float("ff", 0.5, 1.0),
                    "bagging_fraction": trial.suggest_float("bf", 0.5, 1.0),
                    "bagging_freq":   1,
                    "reg_alpha":      trial.suggest_float("a", 1e-4, 10.0, log=True),
                    "reg_lambda":     trial.suggest_float("l", 1e-4, 10.0, log=True),
                    "objective":      "binary",
                    "metric":         "auc",
                    "verbosity":      -1,
                }
                _scores = []
                for _tr_i, _va_i in tss.split(X_tr):
                    if len(np.unique(y_tr[_va_i])) < 2:
                        continue
                    _m = lgb.LGBMClassifier(**_p, random_state=42)
                    _m.fit(X_tr[_tr_i], y_tr[_tr_i],
                           eval_set=[(X_tr[_va_i], y_tr[_va_i])])
                    _prob = _m.predict_proba(X_tr[_va_i])[:, 1]
                    _scores.append(roc_auc_score(y_tr[_va_i], _prob))
                return float(np.mean(_scores)) if _scores else 0.5

            _study_lgb = optuna.create_study(direction="maximize",
                                              sampler=optuna.samplers.TPESampler(seed=42))
            _study_lgb.optimize(_lgb_objective, n_trials=OPTUNA_TRIALS,
                                timeout=60, show_progress_bar=False)
            _best_lgb = _study_lgb.best_params
            _best_lgb.update({"objective": "binary", "metric": "auc", "verbosity": -1,
                               "n_estimators":   _best_lgb.pop("n_est", 100),
                               "num_leaves":     _best_lgb.pop("leaves", 16),
                               "bagging_freq":   1})
            model_lgb = lgb.LGBMClassifier(**_best_lgb, random_state=42)
            model_lgb.fit(X_tr, y_tr)

            # ── OOS evaluation ────────────────────────────────────────────────
            _oos_metrics = {}
            if len(np.unique(y_te)) >= 2:
                _prob_xgb = model_xgb.predict_proba(X_te)[:, 1]
                _prob_lgb = model_lgb.predict_proba(X_te)[:, 1]
                _prob_ens = 0.5 * _prob_xgb + 0.5 * _prob_lgb
                _auc      = roc_auc_score(y_te, _prob_ens)
                # Spearman IC between score and next_intraday_mom
                _true_ret = _df["next_intraday_mom"].iloc[_test_i].values
                _valid     = np.isfinite(_true_ret)
                if _valid.sum() >= 5:
                    from scipy.stats import spearmanr
                    _ic, _ = spearmanr(_prob_ens[_valid], _true_ret[_valid])
                else:
                    _ic = 0.0
                _oos_metrics = {
                    "auc":       round(float(_auc), 4),
                    "ic":        round(float(_ic),  4),
                    "n_oos":     int(len(y_te)),
                    "n_train":   int(len(y_tr)),
                }
            else:
                _oos_metrics = {"auc": 0.5, "ic": 0.0,
                                "n_oos": len(y_te), "n_train": len(y_tr)}

            trained_models[tk] = {
                "xgb":    model_xgb,
                "lgb":    model_lgb,
                "scaler": scaler,
                "feature_cols": FEATURE_COLS,
                "trained_on": datetime.datetime.utcnow().isoformat(),
                "n_days": len(_df),
            }
            oos_metrics[tk] = _oos_metrics
            n_trained += 1

        except Exception as _train_e:
            print(f"  [{tk}] training error: {_train_e}")
            n_skipped += 1

    print(f"\nTraining complete: {n_trained} models trained, {n_skipped} skipped")
    if oos_metrics:
        _aucs = [v["auc"] for v in oos_metrics.values() if "auc" in v]
        _ics  = [v["ic"]  for v in oos_metrics.values() if "ic"  in v]
        print(f"  OOS AUC  mean={np.mean(_aucs):.3f}  median={np.median(_aucs):.3f}")
        print(f"  OOS IC   mean={np.mean(_ics):.4f}  median={np.median(_ics):.4f}")

    # Save model cache
    if trained_models:
        MODEL_FILE.parent.mkdir(exist_ok=True)
        MODEL_FILE.write_bytes(pickle.dumps(
            {"models": trained_models, "feature_cols": FEATURE_COLS,
             "trained": datetime.datetime.utcnow().isoformat()},
            protocol=4))
        METRICS_FILE.write_text(json.dumps(oos_metrics, indent=2))
        print(f"  Models saved → {MODEL_FILE}")

else:
    # Intraday run: load cached models
    print("Intraday run — loading cached models...")
    if MODEL_FILE.exists():
        try:
            _cache = pickle.loads(MODEL_FILE.read_bytes())
            trained_models = _cache["models"]
            FEATURE_COLS   = _cache.get("feature_cols", FEATURE_COLS)
            print(f"  Loaded {len(trained_models)} models "
                  f"(trained {_cache.get('trained','?')[:16]})")
        except Exception as _le:
            print(f"  Model cache load failed: {_le}")
    else:
        print("  No model cache found — cannot predict. Run morning cycle first.")
        sys.exit(0)

# ── Generate signals for current session ──────────────────────────────────────
print("\nGenerating intraday signals...")
import numpy as np

signals = {}
if trained_models:
    # Use most recent snapshot row per ticker as live features
    _latest = (history
               .sort_values("date")
               .groupby("ticker")
               .tail(1)
               .set_index("ticker"))

    for tk, mdl in trained_models.items():
        try:
            if tk not in _latest.index:
                continue
            _row  = _latest.loc[tk]
            _fcols = mdl.get("feature_cols", FEATURE_COLS)
            _x    = np.array([float(_row.get(c, 0.0) or 0.0) for c in _fcols],
                              dtype=float)
            _x    = np.where(np.isfinite(_x), _x, 0.0).reshape(1, -1)
            _xs   = mdl["scaler"].transform(_x)

            _p_xgb = float(mdl["xgb"].predict_proba(_xs)[0, 1])
            _p_lgb = float(mdl["lgb"].predict_proba(_xs)[0, 1])
            _score = round(0.5 * _p_xgb + 0.5 * _p_lgb, 4)

            _signal = "BUY" if _score >= 0.55 else "SELL" if _score <= 0.45 else "HOLD"
            _conf   = round(abs(_score - 0.5) * 2, 4)  # 0 at 0.5, 1 at 0 or 1

            # DSR penalty from main model if available
            _dsr_pen = mdl.get("dsr_penalty", 1.0)
            if _dsr_pen < 1.0:
                _score  = round(0.5 + (_score - 0.5) * _dsr_pen, 4)
                _signal = "HOLD" if abs(_score - 0.5) < 0.05 else _signal

            signals[tk] = {
                "intraday_score": _score,
                "signal":         _signal,
                "confidence":     _conf,
                "n_train_days":   mdl.get("n_days", 0),
                "generated":      datetime.datetime.utcnow().isoformat()[:16],
            }
        except Exception as _sig_e:
            pass

    SIGNALS_FILE.write_text(json.dumps(signals, indent=2))

    _buys  = sum(1 for v in signals.values() if v["signal"] == "BUY")
    _sells = sum(1 for v in signals.values() if v["signal"] == "SELL")
    _holds = sum(1 for v in signals.values() if v["signal"] == "HOLD")
    print(f"  Signals: BUY={_buys}  HOLD={_holds}  SELL={_sells}  "
          f"total={len(signals)}")
    print(f"  Saved → {SIGNALS_FILE}")
else:
    print("  No trained models available — no signals generated.")

print(f"\nIntraday model complete: {datetime.datetime.utcnow():%H:%M UTC}\n")
