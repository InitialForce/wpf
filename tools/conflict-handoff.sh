#!/usr/bin/env bash
# conflict-handoff.sh: move a conflict resolution from the untrusted agent job
# to the trusted job that commits it.
#
#   conflict-handoff.sh collect <conflicts.txt> <outdir> [<delete list>]
#       Run in the agent job after Claude. Copies each conflicted path that
#       still exists to <outdir>/files/<path>, lists removed ones in
#       <outdir>/deleted.txt. A conflicted path named in <delete list> (the
#       agent has no shell, so it asks for a deletion there) counts as
#       removed. Nothing outside the conflict list is collected.
#
#   conflict-handoff.sh apply <conflicts.txt> <resolution dir>
#       Run in the trusted job after it has recreated the same conflicted
#       state. Only paths in its own conflict list are taken from the
#       resolution; anything else in the artifact is ignored. Fails when a
#       resolved file is missing, is a symlink or still has conflict markers.
#       Stages the result.
set -euo pipefail

mode="$1"; list="$2"; dir="$3"; delete_list="${4:-/dev/null}"

case "$mode" in
  collect)
    mkdir -p "$dir/files"
    : > "$dir/deleted.txt"
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      if grep -qxF -- "$f" "$delete_list" 2>/dev/null; then
        echo "$f" >> "$dir/deleted.txt"
      elif [ -e "$f" ] || [ -L "$f" ]; then
        mkdir -p "$dir/files/$(dirname "$f")"
        cp -P -- "$f" "$dir/files/$f"
      else
        echo "$f" >> "$dir/deleted.txt"
      fi
    done < "$list"
    ;;
  apply)
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      src="$dir/files/$f"
      if [ -L "$src" ]; then
        echo "::error::Resolution for $f is a symlink" >&2; exit 1
      elif [ -f "$src" ]; then
        if grep -nE '^(<<<<<<<|=======|>>>>>>>)( |$)' -- "$src" >&2; then
          echo "::error::Conflict markers remain in $f" >&2; exit 1
        fi
        cp -- "$src" "$f"
        git add -- "$f"
      elif grep -qxF -- "$f" "$dir/deleted.txt" 2>/dev/null; then
        rm -f -- "$f"
        git rm -q --cached --ignore-unmatch -- "$f"
      else
        echo "::error::No resolution for $f" >&2; exit 1
      fi
    done < "$list"
    if git diff --name-only --diff-filter=U | grep -q .; then
      echo "::error::Unmerged paths remain" >&2; exit 1
    fi
    ;;
  *)
    echo "usage: $0 collect|apply <conflicts.txt> <dir>" >&2; exit 1 ;;
esac
