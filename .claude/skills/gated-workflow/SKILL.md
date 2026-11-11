---
name: gated-workflow
description: Write or change a GitHub Actions workflow in this repository — the validate/side-effect job split, double gating so a push can never trigger a job that spends something irreversible, artifact-not-commit for emitted data, fingerprint-before-counting, and the orphan data branch. Use whenever adding a workflow, adding a job to one, or changing what a job is allowed to do.
---

# Gated workflows

Every workflow here splits into **one job that gates every push** and **jobs
that can do something irreversible and are unreachable from a push**.

Irreversible here means: spends one of K=5 pre-registered reads, writes a
branch, triggers a deploy, runs a five-hour EDGAR sweep, or costs money.

## The shape

```yaml
on:
  workflow_dispatch:
    inputs:
      run_extract:
        description: 'Build the event file (SELECTION only, no returns, no K)'
        type: boolean
        default: false
  push:
    branches: ['master', 'v27/**', 'v28/**', 'v29/**']
    paths:
      - 'the_script.py'
      - 'validate_the_script.py'
      - '.github/workflows/this_file.yml'

permissions:
  contents: read

jobs:
  validate:              # runs on EVERY push. This is the gate.
    runs-on: ubuntu-latest
    steps:
      - name: Install deps
        run: pip install "numpy<2.4" pandas     # no yfinance, no requests
      - run: python -m py_compile the_script.py
      - run: python validate_the_script.py
      - name: Assert nothing under data/ changed
        run: |
          set -u
          if ! git diff --quiet -- data/ 2>/dev/null; then
            echo "::error::validation modified data/ — it must be read-only"
            git --no-pager diff --stat -- data/
            exit 1
          fi

  extract:
    needs: validate
    if: github.event_name == 'workflow_dispatch' && inputs.run_extract
    ...
```

🔑 **The `if` is a double gate: the event must be a dispatch AND the input
must be set.** Either alone is not enough. A single `workflow_dispatch`
trigger without the input check still fires on a dispatch meant for a
different job in the same file.

**Verify the gate held.** After any change to triggers, check that the
side-effecting jobs report `skipped` on a push:

```bash
gh run view <id> --json jobs -q '.jobs[]|"\(.name) \(.conclusion)"'
# validate success
# extract  skipped      <- this line is the point
```

Adding a `push:` trigger to a workflow that previously had none **turns on
every ungated job in it**. Gate them in the same commit.

## Branch lists go stale silently

Five validators once listed only `v27/**` and `v29/**`, so nothing ran on
`master` — merges were unvalidated for weeks and nothing reported an error,
because a workflow that does not trigger produces no failure. When adding a
branch prefix, add it everywhere, and confirm a run appears.

## Emitted data is an artifact, not a commit

A produced event file, book schedule or snapshot is **specification-adjacent**:
it enters the repo through a reviewed commit at declaration time, carrying
its fingerprint. A job must not commit it.

```yaml
- uses: actions/upload-artifact@v4
  with:
    name: v27-spec4-item301-events
    path: data/events/events_8k_item301.csv
    if-no-files-found: error      # never silently upload nothing
```

Downstream jobs fetch it rather than forcing the commit early:

```yaml
- uses: actions/download-artifact@v4
  if: inputs.events_run_id != ''
  with:
    name: v27-spec4-item301-events
    path: data/events
    run-id: ${{ inputs.events_run_id }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

That needs `permissions: {contents: read, actions: read}` on the job.

**Refuse to rebuild a file that already exists.** Once named by a
declaration it is a frozen input, and rebuilding is how a frozen input
quietly stops being frozen:

```yaml
- run: |
    if [ -f data/events/x.csv ]; then
      echo "::error::already exists — refusing to rebuild"; exit 3
    fi
```

## 🔑 Fingerprint before counting anything

Numbers that will be quoted in a declaration are only meaningful if the file
they were computed on is identified.

```yaml
- name: Fingerprint what was fetched, BEFORE counting anything
  run: |
    set -u
    f="${{ inputs.events_csv }}"
    if [ ! -f "$f" ]; then
      echo "::error::no file at $f — pass events_run_id to fetch it"; exit 4
    fi
    python - "$f" <<'PY'
    import hashlib, sys
    import pandas as pd
    p = sys.argv[1]
    b = open(p, "rb").read().replace(b"\r\n", b"\n")   # CRLF normalised
    d = pd.read_csv(p, keep_default_na=False, na_values=[])
    print(f"  path   {p}")
    print(f"  rows   {len(d)}")
    print(f"  sha256 {hashlib.sha256(b).hexdigest()}")
    PY
```

`core.autocrlf=true` on Windows means a raw hash will not match CI. Always
normalise. Always `keep_default_na=False` — `NA` is a real ticker.

A missing file exits with a **distinct code and a message naming the fix**,
not a traceback from inside the script.

## Heredocs inside a block scalar

YAML strips the common indentation, so this works — the shell sees `PY` at
column 0:

```yaml
run: |
  python - <<'PY'
  import json
  print("hi")
  PY
```

Inside an `if`, keep the heredoc **body and terminator** at the block's base
indentation while the `python` line is indented. Use `<<'PY'` (quoted) so
`$` and backticks are not expanded.

## Things that silently do not work

- **A step cannot test its own `env` in `if:`**, and a secret cannot be read
  in an `if:` at all. Put the presence check inside the script.
- **`inputs.x` is null on a `schedule` event.** Use `${{ inputs.limit || '0' }}`.
- **`concurrency`** on anything that force-pushes, or a slow run gets
  overtaken and the branch moves backwards:
  ```yaml
  concurrency: {group: daily-snapshot, cancel-in-progress: false}
  ```

## Publishing to the orphan `data` branch

Daily artifacts do not belong in `master` history — a registry whose diff is
buried under 250 snapshot commits is a registry nobody audits. The `data`
branch is recreated from nothing each cycle, so a force-push there can never
rewrite a commit holding a registry or a read.

```bash
cp docs/daily.json /tmp/daily.json
git checkout --orphan data-tmp
git rm -rq --cached . || true
rm -rf docs && mkdir -p docs
cp /tmp/daily.json docs/daily.json
git add -f docs/daily.json
git commit -qm "daily snapshot"
git push -f origin data-tmp:data
```

`netlify.toml` fetches from it, and `docs/index.html` reads it client-side
from `raw.githubusercontent.com`, so data refreshes without a site rebuild.
Add the generated file to `.gitignore` so a local run cannot commit a frozen
copy to `master`.

## A green run is not a delivered artifact

Do not end a side-effecting job on exit 0. Print what was produced — rows,
sha256, window, distinct keys — and assert the frozen things were not
touched:

```yaml
- name: Guard — frozen ledgers and S4 evidence untouched
  run: |
    set -u
    if ! git diff --quiet -- data/predictions/ data/shadow/ 2>/dev/null; then
      echo "::error::modified S4 evidence — it must not"; exit 1
    fi
```

## Before you call it done

- [ ] `validate` runs on push and nothing else does.
- [ ] Every side-effecting job is gated on `event_name == 'workflow_dispatch' && inputs.<x>`.
- [ ] You confirmed `skipped` on a real push run.
- [ ] Emitted data uploads as an artifact; nothing commits it.
- [ ] Inputs are fingerprinted before any number is computed from them.
- [ ] A missing input exits with a distinct code and names the fix.
- [ ] `data/` is asserted untouched.
- [ ] The job prints what it produced, not just that it finished.
