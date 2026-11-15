#!/usr/bin/env bash
# Publish the agents' output directories to one branch, append-only.
#
#   publish_agent_branch.sh <branch> <dir> [<dir> ...]
#
# WHY THIS IS A SCRIPT AND NOT TWELVE LINES OF YAML
#
# The first version lived inline in observer.yml and did
#     git checkout observations
# in the job's working tree. On night one the branch did not exist, so the
# orphan path ran and it worked. On night two the branch DID exist, the
# working tree already held an untracked observations/cik_cache.json fetched
# at the start of the job, and git refused:
#
#     error: The following untracked working tree files would be
#     overwritten by checkout: observations/cik_cache.json
#
# So the job collected a full day of filings and then failed to publish them
# -- every night after the first. Nothing was published for three days and
# the run was red in a list nobody was watching.
#
# Three changes, each closing a way that could happen again:
#
#   1. A SEPARATE WORKTREE. The branch is checked out in a temp directory,
#      never in the job's working tree, so there is nothing to collide with.
#   2. NO FORCE PUSH. The old step recreated an orphan and force-pushed it.
#      This appends a commit, so the branch's own history is a record of
#      what was published when, and a bad run cannot erase a good one.
#   3. SHRINK DETECTION. A day file that already exists on the branch may
#      grow and may not get shorter. A ledger that got shorter is an
#      incident, not a diff -- the publish is refused and the branch is
#      left exactly as it was.
#
# It lives in a file so it can be run against a throwaway remote before it
# is trusted with the real one. The inline version never was.
set -euo pipefail

BRANCH="${1:?usage: publish_agent_branch.sh <branch> <dir> [<dir> ...]}"
shift
[ "$#" -ge 1 ] || { echo "::error::publish: no directories given"; exit 2; }

ROOT="$(git rev-parse --show-toplevel)"
WT="$(mktemp -d)"
trap 'git -C "$ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true' EXIT

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  git fetch -q origin "$BRANCH"
  git worktree add -q --detach "$WT" FETCH_HEAD
  echo "publish: appending to existing branch '$BRANCH'"
else
  git worktree add -q --detach "$WT" HEAD
  git -C "$WT" checkout -q --orphan "$BRANCH"
  git -C "$WT" rm -rqf . >/dev/null 2>&1 || true
  find "$WT" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  echo "publish: creating branch '$BRANCH'"
fi

# Line counts of every day file already published, before anything is copied.
BEFORE="$(mktemp)"
( cd "$WT" && find . -name '*.jsonl' -type f | sort | while read -r f; do
    printf '%s\t%s\n' "$(wc -l < "$f" | tr -d ' ')" "$f"
  done ) > "$BEFORE"

copied=0
for d in "$@"; do
  if [ -d "$ROOT/$d" ]; then
    mkdir -p "$WT/$d"
    cp -r "$ROOT/$d/." "$WT/$d/"
    copied=$((copied + 1))
  else
    echo "publish: '$d' was not produced this run — left as published"
  fi
done
[ "$copied" -ge 1 ] || { echo "::error::publish: none of the directories exist — nothing was produced"; exit 3; }

# 🔑 Shrink detection.
shrunk=0
while IFS=$'\t' read -r n f; do
  [ -n "$f" ] || continue
  if [ ! -f "$WT/$f" ]; then
    echo "::error::publish: $f was published and is now MISSING"; shrunk=1; continue
  fi
  now="$(wc -l < "$WT/$f" | tr -d ' ')"
  if [ "$now" -lt "$n" ]; then
    echo "::error::publish: $f SHRANK from $n to $now records — an append-only day file got shorter"
    shrunk=1
  fi
done < "$BEFORE"
if [ "$shrunk" -ne 0 ]; then
  echo "::error::publish: refusing to publish. The branch is unchanged."
  exit 4
fi

cd "$WT"
git add -A
if git diff --cached --quiet; then
  echo "publish: nothing new to commit"
else
  git commit -qm "agent $(date -u +%F) — $(git diff --cached --name-only | wc -l | tr -d ' ') file(s)"
  git push -q origin "HEAD:refs/heads/$BRANCH"
  echo "publish: pushed $(git rev-parse --short HEAD) to '$BRANCH'"
fi
