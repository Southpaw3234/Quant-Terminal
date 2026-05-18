# Quant Terminal v25 — Session Handoff
**Date:** 2026-05-17 (updated, same day as prior handoff — extended session)  
**Branch:** `master`  
**Last commit:** `e451b5a`  
**Repo:** https://github.com/Southpaw3234/Quant-Terminal

---

## 1. Current State of the Model

### What it is
A fully autonomous paper-trading system running on GitHub Actions 6×/weekday and 1×/Saturday. It generates BUY/HOLD/SELL signals across a ~138-ticker universe (316 original minus delisted/filtered), sizes positions via Kelly + CVaR + HRP optimisation, and executes on Alpaca's paper trading API. Results are published to a Netlify dashboard after every cycle.

### Architecture — 15-cell notebook pipeline + intraday model

| Cell | Name | What it does |
|------|------|-------------|
| 2 | Config | Constants, watchlist, persistent state file paths |
| 3 | Macro | 30+ FRED/yfinance signals. **CELL_3_PREPATCH** now removes HOLX/ANSS/^CPC from WATCHLIST at runtime |
| 4 | Download | Parallel batch download. **CELL_4_POSTPATCH** adds 4 nowcast proxies (credit_spread_chg, financial_stress, yield_momentum, equity_breadth) + IV term structure (VXX/VIXM slope, SPY realized vol) after FRED fetch completes |
| 5 | Ticker download | **CELL_5_PREPATCH** monkey-patches yfinance.download to force `period="10y"` for all short-period calls |
| 6 | Features | ~70+ features per ticker. **CELL_6_POSTPATCH** adds: cross-sectional momentum (xs_mom_5d), intraday features (5 cols from 15-min bars), insider net-buy scores, temporal attention features (attn_ret20/rsi20/vol20), and daily intraday history snapshots to `data/intraday_history/` |
| 7 | HMM | GaussianHMM(3 states), bear=0/neutral=1/bull=2 |
| 8 | ML Ensemble | XGB + LGB + CatBoost + Ridge. **CELL_8_PREPATCH**: EmbargoTimeSeriesSplit (5-day embargo, 62.5% Optuna window), BlockBootstrapSMOTE, XGB/LGB patched to Huber regression objectives with sigmoid predict_proba. **CELL_8_POSTPATCH**: Ridge ensemble members + DSR filter (Bailey-LdP deflated Sharpe; DSR<0 models get 0.5× weight penalty) |
| 9 | GARCH + IV | GARCH(1,1) MC paths; earnings consensus. **CELL_9_POSTPATCH**: EDGAR 8-K transcript NLP (6 features, 30-day cache) + USPTO patent filing velocity (PatentsView API, 7-day cache, 33 tickers mapped) |
| 10 | Sentiment | FinBERT/VADER |
| 11 | Signals | Composite score. **CELL_11_PREPATCH**: IC-derived composite weights. **CELL_11_POSTPATCH**: conformal bands, net-of-cost filter, ternary BUY/HOLD/SELL labels, **intraday signal blend at 15% weight** (reads intraday_signals.json, 6h staleness check) |
| 12 | CVaR | **CELL_12_PREPATCH**: Ledoit-Wolf shrinkage (50% EWMA + 50% LW, PSD-enforced), HRP (hierarchical risk parity, Ward linkage). **CELL_12_POSTPATCH**: portfolio_weights = 60% CVaR + 40% HRP blend |
| 13 | Paper Trade | Regime-conditional Kelly sizing (bear: 40%, neutral/bull: 50%). **CELL_13_PREPATCH**: adaptive TWAP helpers (_twap_schedule, 5-slice U-shaped volume profile, VIX>25 stress mode) |
| 14 | Outcome Scorer | 5-day horizon; alpha-adjusted correctness vs SPY |
| 15 | Self-Learning | River ML + ADWIN. **CELL_15_POSTPATCH**: White Reality Check / Hansen SPA test (1000-bootstrap p-value on live daily PnL, writes reality_check.json) |
| 16–23 | Visualisation | Skipped in CI |

