#!/usr/bin/env python3
"""
Daily autonomous trading cycle runner for GitHub Actions.

Reads trading_model_v24.1.ipynb and executes the core pipeline cells in
sequence, then commits updated CSVs back to the repo.

Cells executed:  2 (imports) → 3 (config) → 4 (macro) → 5 (data) →
                 6 (features) → 7 (HMM) → 8 (models) → 9 (GARCH/IV) →
                 10 (sentiment) → 11 (signals) → 12 (CVaR) →
                 13 (paper trade) → 14 (score) → 15 (diagnosis)

Cells skipped:   0 (markdown title), 1 (pip install),
                 16-22 (display-only dashboard/audit/backtest cells)
"""

import json
import sys
import os
from pathlib import Path
import datetime

print(f"\n{'='*60}")
print(f"QUANT TERMINAL — GitHub Actions Daily Run")
print(f"Started: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
print(f"{'='*60}\n")

# ── Ensure data directories exist ────────────────────────────────────────
for sub in ["paper_trades", "predictions", "weights", "models"]:
    Path(f"data/{sub}").mkdir(parents=True, exist_ok=True)

# ── Load notebook ─────────────────────────────────────────────────────────
NB_PATH = "trading_model_v24.1.ipynb"
with open(NB_PATH, encoding="utf-8-sig") as f:
    nb = json.load(f)

cells = nb["cells"]
print(f"Loaded notebook: {len(cells)} cells\n")

# ── Cells to skip (0-indexed) ──────────────────────────────────────────────
# 0:  markdown title cell
# 1:  pip install (handled by requirements.txt)
# 16: homepage render (display-only)
# 17: BTC cycle tracker (display-only)
# 18: scheduler thread (we're running directly, not via thread)
# 19: dashboard render (display-only)
# 20: full audit (display-only)
# 21: performance tracker (display-only)
# 22: backtest engine (display-only)
SKIP_CELLS = {0, 1, 16, 17, 18, 19, 20, 21, 22}

# ── GitHub Actions patch appended to Cell 3 (config) ─────────────────────
# Overrides Drive paths → local data/ directory
# Sets FAST_MODE=True  → skips FinBERT, reduces GARCH paths (much faster)
GH_ACTIONS_PATCH = r"""
# ── GitHub Actions overrides (injected by run_daily.py) ──────────────────
import os as _os
if _os.environ.get("GH_ACTIONS"):
    # Redirect all persistent storage to the local data/ directory
    # (Google Drive is not available in GitHub Actions)
    _drive_dir     = Path("data")
    _drive_mounted = False

    (Path("data") / "paper_trades").mkdir(exist_ok=True)
    (Path("data") / "predictions").mkdir(exist_ok=True)
    (Path("data") / "models").mkdir(exist_ok=True)
    (Path("data") / "weights").mkdir(exist_ok=True)

    PT_LOG_FILE         = str(Path("data/paper_trades/paper_trades.csv"))
    PRED_LOG_FILE       = str(Path("data/predictions/predictions.csv"))
    DAILY_PNL_LOG_FILE  = str(Path("data/predictions/daily_pnl_log.csv"))
    LOG_DIR             = "data"
    RULES_FILE          = str(Path("data/weights/learned_rules.json"))
    WEIGHTS_FILE        = str(Path("data/weights/adaptive_weights.json"))
    MODEL_CACHE_FILE    = Path("data/models_cache.pkl")
    VWAP_LOG_FILE       = Path("data/vwap_benchmark.csv")
    EXEC_LOG_FILE       = Path("data/execution_quality.csv")
    KILL_FLAG_FILE      = Path("data/KILL_SWITCH_ACTIVE.flag")
    PDT_LOG_FILE        = Path("data/pdt_log.csv")
    MODEL_RETRAIN_FLAG  = Path("data/RETRAIN_NEEDED.flag")

    # Speed settings for CI environment
    FAST_MODE    = True    # skips FinBERT, reduces GARCH paths
    GARCH_PATHS  = 50      # default 500 → 50 for speed (still valid signal)
    QUICK_TUNE_TRIALS    = 3
    FULL_TUNE_TRIALS_XGB = 10
    FULL_TUNE_TRIALS_LGB = 10
    FULL_TUNE_TRIALS_CAT = 10

    print("GH Actions mode: storage → data/, FAST_MODE=True, GARCH_PATHS=50")
"""

# ── Shared execution namespace ────────────────────────────────────────────
namespace = {"__name__": "__main__"}

# ── Execute cells ─────────────────────────────────────────────────────────
for i, cell in enumerate(cells):
    if cell["cell_type"] != "code":
        continue
    if i in SKIP_CELLS:
        print(f"[SKIP] Cell {i}")
        continue

    src = "".join(cell["source"])

    # Inject patch at the END of cell 3 (config/Drive setup)
    if i == 3:
        src = src + "\n\n" + GH_ACTIONS_PATCH

    print(f"\n{'─'*55}")
    print(f"[CELL {i}] Running...")
    print(f"{'─'*55}")

    try:
        exec(src, namespace)  # noqa: S102
    except SystemExit as e:
        print(f"[CELL {i}] SystemExit({e.code}) — continuing")
    except Exception as e:
        import traceback
        print(f"[WARNING] Cell {i} raised an exception:")
        traceback.print_exc()
        # Non-fatal: keep going so later cells can still run

print(f"\n{'='*60}")
print("Daily cycle complete!")
print(f"Finished: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
print(f"{'='*60}\n")
