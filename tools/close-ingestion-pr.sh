#!/usr/bin/env bash
# close-ingestion-pr.sh <upstream pr> <reason>: close the fork PR that
# ingestion opened for an upstream PR once ingestion has ended without it.
#
# Called when an upstream PR is escalated, given up, or recorded as already in
# the fork (graduated_upstream). A PR is touched only when it is the
# pipeline's own: base if/main, head branch claude/cherry-pick-pr-<upstream
# pr> in this repository (not a fork), opened by BOT_LOGIN, labelled
# automated (the same checks automerge-run.sh makes). Such a PR loses the
# needs-human-merge label, gets a comment with <reason> and the run link, and
# is closed. Its branch is deleted only while it still points at the PR's
# head commit. Nothing that matches: nothing to do.
#
# Never fails the calling step: a GitHub error is a ::warning::, and the
# discovery sweep (tools/stale-ingestion-prs.py) closes what is left later.
#
# Environment: GH_TOKEN, GITHUB_REPOSITORY, the GitHub run variables, and
# optionally BOT_LOGIN (default: bot_identity.github_login from the
# config.yaml next to this script, which is if/main's).
set -uo pipefail

pr="$1"; reason="$2"
[[ "$pr" =~ ^[0-9]+$ ]] || { echo "::warning::close-ingestion-pr: invalid PR number" >&2; exit 0; }
if [ -z "${BOT_LOGIN:-}" ]; then
  config="$(dirname "${BASH_SOURCE[0]}")/../.if-fork/config.yaml"
  BOT_LOGIN=$(sed -n 's/^  github_login: *"\{0,1\}\([a-z0-9-]*\[bot\]\)"\{0,1\} *$/\1/p' "$config" 2>/dev/null)
fi
if [ -z "${GH_TOKEN:-}" ] || [ -z "${BOT_LOGIN:-}" ]; then
  echo "::warning::close-ingestion-pr: no GH_TOKEN or BOT_LOGIN; open PRs for upstream #$pr are left for the discovery sweep"
  exit 0
fi
branch="claude/cherry-pick-pr-$pr"
run_url="${GITHUB_SERVER_URL:-https://github.com}/$GITHUB_REPOSITORY/actions/runs/${GITHUB_RUN_ID:-0}"
pr_json=$(mktemp)
trap 'rm -f "$pr_json"' EXIT

if ! open=$(gh pr list --repo "$GITHUB_REPOSITORY" --state open --head "$branch" \
              --base if/main --json number --jq '.[].number'); then
  echo "::warning::close-ingestion-pr: could not list PRs for $branch"
  exit 0
fi
for n in $open; do
  [[ "$n" =~ ^[0-9]+$ ]] || continue
  # --head matches the branch name in any fork, so check the PR itself.
  if ! gh api "repos/$GITHUB_REPOSITORY/pulls/$n" > "$pr_json"; then
    echo "::warning::close-ingestion-pr: could not read PR #$n"; continue
  fi
  why=$(jq -r --arg repo "$GITHUB_REPOSITORY" --arg bot "$BOT_LOGIN" --arg branch "$branch" '
    if .state != "open" then "not open"
    elif .base.ref != "if/main" then "base is not if/main"
    elif .head.repo.full_name != $repo then "head is not in \($repo)"
    elif .head.ref != $branch then "head branch is not \($branch)"
    elif .user.login != $bot then "opened by \(.user.login), not \($bot)"
    elif ([.labels[].name] | index("automated")) == null then "no automated label"
    else "" end' "$pr_json")
  if [ -n "$why" ]; then
    echo "::notice::Not closing PR #$n: $why"; continue
  fi
  head_sha=$(jq -r .head.sha "$pr_json")
  gh pr edit "$n" --repo "$GITHUB_REPOSITORY" --remove-label needs-human-merge >/dev/null 2>&1 || true
  if ! gh pr close "$n" --repo "$GITHUB_REPOSITORY" \
       --comment "Ingestion of upstream dotnet/wpf#$pr ended without this PR: $reason. Closed automatically. Run: $run_url"; then
    echo "::warning::close-ingestion-pr: could not close PR #$n"; continue
  fi
  echo "::notice::Closed PR #$n for upstream #$pr ($reason)"
  ref_sha=$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/$branch" --jq .object.sha 2>/dev/null || true)
  if [[ "$head_sha" =~ ^[0-9a-f]{40}$ ]] && [ "$ref_sha" = "$head_sha" ]; then
    gh api -X DELETE "repos/$GITHUB_REPOSITORY/git/refs/heads/$branch" >/dev/null \
      || echo "::warning::close-ingestion-pr: could not delete $branch"
  else
    echo "::notice::Kept $branch: it does not point at PR #$n's head"
  fi
done
exit 0
