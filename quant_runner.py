#!/usr/bin/env python3
"""
Quant Terminal v24.1 — GitHub Actions Runner
=============================================
Runs the full 5-stage autonomous cycle once per invocation.
Designed to be called by GitHub Actions at 09:35 ET every trading day.

Stages:
   1. Macro refresh
   2a. IV flags refresh
   2b. Market data + features + signals (parallel)
   3. Trade execution + prediction logging
   4. Outcome scoring
   5. Failure diagnosis + rule rewriting

All logs persist to Google Drive via rclone (configured in GitHub secrets).
Output is tee'd to cycle_output.log and uploaded as a GitHub Actions artifact.

Upgrade from v21 → v24.1:
  - CVaR portfolio optimisation (CLARABEL solver)
  - Options IV earnings flag with position scaling
  - Kelly Criterion + dynamic VIX leverage
  - River ML adaptive signal weights
  - Self-learning rule rewriter
  - FinBERT sentiment (skipped in FAST_MODE)
  - 140-ticker watchlist
"""

import os
import sys
import json
import subprocess
import datetime
import traceback
from pathlib import Path

# ── Logging setup — tee stdout to cycle_output.log ───────────────────────
import io

LOG_FILE = "cycle_output.log"

class _Tee(io.TextIOWrapper):
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data)
    def flush(self):
        for s in self._streams:
            try: s.flush()
            except Exception: pass

