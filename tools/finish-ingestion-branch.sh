#!/usr/bin/env bash
# finish-ingestion-branch.sh: check and push a cherry-pick branch made by
# apply-upstream-pr.sh. Runs in a trusted job, before anything is pushed.
#
# Environment: PR_NUMBER, PINNED_HEAD_SHA, BRANCH, BASE_SHA (the pinned if/main
# commit the branch was made from), TRUSTED (if/main checkout whose tools and
# config are used), OUT (directory for result files), and CONFLICT_RESOLVED
# (true when the commit holds files the conflict agent wrote; the commit then
# gets a "Conflict-Resolved-By: claude" trailer, and automerge leaves the PR
# to a person).
# The current checkout must be on $BRANCH with exactly one commit on top of
# BASE_SHA.
#
# Writes $OUT/pushed (true) on success, or $OUT/result=refused and
# $OUT/reason when the branch is refused. Exits 0 in both cases.
set -euo pipefail

[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid BASE_SHA" >&2; exit 1; }
mkdir -p "$OUT"
refuse() {
  echo refused > "$OUT/result"
  echo "$1" > "$OUT/reason"
  echo "::error::Branch $BRANCH refused: $1"
  exit 0
}

if [ "$(git rev-list --count "$BASE_SHA..HEAD")" -ne 1 ]; then
  refuse not_exactly_one_commit
fi

# Run if/main's checker and policy on the whole branch diff, never the
# candidate's own copies.
git diff --name-only "$BASE_SHA...HEAD" > "$OUT/changed.txt"
if ! python3 "$TRUSTED/tools/check-denylist.py" --config "$TRUSTED/.if-fork/config.yaml" \
       --changed-files "$OUT/changed.txt" > "$OUT/denylist.json"; then
  cat "$OUT/denylist.json"
  refuse file_denylist
fi

TRAILERS=(--trailer "Upstream-PR: https://github.com/dotnet/wpf/pull/$PR_NUMBER"
          --trailer "Upstream-PR-Head: $PINNED_HEAD_SHA")
if [ "${CONFLICT_RESOLVED:-false}" = true ]; then
  TRAILERS+=(--trailer "Conflict-Resolved-By: claude")
fi
git commit --amend --no-edit "${TRAILERS[@]}"
git push --force-with-lease origin "HEAD:refs/heads/$BRANCH"
echo true > "$OUT/pushed"
