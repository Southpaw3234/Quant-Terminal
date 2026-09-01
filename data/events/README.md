# `data/events/` — event-study inputs and ledger

v27 component **A2**. See `docs/V27_BUILD_PLAN.md` §④A and the E1 criterion in
`docs/V27_PREREGISTRATION.md`.

## `events.csv` — input schema

| column | required | meaning |
|---|---|---|
| `event_id` | ✅ | stable unique key. **The freeze is keyed on this** — reusing an id for a different event silently pins the old value. |
| `ticker` | ✅ | uppercase symbol |
| `event_ts` | ✅ | **announcement timestamp, not the trade date.** See below. |
| `event_type` | ✅ | grouping label; each distinct value is summarised separately |
| `control_ticker` | — | per-event matched control. Falls back to `QT_EVENT_BENCH` (SPY) when absent. |

### `event_ts` is the single most important field

It must be the moment the information became **public** — the SEC acceptance
timestamp, the press-release time — not the date you noticed it and not the
date you would have traded it.

The engine enters on the first bar **strictly after** `event_ts`. So if you
record a Form 4 accepted at 18:30 ET on a Tuesday as `Tuesday`, entry is
Wednesday's close, which is correct. If you record it as the *filing period*
date (often days earlier), you have introduced a look-ahead the engine cannot
detect, because it has no way to know your timestamp is wrong.

🔑 **Every look-ahead this design can prevent, it prevents structurally. This
field is the one place a look-ahead can still enter, and it enters through
data quality rather than code.** Treat a wrong `event_ts` as the same class of
error as v25's stale-signal bug (`5e96366`), which cost fifty prediction days.

## `event_study.csv` — output ledger

Append-only and **frozen on first write**, keyed on `event_id`. A written
abnormal return never changes, even if prices are re-downloaded and the
recomputed value differs — drift is printed loudly (`FROZE n recomputed
value(s)`) rather than silently applied.

Columns: `event_id, ticker, event_type, event_ts, entry_date, exit_date,
horizon, event_ret, control, control_ret, abnormal_ret`.

## Running it

```bash
python event_study.py
```

Environment overrides are listed in the `event_study.py` docstring. The two
that matter operationally:

* `QT_EVENT_HORIZON` — holding period in **trading bars** (default 21)
* `QT_EVENT_MUTABLE=1` — disables the freeze. **Debugging only.** It must
  never appear on a scheduled run; the whole point of the ledger is that the
  number read on decision day is the series that was accumulated.

## Before trusting any reading

```bash
python validate_event_study.py
```

Seven checks against synthetic data with known answers — null returns exactly
zero, a planted +2% is recovered, a look-ahead would read +50% where the
correct answer is 0%, overlaps deduplicate, unsettled bars are withheld, and
written rows do not move. CI runs this on every push to a `v27/**` branch.

## What is NOT here yet

* **Event extractors** (A3) — nothing populates `events.csv` yet; it is
  written by hand or by a future extractor against the twelve feeds already
  wired in `quant_runner.py`.
* **WRC/SPA** — E1 additionally requires multiple-testing correction at
  **K=5**. The engine reports its own read and explicitly says the correction
  is *not* evaluated. Wiring `abnormal_ret` into the existing WRC/SPA code is
  a separate step.
* **Characteristic matching** — `control_ticker` is the seam. Size / sector /
  momentum matching is deliberately not invented before real events exist,
  because a matching scheme chosen after seeing the data is a free parameter
  nobody counted against K.
