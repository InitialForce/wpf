#!/usr/bin/env bash
# WPF Autoresearch Ralph loop.
#
# Usage:   ./ralph.sh [max_iters]
# Example: ./ralph.sh 50
#
# What it does: in a loop, pipes program.md to `claude` (skipping permissions)
# with the autoresearch directory as cwd. Each invocation gets a fresh context
# window. Claude reads program.md + the log tail, edits WPF source, commits,
# runs eval.py, and exits. The loop then re-invokes claude. NEVER STOP unless
# - max_iters hit, OR
# - <halt/> sentinel appears in program.md (manual stop signal), OR
# - human Ctrl-Cs the loop.
#
# Each iteration writes its own commit in /c/work/wpf-perf/ AND a row in
# results.tsv (and a JSON line in results.jsonl). Use plot.py to visualise.

set -uo pipefail

MAX_ITERS="${1:-1000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [[ ! -f baseline.json ]]; then
    echo "[ralph] baseline.json missing. Run bootstrap.py first." >&2
    exit 1
fi

echo "[ralph] starting loop, max_iters=${MAX_ITERS}"
echo "[ralph] press Ctrl-C to stop"

for ((i = 1; i <= MAX_ITERS; i++)); do
    if grep -q "<halt/>" program.md; then
        echo "[ralph] <halt/> sentinel detected in program.md — stopping"
        break
    fi
    echo
    echo "─── ralph iter $i / $MAX_ITERS ───"

    # Fresh context window each iteration (Geoff Huntley's key insight).
    # --dangerously-skip-permissions: this is a closed-loop sandbox, not interactive.
    if ! claude --dangerously-skip-permissions < program.md; then
        echo "[ralph] claude exited non-zero on iter $i — continuing"
    fi
done

echo "[ralph] loop ended after $i iterations"
