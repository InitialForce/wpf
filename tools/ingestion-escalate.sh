#!/usr/bin/env bash
# ingestion-escalate.sh <reason>: stop pr-ingestion for a human.
#
# Clears any half-done cherry-pick, records an 'escalated' ledger event on
# if/main with if/main's own ledger-event.py, writes a step summary and exits 1.
#
# Environment: PR_NUMBER, PINNED_HEAD_SHA, TRUSTED (if/main checkout), and the
# GitHub run variables. Runs in the work checkout with the bot key imported.
set -euo pipefail

REASON="$1"
[[ "$REASON" =~ ^[a-z_]+$ ]] || REASON=unknown
RUN_URL="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"

git cherry-pick --abort 2>/dev/null || true
git reset -q --hard
git checkout -q -B if/main origin/if/main
python3 "$TRUSTED/tools/ledger-event.py" \
  --event escalated \
  --pr-number "$PR_NUMBER" \
  --head-sha "$PINNED_HEAD_SHA" \
  --actor pr-ingestion \
  --details-json "{\"reason\":\"$REASON\",\"run_url\":\"$RUN_URL\"}" \
  --push
{
  echo "### Upstream PR #$PR_NUMBER was not applied"
  echo
  echo "Reason: \`$REASON\`"
  cat "$RUNNER_TEMP/resolution/escalation.md" 2>/dev/null || true
} >> "$GITHUB_STEP_SUMMARY"
echo "::error::Upstream PR #$PR_NUMBER needs a human: $REASON"
exit 1
