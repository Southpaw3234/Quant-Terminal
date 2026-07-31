# Archived GitHub Actions run logs — stale-feature era

> Directory is named `archived_runs/`, not `run_logs/`, deliberately: the
> repo's `.gitignore` carries a bare `run_logs/` rule for the workstation's
> local trigger/watch logs, and a bare rule matches that directory name at
> **any** depth — an archive placed under `run_logs/` here is silently ignored
> and never actually committed.

GitHub Actions retains workflow logs for **90 days**, after which they are gone
permanently. Runs archived here were pulled before their expiry because they are
the only surviving record of something the committed evidence files cannot show.

Each file is the output of `gh run view <id> --log`, with two cosmetic
transformations applied so the text is greppable:

1. ANSI colour escapes (`\x1b[...m`) stripped;
2. the per-line `trade<TAB>UNKNOWN STEP<TAB>` prefix that `gh` prepends stripped.

Nothing else was altered — timestamps, ordering and content are verbatim. To
verify a line, `gh run view <id> --log` reproduces it exactly until the log
expires.

---

## `2026-05-12_run-25732008349_morning.log`

**Run [25732008349](https://github.com/Southpaw3234/Quant-Terminal/actions/runs/25732008349)**
· `workflow_dispatch` · created 2026-05-12 11:39:18Z · completed 12:35:27Z (56 min)
· conclusion `success` · head `de8a6d4` · archived 2026-07-31, ~10 days before its
~2026-08-10 expiry.

**Why this one is worth keeping.** 2026-05-12 is the **first date in the frozen
stale-era Frame-1 window** (`../rank_ic.csv`, window `2026-05-12 -> 2026-07-07`,
N=45), and it **predates the trades ledger entirely** — `data/predictions/`
trade rows begin 2026-05-29. Without this log there is no record of what the
system actually did on day one of the window whose final read is frozen in this
directory.

**What it shows** (line numbers are into the archived file):

| | |
|---|---|
| L699 | `Run type: morning (UTC hour=11 day=2)` |
| L2262 | `Run date: 2026-05-12 \| Capital: $10,000` |
| L2264 | `Current equity: $10,000.00` |
| L2267–2278 | `BUY GOOGL x1 @ $383.25` · `BUY SPY x2 @ $718.01` · `BUY AMD x2 @ $341.54` · `BUY INTC x28 @ $95.78` |
| L2292 | `4 trades executed \| 139 predictions logged` |
| L2339 | `MORNING cycle complete -- 2026-05-12 12:34 UTC` |

Three details worth flagging to anyone reading this later:

- **`Current equity: $10,000.00` is the old-era hardcoded capital**, not a real
  broker read — the account was not yet wired to live Alpaca equity. Any P&L or
  sizing arithmetic derived from this run is on a $10k notional basis and does
  **not** compare to later runs.
- **139 predictions**, against 307 in the current universe.
- **`Run type: morning` resolved at 11Z.** Under the market-hours guard shipped
  2026-07-24 (`e7b1d5f`, morning resolves only inside 13–20Z) this run would today
  be **downgraded to intraday**. It is a clean example of the pre-guard behaviour
  that caused the 7/24 zero-entry incident.

**Caveat that applies to every signal in this log:** it sits inside the
stale-feature era (fix `5e96366`, 2026-07-13). Every price and feature the run
acted on was drawn from a 5–10 session stale featured row, so the entries above
are **not** evidence about the current model. See `../README.md`.
