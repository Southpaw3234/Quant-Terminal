# Stale-feature era — final Frame-1 gate read (frozen 2026-07-14)

Every prediction from v25.1 (`ec9d19a`, 2026-05-17) through 2026-07-13 was
computed off 5-10 session stale featured rows (era bug, fix `5e96366`).
The Stage-1 window was RESTARTED at 2026-07-14 (user decision, 7/14 ledger);
`analyze_rank_ic.py` now filters to pred dates >= QT_STAGE1_START (default
2026-07-14), so these files freeze the last full read of the lagged model:

- window 2026-05-12 -> 2026-07-07, N=45
- mean rank-IC -0.0403 (t -3.05), trailing-20d -0.0329
- clean L/S cumulative -32.5%, hedged -32.6%, residual beta +0.08

Regenerate the full blended series any time with:
`QT_STAGE1_START=2026-05-12 python analyze_rank_ic.py`

## Addendum 2026-07-16 — self-learning state frozen (era-gate follow-up)

The 7/16 audit found three more live consumers of stale-era scored outcomes
(kill switch fixed separately, `49a5884`): the Cell-15 rule engine
(LEARNED_RULES dampeners applied to live composite scores in Cell 11, plus
ADAPTIVE_WEIGHTS / FEATURE_IMPORTANCE / River rows), the Cell-15 staleness
detector, and the Cell-13 `_WL_RATIO` Kelly win/loss cache. All three are now
era-gated to `QT_STAGE1_START` in quant_runner.py.

Frozen here before the reset (both re-derived daily from predictions.csv, so
these snapshots are for the record — the live carriers self-heal):

- `learned_rules_stale_era.json` — 47 rule keys (45 per-ticker dampeners up to
  20%, heavily semis/energy), all learned from stale-era outcomes with
  baseline-corrupted `price_at_pred`.
- `adaptive_weights_stale_era.json` — ensemble weights as drifted by stale-era
  accuracy reads (w_ensemble 0.5714 vs 0.55 default).

The live `data/weights/*.json` copies were reset to Cell-3 defaults ({} rules,
0.55/0.20/0.10/0.10/0.05 weights); the rule engine relearns from fresh-era
rows only once they mature (~2026-07-21+).

## Addendum 2026-07-31 — run log archived before GitHub's 90-day expiry

`archived_runs/2026-05-12_run-25732008349_morning.log` — the morning run of
**2026-05-12, the first date in the frozen window above**, pulled from GitHub
Actions ~10 days before its ~2026-08-10 retention expiry. It predates the trades
ledger (which starts 2026-05-29), so it is the only surviving record of what the
system did on day one of this window: 4 BUYs (GOOGL, SPY, AMD, INTC x28),
139 predictions, and the old-era hardcoded `Current equity: $10,000.00`.
See `archived_runs/README.md` for the full read and its caveats.
