# Weekly review checklist (cloud routine, Mondays 13:00 ET)

This replaced the daily session on 2026-08-06. The guards are in place and
`morning_watchdog.yml` pages Discord when a morning run misses, so **this review
is about EVIDENCE and GATES, not re-verifying plumbing.** Report the answers,
not the process.

## Read narrowly — this matters

`HANDOFF.md` is ~450KB and single lines exceed 30KB. **Never `Read` or `cat` it
whole; it will blow your context.** Instead:

```bash
grep -n '^## .* SESSION LEDGER' HANDOFF.md | head -3   # locate newest ledgers
sed -n '31,60p' HANDOFF.md | cut -c1-600               # checkpoint table, truncated
```

Then `sed` over the newest ledger's line range. `cut -c1-600` on any HANDOFF
line is the safe default.

## Environment gotchas

- `data/predictions/predictions.csv` has quoted dict fields containing commas,
  so `awk -F,` sees **29, 33 or 37** fields per row. Read `rank_score` as
  **`$NF`** and legacy `confidence` as `$4`. Never use a fixed high index.
- **Never put `/` inside a `gh --jq` expression** — query fields separately
  (`gh run view ID --json status -q .status`). A `/` gets path-mangled.

## What to check

**1. Runs.** `gh run list --limit 40`. The morning run is the ~130–160 min one
each weekday; everything else is ≤30 min. Flag any weekday missing one, and any
`not acquired by Runner of type hosted` failures — that is GitHub infra, needing
a re-dispatch, **not** a code fault.

**2. Clocks** — row counts and latest values:

| file | frame | status |
|---|---|---|
| `data/shadow/rank_ic.csv` | Frame-1 legacy | **INVALID — see §4** |
| `data/shadow/rank_ic_v2.csv` | Frame-1 v2 | the trustworthy one; first row due ~2026-08-13, needs ~30 obs → mid-Sept |
| `data/shadow_intraday/rank_ic.csv` | Frame-2 | audited clean 8/06; gate ~8/21 |
| `data/stat_arb/stat_arb_ls.csv` | Frame-3 | gate at ≥30 obs, due ~2026-08-12 |

**3. Gates.** Stage-1 needs rank-IC **≥0.03 with t ≥2.0**. State plainly how far
each frame is, and whether any is close. As of 8/06 none were: Frame-2 read
`−0.0030, t −0.18` on 18 obs; Frame-3 Sharpe `−1.37` on 26 obs; walk-forward AUC
`0.4986` against a `0.4973` baseline.

**4. Do NOT quote the Frame-1 legacy series as evidence.** It ranks on
`confidence`, which Cell 13's execution gate flattens to exactly 0.50 for every
HOLD and SELL — 97.5% ties, zero names below 0.5 on every day, both decile legs
falling out in file order. `rank_score` / the v2 series replaced it. The
analyzer prints a `*** WARNING: this series is NOT a valid cross-sectional
rank-IC ***` banner on the legacy read — **that banner is expected and healthy.**

**5. Settled rows.** Since `41c7d17` the analyzer withholds any day whose exit
bar is not yet settled, printing `[rank-ic] withholding <date>`. **Previously
written rows must never change.** If a row that existed last week has a
different value this week, that is a real regression — say so loudly.

**6. Attribution.** Dated changes are listed in the checkpoint table. The 8th
and most recent **model-behaviour** change is `fafd4d6` (sector cap, effective
8/03); everything since is measurement-only. Attribute shifts to a dated change
where one exists, and never read a measurement change as alpha.

## Output

Half a page. Lead with: **did anything break, and did any gate move?** Then the
one thing worth attention this week. Flag anything needing a decision.

**Read-only. Do not commit, do not open PRs, do not change code.** If something
needs fixing, describe it and stop.