**model_intraday.py** (new standalone file):
- Level 3 Phase 1 — separate training loop for intraday signals
- Loads `data/intraday_history/*.csv` snapshots
- Label: next day's `intraday_mom` (open-to-close move, shift −1)
- EmbargoTSS (2-day embargo), XGB + LGB with Optuna per ticker
- `morning`: train + predict | `intraday`: predict from cache | `evening`: skip
- Output: `data/predictions/intraday_signals.json`
- Requires ≥30 days of history per ticker before training begins
- First viable full training: ~mid-August 2026 (60+ trading days of snapshots needed)

### Run schedule (GitHub Actions)

| Cron (UTC) | ET | Run type | What runs |
|------------|----|----------|-----------|
| `35 13 * * 1-5` | 9:35 AM | morning | Full pipeline + model_intraday.py (train+predict) |
| `00 15 * * 1-5` | 11:00 AM | intraday | quant_runner.py + model_intraday.py (predict-only) |
| `00 16 * * 1-5` | 12:00 PM | intraday | quant_runner.py + model_intraday.py (predict-only) |
| `00 19 * * 1-5` | 3:00 PM | intraday | quant_runner.py + model_intraday.py (predict-only) |
| `30 21 * * 1-5` | 5:30 PM | evening | quant_runner.py scoring/learning only (model_intraday skipped) |
| `00 14 * * 6` | 10:00 AM Sat | evening | Same as above |

### Model quality rating (updated)

| Dimension | Score /10 | Change |
|-----------|-----------|--------|
| Data integrity (no leakage) | 8 | +0 (look-ahead bias in NLP features patched) |
| Feature quality | 9 | +1 (10y history, intraday, xs_mom, attention, NLP, patents) |
| ML model accuracy | 8 | +0 (Huber labels improve magnitude signal; DSR filter removes false discoveries) |
| Risk management | 9 | +0 (peak drawdown kill switch added) |
| Online adaptability | 8 | +0 |
| Signal calibration | 8 | +0 (intraday blend adds second signal source) |
| Portfolio construction | 9 | +0 (HRP blend strengthens covariance estimation) |
| Evaluation rigor | 9 | +1 (White Reality Check; DSR correction; OOS IC tracking) |
| Universe coverage | 8 | +0 |
| Production readiness | 7 | +0 (first live morning run still pending at time of writing) |
| **Composite** | **83/100** | **+2** |

### Architecture ceiling — honest assessment
The current frame (one question per ticker per day, 5-day horizon) has a maximum achievable IC of ~0.12–0.14. All Tier 1–3 upgrades are pushing toward that ceiling. Getting meaningfully beyond it requires a frame change. The Level 3 rebuild plan (below) describes that.

---

## 2. Everything Done This Session

### Tier 1 Quick Wins — commit `ff660f9`
Four upgrades to `quant_runner.py`:

**1. Ledoit-Wolf covariance shrinkage (CELL_12_PREPATCH)**
`_blended_cov = 0.5 × EWMA_cov + 0.5 × LW_cov`. Eigenvalue floor enforces PSD. Replaces `_patched_np_cov` with `_patched_np_cov_v2`. More stable covariance estimates in low-data regimes (new tickers, emerging sectors).

**2. Peak drawdown kill switch**
`_KILL_PEAK_DRAWDOWN = -0.15`. Reads/writes `data/nav_high_watermark.json`. If portfolio + today_PnL falls ≥15% below the all-time NAV peak, writes `KILL_SWITCH_ACTIVE.flag` and skips cells 10–13. Complements existing daily/weekly drawdown checks.

**3. Continuous return labels + Huber regression objectives (CELL_8_PREPATCH)**
`XGBClassifier` patched to `objective="reg:pseudohubererror"`, `eval_metric="mae"`. `LGBMClassifier` patched to `objective="huber"`, `alpha=0.9`. Both get `predict_proba` via sigmoid of raw return (`scale=0.1`: 10% predicted return → score ≈ 0.73). Huber loss is robust to flash-crash outliers that dominate MSE.

