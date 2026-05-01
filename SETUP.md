# Quant Terminal v21 — GitHub Actions Setup Guide

## What this does
Runs the full 5-stage autonomous trading cycle at **09:35 ET every weekday**,
completely free, on GitHub's servers. Your laptop can be off. No Colab needed.

## Files
```
your-repo/
├── quant_runner.py              ← the trading engine
├── .github/
│   └── workflows/
│       └── quant_daily.yml     ← the schedule
└── SETUP.md                    ← this file
```

---

## Step 1 — Create a GitHub repository

1. Go to github.com → New repository
2. Name it `quant-terminal` (or anything you like)
3. Set to **Private** (keeps your strategy confidential)
4. Add a README, then click Create

---

## Step 2 — Upload the files

Upload both files to the root of your repo:
- `quant_runner.py`
- `.github/workflows/quant_daily.yml`

You can drag and drop them in the GitHub web UI.

---

## Step 3 — Set up Google Drive sync (rclone)

This is what moves the prediction logs, weights, and rules between
GitHub's servers and your Google Drive so nothing is lost between runs.

**On your laptop:**

1. Install rclone: https://rclone.org/install/
2. Run: `rclone config`
3. Choose `n` for new remote
4. Name it: `gdrive`
5. Choose Google Drive (option 13 or search for "drive")
6. Leave client_id and client_secret blank (press Enter)
7. Choose scope 1 (full access)
8. Follow the browser auth flow
9. Say Yes to advanced config if asked, then confirm

**Export your config:**
```bash
cat ~/.config/rclone/rclone.conf | base64 | tr -d '\n'
```
Copy the entire output — this is your `GDRIVE_RCLONE_CONF` secret.

---

## Step 4 — Add GitHub Secrets

Go to your repo → Settings → Secrets and variables → Actions → New secret

Add these secrets:

| Secret name         | Value                          | Required? |
|---------------------|--------------------------------|-----------|
| GDRIVE_RCLONE_CONF  | base64 rclone config (Step 3)  | ✅ Yes    |
| FRED_API_KEY        | From fred.stlouisfed.org       | Recommended |
| ALPACA_API_KEY      | From alpaca.markets            | Optional  |
| ALPACA_SECRET_KEY   | From alpaca.markets            | Optional  |
| NEWS_API_KEY        | From newsapi.org               | Optional  |
| AV_API_KEY          | From alphavantage.co (free)    | Optional  |

**TRADIER_UPGRADE** (when ready later):
```
TRADIER_API_KEY  → from dashboard.tradier.com/settings/api
```
Also uncomment the TRADIER lines in quant_runner.py and quant_daily.yml.

---

## Step 5 — Test it manually

1. Go to your repo → Actions tab
2. Click "Quant Terminal v21 — Daily Cycle"
3. Click "Run workflow" → Run workflow
4. Watch the logs live — should take 20-30 min first run

---

## Step 6 — Check your morning report

The cycle runs every weekday at 09:35 ET automatically.
To read the morning report:

1. Open Colab → upload trading_model_v21.ipynb
2. Run only **Cell 16** (the homepage cell)
   - This reads from your Google Drive files
   - Shows today's signals, accuracy stats, learned rules
   - No need to re-run the full model

---

## DST adjustment

The workflow runs at 13:35 UTC which is:
- **09:35 ET** during EST (November → March)
- **10:35 ET** during EDT (March → November)

To keep it at 09:35 ET year-round, change the cron in quant_daily.yml:
- **EDT (Mar-Nov):** `35 13 * * 1-5`
- **EST (Nov-Mar):** `35 14 * * 1-5`

Or use `35 14 * * 1-5` to always run at 10:35 ET regardless of DST.

---

## Monitoring

- **Logs:** GitHub → Actions → click any run → cycle_output.log artifact
- **Drive files:** Google Drive → quant_terminal_v21 folder
- **Morning report:** Colab Cell 16 only (reads from Drive, no retraining)

---

## Upgrade path (Tradier)

When ready to upgrade earnings data and options chains:
1. Open Tradier account at tradier.com ($10/mo brokerage)
2. Get API key from dashboard.tradier.com/settings/api
3. Add `TRADIER_API_KEY` secret to GitHub
4. Uncomment TRADIER lines in quant_runner.py (search `TRADIER_UPGRADE`)
5. Uncomment TRADIER line in quant_daily.yml
6. Commit changes — next run uses Tradier automatically
