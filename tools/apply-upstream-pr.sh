#!/usr/bin/env bash
# apply-upstream-pr.sh: apply one reviewed upstream dotnet/wpf PR onto a branch
# made from a pinned if/main commit, without any model involvement.
#
# Used by pr-ingestion.yml in three jobs (cherry-pick, resolve-conflicts,
# finalize). Running it again on the same inputs gives the same result, which
# is how the conflict resolver's output is handed back to a trusted job.
#
# Environment (all required):
#   PR_NUMBER        upstream PR number (^[0-9]+$)
#   PINNED_HEAD_SHA  reviewed upstream head (^[0-9a-f]{40}$)
#   BASE_SHA         if/main commit to branch from (^[0-9a-f]{40}$). Every job
#                    of one run uses the same commit, so the trusted job
#                    recreates exactly the state the resolver saw.
#   BRANCH           local branch to create, e.g. claude/cherry-pick-pr-123
#   OUT              directory for result files
#   TRUSTED          checkout of if/main whose tools and config are used
#   GH_TOKEN         token for read-only GitHub API calls
# Expects remotes: origin (with BASE_SHA fetched) and upstream.
#
# Writes to $OUT:
#   result     applied | conflict | empty | refused
#   reason     why it was refused (only for refused)
#   title      upstream PR title
#   source     upstream commit that was cherry-picked
#   conflicts.txt  conflicted paths (only for conflict)
set -euo pipefail

[[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || { echo "invalid PR_NUMBER" >&2; exit 1; }
[[ "$PINNED_HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid PINNED_HEAD_SHA" >&2; exit 1; }
[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid BASE_SHA" >&2; exit 1; }
mkdir -p "$OUT"

refuse() {
  echo refused > "$OUT/result"
  echo "$1" > "$OUT/reason"
  echo "::error::Upstream PR #$PR_NUMBER refused: $1"
  exit 0
}

gh api "repos/dotnet/wpf/pulls/$PR_NUMBER" > "$OUT/upr.json"
UP_HEAD=$(jq -r .head.sha "$OUT/upr.json")
MERGED=$(jq -r .merged "$OUT/upr.json")
MERGE_SHA=$(jq -r '.merge_commit_sha // empty' "$OUT/upr.json")
AUTHOR=$(jq -r .user.login "$OUT/upr.json")
# One line only: the title goes into GITHUB_OUTPUT, where a newline would
# start a new key.
jq -r .title "$OUT/upr.json" | tr -d '\r\n' | head -c 200 > "$OUT/title"
TITLE=$(cat "$OUT/title")

if [ "$UP_HEAD" != "$PINNED_HEAD_SHA" ]; then
  refuse head_moved_after_review
fi

git fetch --no-tags upstream main "+refs/pull/${PR_NUMBER}/head:refs/remotes/pr/${PR_NUMBER}"
if [ "$MERGED" = "true" ] && [ -n "$MERGE_SHA" ]; then
  git fetch --no-tags upstream "$MERGE_SHA"
  SOURCE="$MERGE_SHA"
else
  SOURCE="$PINNED_HEAD_SHA"
fi
echo "$SOURCE" > "$OUT/source"

for sha in "$SOURCE" "$PINNED_HEAD_SHA"; do
  if ! python3 "$TRUSTED/tools/check-commit-denylist.py" "$sha" \
         --config "$TRUSTED/.if-fork/config.yaml" >&2; then
    refuse commit_denylist
  fi
done

git checkout -q -B "$BRANCH" "$BASE_SHA"
PICK_ARGS=(-x)
if [ "$SOURCE" != "$MERGE_SHA" ]; then
  # Squash the open PR into one synthetic commit on its upstream base so
  # cherry-pick can do a real 3-way merge, including modify/delete conflicts.
  BASE=$(git merge-base "$PINNED_HEAD_SHA" upstream/main)
  SOURCE=$(GIT_AUTHOR_NAME="$AUTHOR" GIT_AUTHOR_EMAIL="$AUTHOR@users.noreply.github.com" \
    git commit-tree "$PINNED_HEAD_SHA^{tree}" -p "$BASE" -m "$TITLE (dotnet/wpf#$PR_NUMBER)")
  PICK_ARGS=()
elif [ "$(git rev-list --parents -n 1 "$SOURCE" | wc -w)" -gt 2 ]; then
  PICK_ARGS+=(-m 1)
fi

if git cherry-pick "${PICK_ARGS[@]}" "$SOURCE"; then
  echo applied > "$OUT/result"
  exit 0
fi

git diff --name-only --diff-filter=U > "$OUT/conflicts.txt"
if [ -s "$OUT/conflicts.txt" ]; then
  echo conflict > "$OUT/result"
elif git diff --cached --quiet; then
  git cherry-pick --abort 2>/dev/null || true
  echo empty > "$OUT/result"
else
  git cherry-pick --abort 2>/dev/null || true
  refuse apply_failed
fi
