#!/usr/bin/env bash
# automerge-verify.sh: decide whether automerge.yml may merge a pipeline PR.
#
# Runs in a checkout of this repository with origin/if/main fetched. All
# checker code, policy and the bot's public key come from $TRUSTED, a
# separate checkout of if/main. The PR's own copies are never used.
#
# Environment:
#   PR           internal PR number (^[0-9]+$)
#   HEAD_SHA     PR head the build ran on (^[0-9a-f]{40}$)
#   HEAD_BRANCH  if/staging or claude/cherry-pick-pr-<N>
#   BOT_FPR      expected fingerprint of the bot's GPG key
#   TRUSTED      if/main checkout with tools/, .if-fork/config.yaml and
#                docs/bot-gpg-pubkey.asc
#   OUT          directory for result files
# For if/staging the upstream remote must have release/10.0 fetched.
#
# Writes $OUT/result:
#   ok       every check passed and the PR contains the if/main tip
#   behind   every check passed, but if/main has moved on (beyond ledger
#            commits); $OUT/upstream_pr and $OUT/upstream_head are set for a
#            cherry-pick branch so the caller can re-dispatch ingestion
#   refuse   a check failed; $OUT/reason says which
# Always exits 0 unless the inputs are malformed.
set -euo pipefail

[[ "$PR" =~ ^[0-9]+$ ]] || { echo "invalid PR" >&2; exit 1; }
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid HEAD_SHA" >&2; exit 1; }
mkdir -p "$OUT"

refuse() {
  echo refuse > "$OUT/result"
  echo "$1" > "$OUT/reason"
  echo "Refusing PR #$PR: $1"
  exit 0
}

git fetch -q --no-tags origin "+refs/pull/$PR/head:refs/remotes/pr/head"
[ "$(git rev-parse refs/remotes/pr/head)" = "$HEAD_SHA" ] || refuse "PR head is not the built commit $HEAD_SHA"
git checkout -q --detach "$HEAD_SHA"
MAIN=origin/if/main

# --- Bot signatures --------------------------------------------------------
GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap 'gpgconf --homedir "$GNUPGHOME" --kill all 2>/dev/null; rm -rf "$GNUPGHOME"' EXIT
gpg --batch --quiet --import "$TRUSTED/docs/bot-gpg-pubkey.asc" 2>/dev/null
KEY_FPR=$(gpg --batch --with-colons --fingerprint | awk -F: '/^fpr/ {print $10; exit}')
[ -n "${BOT_FPR:-}" ] && [ "$KEY_FPR" = "$BOT_FPR" ] \
  || refuse "bot public key in if/main does not match the expected fingerprint"
bot_signed() {
  git verify-commit --raw "$1" 2>&1 \
    | awk -v f="$KEY_FPR" '$2 == "VALIDSIG" && ($3 == f || $NF == f) {ok = 1} END {exit !ok}'
}

# Nobody reviews what the conflict agent writes, so a person merges it.
HUMAN_MERGE="a person must review and merge it (needs-human-merge)"
agent_resolved() {
  [ -n "$(git log -1 --format='%(trailers:key=Conflict-Resolved-By,valueonly)' "$1")" ]
}

# --- Protected paths and the file denylist ---------------------------------
git diff --name-only "$MAIN...HEAD" > "$OUT/changed.txt"
# Fork-owned paths: the pipeline's own workflows, code, policy, ledger, bot
# key, tests and packaging. Upstream dotnet/wpf has none of them (it has
# .github/, but the fork deleted upstream's workflows), so neither a
# cherry-pick nor the sync PR may change them without a person. Same list as
# the fork-owned part of file_denylist in .if-fork/config.yaml.
FORK_OWNED='^(\.github/|\.if-fork/|tools/|tests/|test/|docs/|perf/|data/|packaging/InitialForce\.[^/]*/|FORK\.md$|NOTICE\.md$|pyproject\.toml$|(.*/)?\.claude/|(.*/)?(CLAUDE\.md|CLAUDE\.local\.md|AGENTS\.md|\.mcp\.json)$)'
PROTECTED=$(grep -E "$FORK_OWNED" "$OUT/changed.txt" || true)
[ -z "$PROTECTED" ] || refuse "touches fork-owned files that need a human merge: $(echo "$PROTECTED" | tr '\n' ' ')"

deny() {
  python3 "$TRUSTED/tools/check-denylist.py" --config "$TRUSTED/.if-fork/config.yaml" \
    --changed-files "$1" > "$OUT/denylist.json" \
    || refuse "touches denylisted files: $(tr '\n' ' ' < "$1")"
}

