#!/usr/bin/env bash
# ingestion-escalate.sh <reason>: end pr-ingestion for this upstream PR head.
#
# Clears any half-done cherry-pick, records an 'escalated' ledger event on
# if/main with if/main's own ledger-event.py and writes a step summary.
#
# A designed stop (a gate refused the change: denylist, path policy, head
# moved, the resolver's hard-fail or injection report) is a normal outcome:
# it ends with a ::notice:: and exit 0, so the run stays green and Claude On
# Failure opens no "CI failure" issue. Only a reason that means the pipeline
# itself broke (apply_failed, reapply_mismatch, unknown) ends with exit 1.
#
# Environment: PR_NUMBER, PINNED_HEAD_SHA, TRUSTED (if/main checkout), and the
# GitHub run variables. Runs in the work checkout with the bot key imported.
set -euo pipefail

REASON="$1"
[[ "$REASON" =~ ^[a-z_]+$ ]] || REASON=unknown
RUN_URL="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"

case "$REASON" in
  apply_failed|reapply_mismatch|unknown) FAILED=true ;;
  *) FAILED=false ;;
esac

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
if [ "$FAILED" = true ]; then
  echo "::error::Upstream PR #$PR_NUMBER could not be applied: $REASON"
  exit 1
fi
echo "::notice::Upstream PR #$PR_NUMBER was not applied: $REASON (recorded as escalated)"
