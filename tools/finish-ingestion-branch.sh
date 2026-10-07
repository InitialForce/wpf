#!/usr/bin/env bash
# finish-ingestion-branch.sh: check and push a cherry-pick branch made by
# apply-upstream-pr.sh. Runs in a trusted job, before anything is pushed.
#
# Environment: PR_NUMBER, PINNED_HEAD_SHA, BRANCH, TRUSTED (if/main checkout
# whose tools and config are used), OUT (directory for result files).
# The current checkout must be on $BRANCH with exactly one commit on top of
# origin/if/main.
#
# Writes $OUT/pushed (true) on success, or $OUT/result=refused and
# $OUT/reason when the branch is refused. Exits 0 in both cases.
set -euo pipefail

mkdir -p "$OUT"
refuse() {
  echo refused > "$OUT/result"
  echo "$1" > "$OUT/reason"
  echo "::error::Branch $BRANCH refused: $1"
  exit 0
}

if [ "$(git rev-list --count origin/if/main..HEAD)" -ne 1 ]; then
  refuse not_exactly_one_commit
fi

# Run if/main's checker and policy on the whole branch diff, never the
# candidate's own copies.
git diff --name-only origin/if/main...HEAD > "$OUT/changed.txt"
if ! python3 "$TRUSTED/tools/check-denylist.py" --config "$TRUSTED/.if-fork/config.yaml" \
       --changed-files "$OUT/changed.txt" > "$OUT/denylist.json"; then
  cat "$OUT/denylist.json"
  refuse file_denylist
fi

git commit --amend --no-edit \
  --trailer "Upstream-PR: https://github.com/dotnet/wpf/pull/$PR_NUMBER" \
  --trailer "Upstream-PR-Head: $PINNED_HEAD_SHA"
git push --force-with-lease origin "HEAD:refs/heads/$BRANCH"
echo true > "$OUT/pushed"
