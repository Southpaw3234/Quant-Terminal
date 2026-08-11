# Weekly review checklist (cloud routine, Mondays 13:00 ET)

This replaced the daily session on 2026-08-06. The guards are in place and
`morning_watchdog.yml` pages Discord when a morning run misses, so **this review
is about EVIDENCE and GATES, not re-verifying plumbing.** Report the answers,
not the process.

## Read narrowly — this matters

`HANDOFF.md` is ~450KB and single lines exceed 30KB. **Never `Read` or `cat` it
whole; it will blow your context.** Instead:

**Derive the line ranges, never hardcode them** — the checkpoint table and the
ledgers both grow every session, so fixed ranges silently drift onto the wrong
content. These two recipes are tested and self-locating:

```bash
# the date-triggered checkpoint table
S=$(grep -n 'date-triggered checkpoints' HANDOFF.md | cut -d: -f1)
E=$(grep -n 'Step 4' HANDOFF.md | head -1 | cut -d: -f1)
sed -n "${S},${E}p" HANDOFF.md | cut -c1-600

# the newest session ledger (start of newest -> line before the one under it)
L=$(grep -n '^## .* SESSION LEDGER' HANDOFF.md | head -2 | cut -d: -f1 | tr '\n' ' ')
set -- $L; sed -n "$1,$(($2-1))p" HANDOFF.md | cut -c1-600
```

`cut -c1-600` on any HANDOFF line is the safe default — several exceed 30KB and
one is the entire document headline.

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
| `data/shadow/rank_ic_v2.csv` | Frame-1 v2 | the trustworthy one; first row due ~2026-08-13, 30 obs → **~2026-09-24** (counted pred-day by pred-day from 8/06, allowing for Labor Day — *not* "mid-September") |
| `data/shadow_intraday/rank_ic.csv` | Frame-2 | audited clean 8/06; gate ~8/21 |
| `data/stat_arb/stat_arb_ls.csv` | Frame-3 | gate at ≥30 obs, due ~2026-08-12 |

**3. Gates.** Stage-1 needs rank-IC **≥0.03 with t ≥2.0**. State plainly how far
each frame is, and whether any is close. As of 8/06 none were: Frame-2 read
`−0.0030, t −0.18` on 18 obs; Frame-3 Sharpe `−1.37` on 26 obs; walk-forward AUC
`0.4986` against a `0.4973` baseline.

The scorecard only prints *during* a morning run, so compute the gate numbers
straight off the CSV rather than hunting for them in a log:

```bash
awk -F, 'NR>1{v=$3+0; n++; s+=v; a[n]=v}
END{m=s/n; for(i=1;i<=n;i++) ss+=(a[i]-m)^2; sd=sqrt(ss/(n-1));
printf "n=%d mean=%+.4f sd=%.4f t=%+.2f\n", n, m, sd, m/(sd/sqrt(n))}' \
  data/shadow_intraday/rank_ic.csv
```

Same recipe works on `data/shadow/rank_ic_v2.csv` once it exists.

**4. Do NOT quote the Frame-1 legacy series as evidence.** It ranks on
`confidence`, which Cell 13's execution gate flattens to exactly 0.50 for every
HOLD and SELL — 97.5% ties, zero names below 0.5 on every day, both decile legs
falling out in file order. `rank_score` / the v2 series replaced it. The
analyzer prints a `*** WARNING: this series is NOT a valid cross-sectional
rank-IC ***` banner on the legacy read — **that banner is expected and healthy.**

**5. Settled rows — and the freeze.** Since `41c7d17` the analyzer withholds any
day whose exit bar is not yet settled, printing `[rank-ic] withholding <date>`.
That only ever **deferred a row's first write**; it never made written rows
immutable, and on 8/10 they demonstrably moved (`2026-07-15` `278,0.0959` →
`279,0.0955`; all 13 rows of `cross_sectional_ls.csv` rewritten).

Since **`29c92da` (8/11) the series is append-only** — first write wins, so a
previously written row *cannot* change. What to check now is the new log line:

```
[rank-ic] rank_ic: FROZE N recomputed value(s) that would have changed already-written rows
```

That line is **healthy** — it is the freeze doing its job — but the *magnitude*
is evidence about price-source stability, so quote it if N is large or a value
moved a lot. A row that changes anyway is a real regression: say so loudly.
`QT_RANK_IC_MUTABLE=1` disables the freeze and must never appear on a cron run.

**6. Attribution.** Dated changes are listed in the checkpoint table. The 9th
and most recent **model-behaviour** change is `65be103` (**`WYFI` added to
`WATCHLIST`**, effective from the first morning retrain on/after **2026-08-12**);
the 8th was `fafd4d6` (sector cap, 8/03). Attribute shifts to a dated change
where one exists, and never read a measurement change as alpha.

⚠️ **`WYFI` moves the equity cross-section 279 → 280.** A rank-IC row with
`n=280` is that change, not a data problem. Rows written before the retrain keep
`n=279` — see §5, the series is append-only as of `29c92da` and old rows no
longer move. If `WYFI` is *absent* from `predictions.csv`, the symbol is
probably not tradeable and was dropped by the delisted filter or `MIN_ROWS`;
that is expected behaviour, not a fault.

## Output

Half a page. Lead with: **did anything break, and did any gate move?** Then the
one thing worth attention this week. Flag anything needing a decision.

**Read-only. Do not commit, do not open PRs, do not change code.** If something
needs fixing, describe it and stop.
