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