_log_fh = open(LOG_FILE, "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _log_fh)
sys.stderr = _Tee(sys.__stderr__, _log_fh)

print(f"\n{'='*60}")
print(f"QUANT TERMINAL v24.1 — GitHub Actions Daily Run")
print(f"Started: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
print(f"{'='*60}\n")

# ── rclone helpers ────────────────────────────────────────────────────────
GDRIVE_RCLONE_CONF = os.environ.get("GDRIVE_RCLONE_CONF", "")
GDRIVE_REMOTE      = "gdrive"
GDRIVE_FOLDER      = "quant_terminal_v24.1"   # folder in My Drive
LOCAL_DATA         = Path("data")

def _write_rclone_conf():
    """Decode the base64 rclone config secret and write to ~/.config/rclone/rclone.conf"""
    if not GDRIVE_RCLONE_CONF:
        print("⚠  GDRIVE_RCLONE_CONF not set — Drive sync disabled, using local data/")
        return False
    import base64
    conf_path = Path.home() / ".config" / "rclone" / "rclone.conf"
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_bytes(base64.b64decode(GDRIVE_RCLONE_CONF))
    print(f"✓ rclone config written → {conf_path}")
    return True

def _rclone_download():
    """Sync Google Drive → local data/ before the cycle."""
    print("\nStage 0a: Syncing data from Google Drive...")
    LOCAL_DATA.mkdir(exist_ok=True)
    for sub in ["paper_trades", "predictions", "weights", "models"]:
        (LOCAL_DATA / sub).mkdir(exist_ok=True)
    try:
        result = subprocess.run(
            ["rclone", "copy",
             f"{GDRIVE_REMOTE}:{GDRIVE_FOLDER}", str(LOCAL_DATA),
             "--exclude", "models_cache.pkl",   # too large to sync every run
             "--transfers", "8", "--quiet"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            print("✓ Drive → local sync complete")
        else:
            print(f"⚠  rclone download warning: {result.stderr[:300]}")
    except Exception as e:
        print(f"⚠  rclone download error: {e} — continuing with local data")

def _rclone_upload():
    """Sync local data/ → Google Drive after the cycle."""
    print("\nStage 6: Syncing data to Google Drive...")
    try:
        result = subprocess.run(
            ["rclone", "copy",
             str(LOCAL_DATA), f"{GDRIVE_REMOTE}:{GDRIVE_FOLDER}",
             "--exclude", "models_cache.pkl",
             "--transfers", "8", "--quiet"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            print("✓ local → Drive sync complete")
        else:
            print(f"⚠  rclone upload warning: {result.stderr[:300]}")
    except Exception as e:
        print(f"⚠  rclone upload error: {e}")

# ── Google Drive sync (download existing data before cycle) ───────────────
_drive_available = _write_rclone_conf()
if _drive_available:
    _rclone_download()

# ── Ensure local data directories always exist ────────────────────────────
for _sub in ["paper_trades", "predictions", "weights", "models"]:
    (LOCAL_DATA / _sub).mkdir(parents=True, exist_ok=True)

# ── Load notebook and execute core pipeline cells ─────────────────────────
NB_PATH = "trading_model_v24.1.ipynb"
print(f"\nLoading notebook: {NB_PATH}")

with open(NB_PATH, encoding="utf-8-sig") as f:
    nb = json.load(f)

cells = nb["cells"]
print(f"Notebook loaded: {len(cells)} cells\n")

# Cell indices to skip (0-indexed):
#   0  — markdown title
#   1  — pip install (handled by requirements.txt)
#   16 — homepage render (display-only)
#   17 — BTC cycle tracker (display-only)
#   18 — scheduler/threading (we run directly)
#   19 — dashboard render (display-only)
#   20 — full audit (display-only)
#   21 — performance tracker (display-only)
#   22 — backtest engine (display-only)
SKIP_CELLS = {0, 1, 16, 17, 18, 19, 20, 21, 22}

# Patch appended to Cell 3 (config): redirects all file paths to local data/
# and applies speed settings appropriate for CI.
GH_ACTIONS_PATCH = r"""
# ── GitHub Actions overrides (injected by quant_runner.py) ───────────────
import os as _os
if _os.environ.get("GH_ACTIONS"):
    from pathlib import Path as _Path

    # Redirect all persistent storage to data/ (Google Drive not mounted here;
    # rclone syncs before/after instead)
    _drive_dir     = _Path("data")
    _drive_mounted = False

    PT_LOG_FILE         = str(_Path("data/paper_trades/paper_trades.csv"))
    PRED_LOG_FILE       = str(_Path("data/predictions/predictions.csv"))
    DAILY_PNL_LOG_FILE  = str(_Path("data/predictions/daily_pnl_log.csv"))
    LOG_DIR             = "data"
    RULES_FILE          = str(_Path("data/weights/learned_rules.json"))
    WEIGHTS_FILE        = str(_Path("data/weights/adaptive_weights.json"))
    MODEL_CACHE_FILE    = _Path("data/models_cache.pkl")
    VWAP_LOG_FILE       = _Path("data/vwap_benchmark.csv")
    EXEC_LOG_FILE       = _Path("data/execution_quality.csv")
    KILL_FLAG_FILE      = _Path("data/KILL_SWITCH_ACTIVE.flag")
    PDT_LOG_FILE        = _Path("data/pdt_log.csv")
    MODEL_RETRAIN_FLAG  = _Path("data/RETRAIN_NEEDED.flag")

    # CI speed settings (keeps each run under 60 min on free-tier runners)
    FAST_MODE            = True   # skips FinBERT, GARCH MC paths
    GARCH_PATHS          = 50     # default 500 → 50  (still valid signal)
    QUICK_TUNE_TRIALS    = 3
    FULL_TUNE_TRIALS_XGB = 10
    FULL_TUNE_TRIALS_LGB = 10
    FULL_TUNE_TRIALS_CAT = 10

    print("GH_ACTIONS mode: storage → data/, FAST_MODE=True, GARCH_PATHS=50")
"""

# Shared execution namespace — all cells share this dict like one Python module
namespace = {"__name__": "__main__"}

# Execute cells sequentially
failed_cells = []
for i, cell in enumerate(cells):
    if cell["cell_type"] != "code":
        continue
    if i in SKIP_CELLS:
        print(f"[SKIP] Cell {i}")
        continue

    src = "".join(cell["source"])

    # Append the GH_ACTIONS patch right after the Drive-mount block in Cell 3
    if i == 3:
        src = src + "\n\n" + GH_ACTIONS_PATCH

    print(f"\n{'─'*55}")
    print(f"[CELL {i}] Running...")
    print(f"{'─'*55}")

    try:
        exec(src, namespace)  # noqa: S102
    except SystemExit as e:
        print(f"[CELL {i}] SystemExit({e.code}) — continuing")
    except Exception:
        print(f"[WARNING] Cell {i} raised an exception:")
        traceback.print_exc()
        failed_cells.append(i)

# ── Summary ───────────────────────────────────────────────────────────────
finish_ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
print(f"\n{'='*60}")
print(f"Daily cycle complete — {finish_ts}")
if failed_cells:
    print(f"Cells with warnings: {failed_cells}")
print(f"{'='*60}\n")

# ── Flush log and sync back to Google Drive ───────────────────────────────
_log_fh.flush()
if _drive_available:
    _rclone_upload()

_log_fh.close()
