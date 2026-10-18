#!/usr/bin/env bash
# prepare-resolver.sh <conflicts.txt> <input dir>: get the conflict agent's job
# ready after the conflict has been recreated and before Claude starts.
#
# Used by the resolve-conflicts jobs of pr-ingestion.yml and nightly-rebase.yml.
# Run it from a copy outside the work tree, in the root of the work tree.
#
#   - Refuses when the work tree holds files that would configure Claude Code:
#     .claude/, .mcp.json, CLAUDE.md, CLAUDE.local.md or AGENTS.md, at any depth.
#     The workflow also passes --setting-sources user and --strict-mcp-config,
#     so this is a second line.
#   - Refuses when a conflicted path has a character that a permission rule
#     cannot hold safely (anything outside A-Z a-z 0-9 . _ / + -).
#   - Writes, for each conflicted path, <input dir>/<path>/base, ours and
#     theirs (a missing side has no file), plus upstream.diff (base to theirs)
#     and fork.diff (base to ours). The agent reads these instead of running git.
#
# On success prints the --allowedTools value and exits 0: Read in the work
# tree, Edit only on the conflicted paths, .escalation.md and
# .resolver-delete.txt. On a refusal writes the reason to .escalation.md and
# exits 3. Any other exit code is an error.
set -euo pipefail

list="$1"; dir="$2"

refuse() {
  {
    echo "## Conflict agent not started"
    echo
    echo "$1"
  } > .escalation.md
  echo "::error::Conflict agent not started: $1" >&2
  exit 3
}

# The agent writes these two; never start from a copy the tree brought.
rm -f .escalation.md .resolver-delete.txt

found=$(find . -path ./.git -prune -o \( -iname CLAUDE.md -o -iname CLAUDE.local.md \
  -o -iname AGENTS.md -o -iname .mcp.json -o -iname .claude \) -print | head -n 20)
if [ -n "$found" ]; then
  refuse "The work tree holds agent configuration files, which an upstream change must not add: $(echo "$found" | tr '\n' ' ')"
fi

side() { if [ -f "$dir/$f/$1" ]; then echo "$dir/$f/$1"; else echo /dev/null; fi; }

rules="Read(./**),Edit(./.escalation.md),Edit(./.resolver-delete.txt)"
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if ! [[ "$f" =~ ^[A-Za-z0-9._+-][A-Za-z0-9._/+-]*$ ]] || [[ "/$f/" == */../* ]]; then
    refuse "A conflicted path has characters the agent's tool rules cannot hold: $(printf '%q' "$f")"
  fi
  mkdir -p "$dir/$f"
  for stage in 1:base 2:ours 3:theirs; do
    git show ":${stage%%:*}:$f" > "$dir/$f/${stage#*:}" 2>/dev/null || rm -f "$dir/$f/${stage#*:}"
  done
  diff -u "$(side base)" "$(side theirs)" > "$dir/$f/upstream.diff" || true
  diff -u "$(side base)" "$(side ours)" > "$dir/$f/fork.diff" || true
  rules="$rules,Edit(./$f)"
done < "$list"

echo "$rules"
