#!/usr/bin/env bash
# nightly-merge.sh: merge an upstream release/10.0 commit into a fresh
# if/staging made from origin/if/main, without committing.
#
# Used by nightly-rebase.yml in three jobs (sync, resolve-conflicts,
# finalize). Running it again on the same inputs gives the same result, which
# is how the conflict resolver's output is handed back to a trusted job.
#
# Environment: UPSTREAM_SHA (^[0-9a-f]{40}$), OUT (directory for results).
# Expects origin/if/main and the upstream commit to be fetched.
# Writes $OUT/conflicts.txt (empty when the merge is clean).
set -euo pipefail

[[ "$UPSTREAM_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid UPSTREAM_SHA" >&2; exit 1; }
mkdir -p "$OUT"

git checkout -q -B if/staging origin/if/main
git merge --no-ff --no-commit "$UPSTREAM_SHA" || true

# The fork deliberately deleted upstream's GitHub workflows and CODEOWNERS.
# When upstream edits one of those, keep the deletion.
git diff --name-only --diff-filter=U | while read -r f; do
  case "$f" in
    .github/*|CODEOWNERS)
      if ! git cat-file -e "HEAD:$f" 2>/dev/null; then
        git rm -q --cached -- "$f" && rm -f -- "$f"
        echo "Kept fork deletion of $f" >&2
      fi
      ;;
  esac
done

git diff --name-only --diff-filter=U > "$OUT/conflicts.txt"
