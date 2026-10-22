#!/usr/bin/env bash
# ingestion-escalate.sh <reason>: end pr-ingestion for this upstream PR head.
#
# Clears any half-done cherry-pick and writes a step summary. What follows
# depends on the reason:
#
#   A designed refusal (DESIGNED_REFUSALS: a gate refused the change, or the
#   resolver reported a hard-fail pattern or a suspected injection) records
#   an 'escalated' ledger event on if/main with if/main's own ledger-event.py,
#   closes any open fork PR for this upstream PR (tools/close-ingestion-pr.sh)
#   and ends with a ::notice:: and exit 0. The run stays green and Claude On
#   Failure opens no issue.
#
#   Every other reason means the pipeline failed: a git or resolver failure,
#   an incomplete or invalid resolution, a path-policy error, an unexpected
#   commit count, or a reason this list does not know. It ends with ::error::
#   and exit 1, and records nothing in the ledger, so the pair stays
#   retryable: pr-discovery re-sends it as a lost handoff (at most 3 times).
#
# Environment: PR_NUMBER, PINNED_HEAD_SHA, TRUSTED (if/main checkout), GH_TOKEN
# (App token, for closing the PR), and the GitHub run variables. Runs in the
# work checkout with the bot key imported.
set -euo pipefail

DESIGNED_REFUSALS=(
  head_moved_after_review   # the upstream head is not the reviewed one
  commit_denylist           # config commit_denylist
  conflict_in_denylist      # a conflict on a file_denylist path
  file_denylist             # the branch diff touches file_denylist
  fork_owned_paths          # path-policy.py: a fork-owned path
  paths_outside_allowlist   # path-policy.py: outside src/ and eng/
  resolver_refused          # the resolver's escalation file (hard-fail, injection)
)

REASON="$1"
[[ "$REASON" =~ ^[a-z_]+$ ]] || REASON=unknown
RUN_URL="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"

DESIGNED=false
for r in "${DESIGNED_REFUSALS[@]}"; do
  [ "$r" = "$REASON" ] && DESIGNED=true
done

git cherry-pick --abort 2>/dev/null || true
git reset -q --hard
git checkout -q -B if/main origin/if/main
{
  echo "### Upstream PR #$PR_NUMBER was not applied"
  echo
  echo "Reason: \`$REASON\`"
  cat "$RUNNER_TEMP/resolution/escalation.md" 2>/dev/null || true
} >> "$GITHUB_STEP_SUMMARY"

if [ "$DESIGNED" != true ]; then
  echo "::error::Upstream PR #$PR_NUMBER failed in the pipeline ($REASON). Nothing recorded; pr-discovery retries it."
  exit 1
fi
python3 "$TRUSTED/tools/ledger-event.py" \
  --event escalated \
  --pr-number "$PR_NUMBER" \
  --head-sha "$PINNED_HEAD_SHA" \
  --actor pr-ingestion \
  --details-json "{\"reason\":\"$REASON\",\"run_url\":\"$RUN_URL\"}" \
  --push
bash "$TRUSTED/tools/close-ingestion-pr.sh" "$PR_NUMBER" "escalated ($REASON)"
echo "::notice::Upstream PR #$PR_NUMBER was not applied: $REASON (recorded as escalated)"
