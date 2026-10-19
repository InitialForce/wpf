#!/usr/bin/env bash
# finish-ingestion-branch.sh: check and push a cherry-pick branch made by
# apply-upstream-pr.sh. Runs in a trusted job, before anything is pushed.
#
# Environment: PR_NUMBER, PINNED_HEAD_SHA, BRANCH, BASE_SHA (the pinned if/main
# commit the branch was made from), TRUSTED (if/main checkout whose tools and
# config are used), OUT (directory for result files), and CONFLICT_RESOLVED
# (true when the commit holds files the conflict agent wrote; the commit then
# gets a "Conflict-Resolved-By: claude" trailer, and automerge merges the PR
# only after a resolution review approves that exact commit).
# The current checkout must be on $BRANCH with exactly one commit on top of
# BASE_SHA.
#
# Writes $OUT/pushed (true) and $OUT/sha (the pushed commit) on success, or
# $OUT/result=refused and $OUT/reason when the branch is refused (reasons:
# not_exactly_one_commit, file_denylist, fork_owned_paths,
# paths_outside_allowlist, path_policy_error). Exits 0 in both cases.
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
# The same path rules automerge-verify.sh applies. A branch automerge would
# refuse for its paths is never pushed: ingestion records escalated with the
# reason (fork_owned_paths or paths_outside_allowlist) and opens no PR.
if ! python3 "$TRUSTED/tools/path-policy.py" --kind cherry-pick \
       --changed-files "$OUT/changed.txt" > "$OUT/path-policy.json"; then
  cat "$OUT/path-policy.json"
  REASON=$(jq -r '.reason' "$OUT/path-policy.json")
  [[ "$REASON" =~ ^(fork_owned_paths|paths_outside_allowlist)$ ]] || REASON=path_policy_error
  refuse "$REASON"
fi

TRAILERS=(--trailer "Upstream-PR: https://github.com/dotnet/wpf/pull/$PR_NUMBER"
          --trailer "Upstream-PR-Head: $PINNED_HEAD_SHA")
if [ "${CONFLICT_RESOLVED:-false}" = true ]; then
  TRAILERS+=(--trailer "Conflict-Resolved-By: claude")
fi
git commit --amend --no-edit "${TRAILERS[@]}"
git push --force-with-lease origin "HEAD:refs/heads/$BRANCH"
git rev-parse HEAD > "$OUT/sha"
echo true > "$OUT/pushed"
