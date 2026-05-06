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

# Resolve claude binary. The CLI is an ELF on /c/ (drvfs) where direct exec
# fails with "Exec format error"; invoke via the dynamic loader instead.
CLAUDE_BIN="$(readlink -f "$HOME/.local/bin/claude" 2>/dev/null || true)"
if [[ -z "$CLAUDE_BIN" || ! -f "$CLAUDE_BIN" ]]; then
    echo "[ralph] FATAL: cannot resolve $HOME/.local/bin/claude" >&2
    exit 1
fi
LD_LINUX="/lib64/ld-linux-x86-64.so.2"

run_claude() { "$LD_LINUX" "$CLAUDE_BIN" --dangerously-skip-permissions "$@"; }

echo "[ralph] starting loop, max_iters=${MAX_ITERS}"
echo "[ralph] claude bin: $CLAUDE_BIN"
echo "[ralph] press Ctrl-C to stop"

# Check user session has an attached display. WPF's D3D9 video preview path
# returns NOTAVAILABLE in disconnected RDP sessions and the spike scenario
# truncates to <1000 frames vs the 8000 floor — every iter SPIKE-FAILs and
# burns API quota for nothing. Block until display returns.
session_connected() {
    cmd.exe /c "query session" 2>/dev/null \
        | grep -E "^>" \
        | grep -qE " (Active|Conn) "
}

fast_fail_count=0
disc_wait_count=0

for ((i = 1; i <= MAX_ITERS; i++)); do
    if grep -q "<halt/>" program.md; then
        echo "[ralph] <halt/> sentinel detected in program.md — stopping"
        break
    fi
    if ! session_connected; then
        # 5/10/15/20/30 min escalation, capped at 30 min
        wait_s=$(( disc_wait_count < 4 ? 300 * (disc_wait_count + 1) : 1800 ))
        disc_wait_count=$(( disc_wait_count + 1 ))
        echo
        echo "[ralph] user session disconnected (D3D9 unavailable) — sleeping ${wait_s}s. Reconnect to resume; check #${disc_wait_count}."
        sleep "$wait_s"
        continue
    fi
    if (( disc_wait_count > 0 )); then
        echo "[ralph] session reconnected — resuming iters"
        disc_wait_count=0
    fi
    echo
    echo "─── ralph iter $i / $MAX_ITERS ───"

    # Fresh context window each iteration (Geoff Huntley's key insight).
    # --dangerously-skip-permissions: this is a closed-loop sandbox, not interactive.
    iter_start=$(date +%s)
    if ! run_claude < program.md; then
        iter_dur=$(( $(date +%s) - iter_start ))
        # Fast non-zero exits (<60 s) are almost always API failures (503,
        # rate limit, auth). A normal iter is 7–25 min. Back off so we don't
        # spin and burn quota.
        if (( iter_dur < 60 )); then
            backoff=$(( fast_fail_count == 0 ? 60 : (fast_fail_count >= 5 ? 600 : 60 * (fast_fail_count + 1)) ))
            fast_fail_count=$(( fast_fail_count + 1 ))
            echo "[ralph] claude exited non-zero in ${iter_dur}s (fast-fail #${fast_fail_count}) — sleeping ${backoff}s before next iter"
            sleep "$backoff"
        else
            fast_fail_count=0
            echo "[ralph] claude exited non-zero on iter $i (after ${iter_dur}s) — continuing"
        fi
    else
        fast_fail_count=0
    fi
done

echo "[ralph] loop ended after $i iterations"