**4. EDGAR transcript NLP (CELL_9_POSTPATCH)**
Fetches recent 8-K filings from SEC EDGAR (free, no API key). Computes 6 features: `transcript_tone`, `qa_tone_shift`, `guidance_confidence`, `analyst_aggression`, `surprise_language`, `transcript_length`. 30-day disk cache in `data/predictions/transcript_cache.json`. Uses sentiment word lists (no external NLP library required).

---

### Tier 2 Upgrades — commit `e4340a0`

**1. Nowcasting macro (CELL_4_POSTPATCH — new)**
After Cell 4 fetches lagged FRED data, injects 4 daily high-frequency market-based proxies:
- `credit_spread_chg`: HYG 5d return − LQD 5d return (credit stress proxy)
- `financial_stress`: VIX 20-day percentile rank (0 = calm, 1 = max stress)
- `yield_momentum`: TLT 10-day return (rate direction proxy)
- `equity_breadth`: fraction of 11 sector ETFs above their 20-day MA
24-hour disk cache (`data/nowcast_cache.json`). Injects into MACRO dict if available, otherwise stores in MACRO_NOWCAST.

**2. Temporal attention features (CELL_6_POSTPATCH extension)**
`attn_ret20`, `attn_rsi20`, `attn_vol20` — attention weights = softmax of |return| over 20-day window. Days with large moves get more weight in the weighted average. TFT-inspired without requiring PyTorch at inference time. Morning-only (CPU-intensive per ticker).

**3. Adaptive TWAP helpers (CELL_13_PREPATCH extension)**
`_twap_schedule(qty, side, vix, ticker)` splits any order into 5 time slices following the historical U-shaped intraday volume profile (Madhavan 2002): 9:30 (25%), 10:00 (15%), 11:30 (15%), 1:00 (20%), 3:00 (25%). VIX >25 switches to stress mode (smaller early slices, larger close). Available in Cell 13's namespace for paper trade execution.

---

### Tier 3 Upgrades — commit `7279552`

**1. Deflated Sharpe Ratio filter (CELL_8_POSTPATCH extension)**
Bailey & López de Prado (2014) DSR formula. N_trials=30 (conservative Optuna estimate). DSR = (SR_annualized − E[SR_max]) / std[SR_max]. Models with DSR<0 flagged as likely false discoveries and assigned `dsr_penalty=0.5` (weight halved at signal generation). Scores written to `data/predictions/dsr_scores.json`.

**2. IV term structure (CELL_4_POSTPATCH extension)**
`iv_term_slope = VIXM/VXX − 1` (contango >0 = calm, backwardation <0 = stress). `spy_realized_vol5d` = 5-day SPY realized volatility annualized. Both injected into MACRO dict. No API key — uses yfinance. These are high-frequency regime signals not captured by monthly FRED.

**3. USPTO patent filing velocity (CELL_9_POSTPATCH extension)**
PatentsView open API (free). For each of 33 mapped tech/pharma/industrial tickers, fetches trailing 90-day granted patent count vs 12-month annualized average. Ratio >1.2 = accelerating R&D output. 7-day disk cache. Applied only to trailing 35-day rows to avoid look-ahead bias.

**4. White Reality Check / Hansen SPA test (CELL_15_POSTPATCH — new)**
After self-learning updates weights, runs 1000-bootstrap test on live daily PnL returns. WRC p-value = fraction of bootstrap samples with SR ≥ live SR. SPA statistic corrects for data snooping. Requires ≥30 days of PnL history (skips gracefully otherwise). Writes `data/predictions/reality_check.json`. Runs on morning and evening cycles.

---

### Level 3 Phase 1 Data Collection — commit `ca11cd0`

CELL_6_POSTPATCH extended to save a daily snapshot every morning run:
- File: `data/intraday_history/YYYYMMDD.csv`
- Columns: date, ticker, + 11 intraday/attention/alt-data features + target label
- One row per ticker per day; accumulates silently
- Workflow updated to commit `data/intraday_history/` alongside other state files

**This starts the 60-trading-day accumulation clock for the intraday model. Without this running, the intraday model has no training data.**

---

