#!/usr/bin/env bash
# human-merge-label.sh <pr> <true|false>: mark a pipeline PR that a person must
# merge, or clear the mark.
#
# true adds the needs-human-merge label (and creates the label when the
# repository does not have it yet); false removes it. automerge.yml does not
# read the label: it refuses such PRs by their Conflict-Resolved-By trailer.
# The label is for people.
#
# Environment: GH_TOKEN, GITHUB_REPOSITORY.
set -euo pipefail

pr="$1"; needed="$2"
[[ "$pr" =~ ^[0-9]+$ ]] || { echo "invalid PR number" >&2; exit 1; }

if [ "$needed" = true ]; then
  gh label create needs-human-merge --repo "$GITHUB_REPOSITORY" --color B60205 \
    --description "A person must review and merge this PR; automerge will not" 2>/dev/null || true
  gh pr edit "$pr" --repo "$GITHUB_REPOSITORY" --add-label needs-human-merge
else
  gh pr edit "$pr" --repo "$GITHUB_REPOSITORY" --remove-label needs-human-merge 2>/dev/null || true
fi
