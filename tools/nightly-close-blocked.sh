#!/usr/bin/env bash
# nightly-close-blocked.sh: close the open "Nightly upstream sync blocked"
# issue after a sync worked or if/main already has upstream.
#
# Environment: GH_TOKEN, BLOCKED_ISSUE_TITLE and the GitHub run variables.
set -euo pipefail

for n in $(gh issue list --repo "$GITHUB_REPOSITORY" --state open --label automated \
             --search "\"$BLOCKED_ISSUE_TITLE\" in:title" --json number --jq '.[].number'); do
  gh issue close "$n" --repo "$GITHUB_REPOSITORY" \
    --comment "Upstream sync succeeded in $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID."
done