case "$HEAD_BRANCH" in
  claude/cherry-pick-pr-*)
    BRANCH_PR="${HEAD_BRANCH#claude/cherry-pick-pr-}"
    [[ "$BRANCH_PR" =~ ^[0-9]+$ ]] || refuse "bad branch name $HEAD_BRANCH"
    [ "$(git rev-list --count "$MAIN..HEAD")" -eq 1 ] || refuse "cherry-pick PR must have exactly one commit"
    bot_signed HEAD || refuse "commit $HEAD_SHA is not signed by the bot key"
    agent_resolved HEAD && refuse "Claude resolved a conflict in this cherry-pick; $HUMAN_MERGE"
    UP_URL=$(git log -1 --format='%(trailers:key=Upstream-PR,valueonly)' HEAD | head -n1)
    UP_HEAD=$(git log -1 --format='%(trailers:key=Upstream-PR-Head,valueonly)' HEAD | head -n1)
    [ "$UP_URL" = "https://github.com/dotnet/wpf/pull/$BRANCH_PR" ] \
      || refuse "Upstream-PR trailer does not match branch $HEAD_BRANCH"
    [[ "$UP_HEAD" =~ ^[0-9a-f]{40}$ ]] || refuse "missing or bad Upstream-PR-Head trailer"
    # Allowlist: a cherry-pick merges by itself only when it stays inside
    # upstream's source and build folders (eng/ is still subject to
    # file_denylist below). Anything else goes to a person.
    OUTSIDE=$(grep -vE '^(src|eng)/' "$OUT/changed.txt" || true)
    [ -z "$OUTSIDE" ] || refuse "changes files outside src/ and eng/, which need a human merge: $(echo "$OUTSIDE" | tr '\n' ' ')"
    git show "$MAIN:.if-fork/patch-ledger.jsonl" > "$OUT/ledger.jsonl"
    python3 "$TRUSTED/tools/check-approval.py" --ledger "$OUT/ledger.jsonl" \
      --pr-number "$BRANCH_PR" --head-sha "$UP_HEAD" > "$OUT/approval.json" \
      || refuse "upstream #$BRANCH_PR at $UP_HEAD has no approved verdict in the ledger"
    deny "$OUT/changed.txt"
    echo "$BRANCH_PR" > "$OUT/upstream_pr"
    echo "$UP_HEAD" > "$OUT/upstream_head"
    ;;
  if/staging)
    UPSTREAM=upstream/release/10.0
    [ "$(git rev-list --parents -n 1 HEAD | wc -w)" -eq 3 ] || refuse "sync PR head is not a merge commit"
    bot_signed HEAD || refuse "merge commit $HEAD_SHA is not signed by the bot key"
    git merge-base --is-ancestor HEAD^2 "$UPSTREAM" || refuse "second parent is not upstream release/10.0"
    # Every other new commit must come from upstream.
    OTHER=$(git rev-list HEAD^1 HEAD^2 --not "$MAIN" "$UPSTREAM")
    [ -z "$OTHER" ] || refuse "commits that are neither upstream nor bot-signed: $(echo "$OTHER" | tr '\n' ' ')"
    # Files where the result differs from git's own merge are the conflict
    # resolutions. Kept fork deletions (absent from if/main and the result)
    # are not changes.
    AUTO=$(git merge-tree --write-tree HEAD^1 HEAD^2 | head -n1 || true)
    [[ "$AUTO" =~ ^[0-9a-f]{40}$ ]] || refuse "could not recreate the merge"
    git diff --name-only "$AUTO" HEAD | while read -r f; do
      if git cat-file -e "HEAD:$f" 2>/dev/null || git cat-file -e "HEAD^1:$f" 2>/dev/null; then
        echo "$f"
      fi
    done > "$OUT/resolved.txt"
    deny "$OUT/resolved.txt"
    if agent_resolved HEAD || [ -s "$OUT/resolved.txt" ]; then
      refuse "the sync merge holds conflict resolutions ($(tr '\n' ' ' < "$OUT/resolved.txt")); $HUMAN_MERGE"
    fi
    ;;
  *)
    refuse "branch $HEAD_BRANCH is not a pipeline branch"
    ;;
esac

# --- Bound to the tested base ----------------------------------------------
# Ledger commits land on if/main often; they do not change what was built.
MOVED=$(git diff --name-only "HEAD...$MAIN" | grep -vxF '.if-fork/patch-ledger.jsonl' || true)
if [ -n "$MOVED" ]; then
  echo behind > "$OUT/result"
  echo "if/main changed since the branch was made: $(echo "$MOVED" | head -n 20 | tr '\n' ' ')" > "$OUT/reason"
  exit 0
fi
echo ok > "$OUT/result"
