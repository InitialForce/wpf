#!/usr/bin/env bash
# automerge-run.sh: process every open pipeline PR to if/main whose Build has
# passed on its current head. Called by automerge.yml on each Build
# completion, every 30 minutes, and by hand. Each run looks at all PRs, so a
# run that GitHub drops from the concurrency queue loses nothing: the next
# run picks the PR up.
#
# Per PR:
#   - eligibility: open, base if/main, head in this repository, opened by the
#     App's bot account, label "automated", branch if/staging or
#     claude/cherry-pick-pr-<N>, and the newest pull_request Build run for
#     the head commit concluded "success"
#   - tools/automerge-verify.sh from $TRUSTED decides ok, behind or refuse
#   - ok: right before merging, fetch if/main again and stop if it moved
#     since verification; otherwise merge with a merge commit, pinned to the
#     verified head. GitHub has no "merge only if the base is still X" call,
#     so a push to if/main in the second between that fetch and the merge
#     call is not caught here; the next Build of if/main is.
#   - behind: ask the workflow that made the branch to rebuild it, once per
#     PR head (a marker comment records the request)
#   - refuse: say why, once per PR head (marker comment)
#
# Environment:
#   GH_TOKEN, GITHUB_REPOSITORY  App token and repository
#   BOT_LOGIN    the App's bot login, e.g. my-app[bot]
#   BOT_FPR      expected fingerprint of the bot's GPG key
#   TRUSTED      if/main checkout whose tools/ are used
#   OUT          directory for per-PR result files
#   PROCEED      "true" when the autonomy gate allows merging; otherwise
#                PRs are checked and reported but not merged or rebuilt
#   GATE_REASON  the gate's reason, for the report
# Runs in a full clone of this repository (origin = this repository).
# Exits 1 when any PR could not be processed; the others are still handled.
set -uo pipefail

mkdir -p "$OUT"
FAILED=0

comment_once() { # <pr> <marker> <body>
  local pr="$1" marker="$2" body="$3"
  if gh api "repos/$GITHUB_REPOSITORY/issues/$pr/comments" --paginate --jq '.[].body' \
       | grep -qF -- "$marker"; then
    return 0
  fi
  gh pr comment "$pr" --repo "$GITHUB_REPOSITORY" --body "$marker
$body"
}

build_conclusion() { # <sha>
  gh api "repos/$GITHUB_REPOSITORY/actions/workflows/build.yml/runs?event=pull_request&head_sha=$1&per_page=1" \
    --jq '.workflow_runs[0] | select(.status == "completed") | .conclusion'
}

process() { # <pr> <head sha> <head branch>
  local pr="$1" sha="$2" branch="$3" out="$OUT/$1" reason result verified now
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::PR #$pr: bad head SHA"; return 1; }
  [[ "$branch" == if/staging || "$branch" =~ ^claude/cherry-pick-pr-[0-9]+$ ]] || return 0

  gh api "repos/$GITHUB_REPOSITORY/pulls/$pr" > "$OUT/pr-$pr.json" || return 1
  reason=$(jq -r --arg repo "$GITHUB_REPOSITORY" --arg sha "$sha" --arg bot "$BOT_LOGIN" '
    if .state != "open" then "PR is not open"
    elif .base.ref != "if/main" then "base is \(.base.ref), not if/main"
    elif .head.repo.full_name != $repo then "head is not in this repository"
    elif .user.login != $bot then "PR was opened by \(.user.login), not \($bot)"
    elif ([.labels[].name] | index("automated")) == null then "PR has no automated label"
    elif .head.sha != $sha then "PR head moved to \(.head.sha)"
    else "" end' "$OUT/pr-$pr.json") || return 1
  if [ -n "$reason" ]; then
    echo "::notice::Not merging PR #$pr: $reason"
    return 0
  fi
  if [ "$(build_conclusion "$sha")" != success ]; then
    echo "::notice::PR #$pr: no passed Build for $sha yet."
    return 0
  fi

  git fetch -q origin if/main || return 1
  if [ "$branch" = if/staging ]; then
    git fetch -q --no-tags upstream release/10.0 || return 1
  fi
  verified=$(git rev-parse origin/if/main)
  PR="$pr" HEAD_SHA="$sha" HEAD_BRANCH="$branch" OUT="$out" \
    bash "$TRUSTED/tools/automerge-verify.sh" || return 1
  result=$(cat "$out/result")
  reason=$(cat "$out/reason" 2>/dev/null || true)

  case "$result" in
    ok)
      if [ "$PROCEED" != true ]; then
        echo "::notice::Build passed on PR #$pr but auto-merge is blocked ($GATE_REASON). Merge it by hand."
        return 0
      fi
      git fetch -q origin if/main || return 1
      now=$(git rev-parse origin/if/main)
      if [ "$now" != "$verified" ]; then
        echo "::notice::PR #$pr: if/main moved from $verified to $now during verification. Not merging; the next run checks again."
        return 0
      fi
      gh pr merge "$pr" --repo "$GITHUB_REPOSITORY" --merge --match-head-commit "$sha" || return 1
      echo "::notice::Merged PR #$pr at $sha onto if/main $verified."
      ;;
    behind)
      echo "::notice::PR #$pr is behind if/main: $reason"
      [ "$PROCEED" = true ] || return 0
      if gh api "repos/$GITHUB_REPOSITORY/issues/$pr/comments" --paginate --jq '.[].body' \
           | grep -qF -- "<!-- automerge-rebuild:$sha -->"; then
        echo "::notice::Rebuild of PR #$pr at $sha was already requested."
        return 0
      fi
      if [ "$branch" = if/staging ]; then
        jq -n '{event_type: "nightly-rebase", client_payload: {force: true}}' \
          | gh api "repos/$GITHUB_REPOSITORY/dispatches" --input - || return 1
      else
        jq -n --argjson pr "$(cat "$out/upstream_pr")" --arg sha "$(cat "$out/upstream_head")" \
          '{event_type: "pr-ingestion-requested", client_payload: {pr_number: $pr, head_sha: $sha}}' \
          | gh api "repos/$GITHUB_REPOSITORY/dispatches" --input - || return 1
      fi
      comment_once "$pr" "<!-- automerge-rebuild:$sha -->" \
        "if/main has moved on since this branch was made, so automerge asked for it to be rebuilt on the new if/main. The new build merges on its own when it passes." \
        || return 1
      ;;
    refuse)
      echo "::notice::Not merging PR #$pr: $reason"
      comment_once "$pr" "<!-- automerge-refused:$sha -->" \
        "automerge will not merge this PR at \`$sha\`: $reason" || return 1
      ;;
    *)
      echo "::error::PR #$pr: unknown verify result '$result'"
      return 1
      ;;
  esac
}

gh pr list --repo "$GITHUB_REPOSITORY" --state open --base if/main --label automated \
  --limit 100 --json number,headRefOid,headRefName \
  --jq '.[] | "\(.number) \(.headRefOid) \(.headRefName)"' > "$OUT/candidates.txt" \
  || { echo "::error::Could not list PRs."; exit 1; }

# fd 3, so nothing inside process() can read the list from stdin.
while read -r pr sha branch <&3; do
  [ -z "$pr" ] && continue
  if ! process "$pr" "$sha" "$branch"; then
    echo "::error::PR #$pr could not be processed."
    FAILED=1
  fi
done 3< "$OUT/candidates.txt"

exit "$FAILED"
