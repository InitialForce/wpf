#!/usr/bin/env bash
# close-ingestion-pr.sh <upstream pr> <reason>: close the fork PR that
# ingestion opened for an upstream PR once ingestion has ended without it.
#
# Called when an upstream PR is escalated, given up, or recorded as already in
# the fork (graduated_upstream, including a resolution that keeps the fork).
# Every open PR from claude/cherry-pick-pr-<upstream pr> into if/main loses
# the needs-human-merge label, gets a comment with <reason> and the run link,
# and is closed with its branch deleted. Nothing is open: nothing to do.
#
# Never fails the calling step: a GitHub error is a ::warning::, and the
# discovery sweep (tools/stale-ingestion-prs.py) closes what is left later.
#
# Environment: GH_TOKEN, GITHUB_REPOSITORY, and the GitHub run variables.
set -uo pipefail

pr="$1"; reason="$2"
[[ "$pr" =~ ^[0-9]+$ ]] || { echo "::warning::close-ingestion-pr: invalid PR number" >&2; exit 0; }
if [ -z "${GH_TOKEN:-}" ]; then
  echo "::warning::close-ingestion-pr: no GH_TOKEN; open PRs for upstream #$pr are left for the discovery sweep"
  exit 0
fi
branch="claude/cherry-pick-pr-$pr"
run_url="${GITHUB_SERVER_URL:-https://github.com}/$GITHUB_REPOSITORY/actions/runs/${GITHUB_RUN_ID:-0}"

if ! open=$(gh pr list --repo "$GITHUB_REPOSITORY" --state open --head "$branch" \
              --base if/main --json number --jq '.[].number'); then
  echo "::warning::close-ingestion-pr: could not list PRs for $branch"
  exit 0
fi
for n in $open; do
  [[ "$n" =~ ^[0-9]+$ ]] || continue
  gh pr edit "$n" --repo "$GITHUB_REPOSITORY" --remove-label needs-human-merge >/dev/null 2>&1 || true
  if gh pr close "$n" --repo "$GITHUB_REPOSITORY" --delete-branch \
       --comment "Ingestion of upstream dotnet/wpf#$pr ended without this PR: $reason. Closed automatically. Run: $run_url"; then
    echo "::notice::Closed PR #$n for upstream #$pr ($reason)"
  else
    echo "::warning::close-ingestion-pr: could not close PR #$n"
  fi
done
exit 0