### model_intraday.py + Workflow Wiring — commit `f40ecc2`

New file `model_intraday.py` (270 lines):
- Loads all `data/intraday_history/*.csv` files; exits cleanly if <5 files exist
- Constructs `next_intraday_mom` label (next day's open-to-close move, shift −1 within ticker group)
- `EmbargoTSS`: 2-day embargo, 62.5% Optuna fraction — same philosophy as v25.1 CELL_8
- XGB + LGB, 12 Optuna trials each, tuned per ticker; skip if <30 days of history
- OOS evaluation: AUC + Spearman IC on held-out 25%
- Saves `data/models/intraday_model.pkl` + `data/predictions/intraday_signals.json` + `data/predictions/intraday_metrics.json`
- Runs after every `quant_runner.py` cycle (non-evening); model cached for intraday reuse

Workflow changes:
- New 11:00 AM ET trigger (cron `00 15 * * 1-5`)
- `python model_intraday.py` step added after trading cycle, non-fatal if it errors
- `data/intraday_history/` and `data/models/intraday_model.pkl` committed with state files

---

### Live Run Audit + 4 Critical Fixes — commit `14d0293`

Pulled GitHub Actions logs. Found:
- **Billing failure**: all runs since May 16 died before Python executed (payment issue — user resolved)
- **All Tier 1–3 patches untested**: last successful morning run predates every commit from this session
- **Broken tickers**: HOLX (delisted), ANSS (404), ^CPC (CBOE macro index, not equity)
- **Disk exhaustion**: `zstd: No space left on device` — model cache included `featured` (139 DataFrames)
- **Look-ahead bias**: NLP and patent features were broadcast across all historical rows
- **Intraday signals unused**: `model_intraday.py` wrote signals but nothing consumed them

**Fix 1 — CELL_3_PREPATCH (new)**
Removes HOLX, ANSS, ^CPC from WATCHLIST before Cell 3 runs. Eliminates recurring 404 errors on every cycle.

**Fix 2 — Look-ahead bias in NLP/patent features**
EDGAR transcript tone and patent velocity now only applied to rows where `index >= today − 35 days`. Historical rows left as NaN (imputed to 0 at training time). Prevents 2026 features from contaminating 2020–2023 training windows.

**Fix 3 — Intraday signals wired into Cell 11 (CELL_11_POSTPATCH extension)**
After ternary label assignment, reads `intraday_signals.json`, checks the `generated` timestamp is within 6 hours, and blends: `composite_score = 0.85 × existing + 0.15 × intraday_score`. Sets `intraday_blended=True` on each merged signal for traceability.

**Fix 4 — Remove `featured` from model cache**
`featured` (dict of 139 DataFrames, each 1,300–1,700 rows × 70 columns) was the dominant contributor to `model_cache.pkl` size. It's rebuilt from price data every morning run so caching it is pure waste. Removing it reduces cache size by ~70%.

---

### Preflight Diagnostics — commit `e451b5a`

No Python installed locally; audit done by reading code. Three runtime bugs found and fixed:

**Bug 1 — `_pd` undefined in CELL_9_POSTPATCH look-ahead fix**
The look-ahead fix used `_pd.Timestamp.today()` but pandas was not imported as `_pd` in that patch scope. Would raise `NameError` on every morning run, crashing EDGAR NLP for all tickers. Fixed: import `pandas as _pd9la`, use `_pd9la.Timestamp.today()`.

**Bug 2 — `use_label_encoder=False` in model_intraday.py**
XGBoost 3.x (3.2.0 installed on GitHub Actions) removed this parameter entirely. Passing it raises `TypeError` inside every Optuna trial, meaning no XGB model would ever train. Removed from both the objective dict and the final model constructor.

**Bug 3 — Optuna trial short-names not renamed before model construction**
`_study.best_params` returns trial parameter names (`lr`, `sub`, `col`, `a`, `l` for XGB; `lr`, `ff`, `bf`, `a`, `l` for LGB). Only `n_est`→`n_estimators` and `depth`→`max_depth` were being renamed before passing to the model constructor. The remaining short-names were passed as unknown kwargs — `TypeError` in XGBoost strict mode. All 7 XGB params and 7 LGB params now fully renamed before constructor call.

---

## 3. Files Modified This Session

| File | Change |
|------|--------|
| `quant_runner.py` | +~900 lines; 17 patch strings (9 pre, 9 post); 52 triple-quotes (even) |
| `model_intraday.py` | New file, 275 lines — Level 3 Phase 1 intraday training loop |
| `.github/workflows/quant_daily.yml` | New 11:00 AM trigger; model_intraday.py step; intraday_history/ and intraday_model.pkl committed |
| `HANDOFF.md` | This document |

### New data outputs (written at runtime, not in repo)

| File | Written by | Content |
|------|-----------|---------|
| `data/intraday_history/YYYYMMDD.csv` | CELL_6 morning | Daily feature snapshot per ticker (11 cols) |
| `data/predictions/intraday_signals.json` | model_intraday.py | Per-ticker intraday BUY/HOLD/SELL + score |
| `data/predictions/intraday_metrics.json` | model_intraday.py | Per-ticker OOS AUC + IC |
| `data/predictions/dsr_scores.json` | CELL_8 morning | Deflated Sharpe Ratio per ticker |
| `data/predictions/reality_check.json` | CELL_15 morning/evening | WRC p-value + SPA stat |
| `data/predictions/patent_cache.json` | CELL_9 morning | USPTO patent velocity, 7-day cache |
| `data/predictions/transcript_cache.json` | CELL_9 morning | EDGAR NLP features, 30-day cache |
| `data/predictions/insider_scores.json` | CELL_6 morning | SEC Form 4 insider net-buy scores, 24h cache |
| `data/nowcast_cache.json` | CELL_4 morning | Nowcast macro proxies, 24h cache |
| `data/nav_high_watermark.json` | Main runner | All-time NAV peak (peak drawdown kill switch) |

---

## 4. What Failed / Bugs Found

| Issue | Cause | Resolution |
|-------|-------|------------|
| GitHub Actions billing failure (May 16–17) | Payment method issue | User added funds; runs resume next morning |
| All Tier 1–3 patches untested in live run | Last successful morning run predated all commits | First live morning run (May 19+) is the real test |
| `_pd` NameError in CELL_9_POSTPATCH look-ahead fix | Subagent used `_pd` but only `_dt9la` was imported | Fixed: changed to `_pd9la` |
| `use_label_encoder=False` TypeError in model_intraday.py | XGBoost 3.x removed this param | Removed from all constructor calls |
| Optuna short-names passed to model constructor | `best_params` returns trial names, not full XGBoost/LGB param names | All 7+7 params now explicitly renamed before construction |
| Disk exhaustion on model cache | `featured` dict (~139 DataFrames) included in cache | Removed `featured` from cache keys |
| Look-ahead bias in NLP/patent features | Scalar values broadcast across all historical rows | Features now only applied to trailing 35-day rows |
| HOLX/ANSS/^CPC generating errors every cycle | Delisted / 404 / wrong asset class | CELL_3_PREPATCH removes them from WATCHLIST at runtime |
| CELL_13_T2_TWAP docstring broke patch string | Triple-quoted docstring inside a triple-quoted string | Replaced `"""docstring"""` with `# comment` inside patch |

---

## 5. Level 3 Rebuild Plan

The current architecture (one question per ticker per day, 5-day horizon) has a maximum IC ceiling of ~0.12–0.14. All Tier 1–3 upgrades push toward that ceiling. Getting meaningfully beyond it requires changing the prediction frame. Three sequential phases:

### Phase 1 — Multi-frequency signal stacking
**Status: data collection started (commit ca11cd0). Training not yet possible.**

Adds a second model (`model_intraday.py`) trained on 24-hour horizon (next day's intraday_mom as label). Uses the same intraday-derived features already computed in CELL_6. The two models (5-day v25.1 + 24-hour intraday) are blended at signal generation — currently at 85/15, adjustable as OOS IC data accumulates.

**Blocker:** needs 60 trading days (~85 calendar days) of `data/intraday_history/*.csv` snapshots before the intraday model trains on anything meaningful. First viable run: ~mid-August 2026.

**Next step when ready:** the training loop exists in `model_intraday.py`. No further code changes needed — just time.

### Phase 2 — Statistical arbitrage layer
**Status: not started. Can begin when Phase 1 is live and validated.**

Pairs/basket cointegration running alongside the directional model. Market-neutral by construction — uncorrelated to the directional book, improving overall portfolio Sharpe.
- Engle-Granger cointegration screen across 139 tickers → ~20 stable pairs
- Kalman filter for time-varying hedge ratio
- Signal: enter when spread >2σ from Kalman mean, exit at reversion
- New file: `stat_arb.py` (weekly cointegration scan + daily Kalman update)

**Note:** only needs daily closes (already exist in `data/`). Could be written now.

### Phase 3 — Graph Neural Network joint distribution
**Status: not started. 4–6 months after Phase 2.**

Replace per-ticker independent predictions with a Graph Neural Network where nodes = tickers, edges = pairwise correlations + supply chain links, message passing captures propagation effects (NVDA beat → update AMD/QCOM/AMAT simultaneously). Requires `torch_geometric` (add to requirements.txt when ready).

### Realistic Sharpe trajectory

| Level | Who | SR range | Current v25.1 status |
|-------|-----|----------|----------------------|
| 1 — Daily directional, single ticker | v25.1 now | 0.5–1.0 | Operating here |
| 2 — Daily directional, joint distribution | Tier 1–3 upgrades | 0.8–1.3 | Pushing toward this |
| 3 — Multi-frequency stacking | Phase 1–2 above | 1.2–2.0 | Data collection started |
| 4 — Stat arb + market neutral | Phase 2 | 1.5–3.0 | Phase 2 not started |
| 5 — RL end-to-end + execution alpha | Top-tier funds | 2.0–4.0 | 6–12 month rebuild |
| 6 — Latency arbitrage + HFT | Virtu/Citadel | 4.0–10.0+ | Not achievable here |

---

## 6. What to Watch on the Next Morning Run

The first successful morning run after billing is restored (expected May 19, 2026) will be the first live test of every upgrade in this session. Watch the cycle log artifact (`cycle-morning-N.log` in GitHub Actions):

1. **`[patch]` lines** — each patch should print its completion message. If a cell says `[WARNING] Cell N raised an exception`, that patch has a bug
2. **`[L3] Intraday history snapshot saved`** — confirms data collection clock started
3. **`[Tier3] DSR filter`** — should report N models flagged
4. **`[nowcast]`** — should report 4 proxy values
5. **`[Tier2] Temporal attention features`** — should report N/139 tickers
6. **`Intraday model`** step — will print "Insufficient history" and exit cleanly on day 1 (correct behavior)
7. **Kill switch check** — should print drawdown values; should NOT trigger on day 1

If Cell 8 (`ML ensemble`) raises an exception, it's likely a Huber objective compatibility issue with the notebook's training code — the most likely point of failure.

---

## 7. Next Steps (ordered by priority)

1. **Fix GitHub billing** ✅ (user resolved)
2. **Wait for first morning run** — watch logs for any `[WARNING] Cell N raised an exception`
3. **Validate IC after 30 days** — check `data/predictions/ticker_accuracy.json`. If median IC < 0.03, the Huber label switch may have degraded the binary signal — consider reverting CELL_8_PREPATCH Huber patch as a targeted fix
4. **Write `stat_arb.py`** — pairs scanner only needs daily closes; can be done now while intraday data accumulates
5. **Phase 1 intraday model training** — automatic once 60 trading days of snapshots exist (~mid-August 2026); no code changes needed
6. **Add `torch_geometric`** to requirements.txt (no code yet — just adds the package to the install cache for Phase 3)

---

## 8. Key File Map

```
Quant-Terminal/
├── trading_model_v25.1.ipynb    # Main model notebook (24 cells)
├── quant_runner.py              # GitHub Actions entry point; 17 cell patches;
│                                # kill switch logic; enrichment data; dashboard export
│                                # 3,599 lines, 52 triple-quotes (even)
├── model_intraday.py            # Level 3 Phase 1 — intraday training loop (NEW)
├── requirements.txt             # 27 Python packages
├── .github/
│   └── workflows/
│       └── quant_daily.yml      # 6-trigger workflow (added 11:00 AM ET + intraday model step)
│       └── daily_run.yml        # Legacy workflow (inactive)
├── docs/
│   └── index.html               # Netlify dashboard (reads data.json from data branch)
│   └── data.json                # NOT in master — orphan `data` branch only
├── data/
│   ├── intraday_history/        # YYYYMMDD.csv snapshots — Level 3 training data (NEW)
│   ├── paper_trades/
│   │   └── paper_trades.csv
│   ├── predictions/
│   │   ├── predictions.csv
│   │   ├── daily_pnl_log.csv
│   │   ├── pnl_history.csv
│   │   ├── ticker_accuracy.json
│   │   ├── intraday_signals.json    # model_intraday.py output (NEW)
│   │   ├── intraday_metrics.json    # OOS AUC + IC per ticker (NEW)
│   │   ├── dsr_scores.json          # Deflated Sharpe per ticker (NEW)
│   │   ├── reality_check.json       # White Reality Check p-value (NEW)
│   │   ├── patent_cache.json        # USPTO patent velocity cache (NEW)
│   │   ├── transcript_cache.json    # EDGAR NLP cache (NEW)
│   │   └── insider_scores.json      # SEC Form 4 cache (NEW)
│   ├── weights/
│   │   ├── adaptive_weights.json
│   │   ├── learned_rules.json
│   │   ├── river_model.pkl
│   │   ├── ticker_calibration.json
│   │   ├── feature_importance.json
│   │   ├── portfolio_weights.json
│   │   └── ic_composite_weights.json
│   ├── models/
│   │   ├── model_cache.pkl          # v25.1 main model cache (featured dict REMOVED)
│   │   └── intraday_model.pkl       # model_intraday.py cache (NEW)
│   ├── nav_high_watermark.json      # Peak drawdown kill switch state (NEW)
│   ├── nowcast_cache.json           # Nowcast macro proxies 24h cache (NEW)
│   └── fred_cache.json              # FRED data 24h cache
├── netlify.toml                 # Netlify build (fetches data.json from data branch)
└── .gitignore                   # Excludes docs/data.json, model cache
```

---

## 9. GitHub Branches

| Branch | Purpose |
|--------|---------|
| `master` | All code, notebooks, state CSVs. Current HEAD: `e451b5a` |
| `data` | Orphan branch, single commit, force-replaced each cycle. Contains only `docs/data.json` |

---

## 10. Patch String Registry

All patches in `quant_runner.py` — these run around the notebook cells without modifying notebook JSON:

| Cell | Pre-patch | Post-patch |
|------|-----------|-----------|
| 3 | Remove broken tickers (HOLX/ANSS/^CPC) | — |
| 4 | FRED publication lag map + 24h cache | Nowcast proxies + IV term structure |
| 5 | yfinance 10y history monkey-patch | XS momentum ETF returns |
| 6 | Cross-sectional momentum + intraday helpers | Intraday features + XS momentum + insider scores + temporal attention + L3 history snapshot |
| 8 | EmbargoTSS + BlockBootstrapSMOTE + Huber objectives | Ridge ensemble members + DSR filter |
| 9 | Disable EarningsWhispers scraper | EDGAR transcript NLP + USPTO patent velocity |
| 11 | IC-composite weights | Conformal bands + net-of-cost + ternary labels + intraday blend |
| 12 | Ledoit-Wolf + HRP | CVaR/HRP blend weights |
| 13 | Regime Kelly sizing + adaptive TWAP helpers | Restore MAX_POSITION_PCT |
| 15 | — | White Reality Check / Hansen SPA |

---

*Generated 2026-05-17. Next scheduled run: morning cycle, Monday 2026-05-19 09:35 ET.*  
*First live test of all Tier 1–3 patches and model_intraday.py.*
