#!/usr/bin/env bash
# cherry-pick-pre-flight.sh — Graduation pre-flight check before cherry-pick.
#
# Detects whether the upstream PR has already been absorbed into the target
# branch before attempting to apply it.  Structured JSON to stdout; logs to
# stderr.
#
# Usage:
#   cherry-pick-pre-flight.sh \
#     --pr-number <N> \
#     --head-sha <sha> \
#     --upstream-ref <ref> \
#     --ledger-path <path> \
#     [--patch-path <path>]   # bypass upstream fetch (testing / offline)
#
# Exit codes:
#   0 — action=apply  (not-graduated; safe to proceed with cherry-pick)
#   0 — action=skip   (graduated; already absorbed; ledger event emitted)
#   1 — error         (unexpected failure; structured error on stderr)
#   2 — partial       (some hunks present, some missing; escalation emitted)
#
# Stdout (always exactly one JSON object):
#   { "action": "apply", "patch_path": "/tmp/..." }          exit 0
#   { "action": "skip",  "reason": "already-graduated" }     exit 0
#   { "action": "escalate", "reason": "partial-graduation" } exit 2

set -euo pipefail

# ---------------------------------------------------------------------------
# Logging helpers (structured JSON to stderr)
# ---------------------------------------------------------------------------
_log() {
    local level="$1"
    shift
    printf '{"level":"%s","ts":"%s","msg":"%s"}\n' \
        "$level" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$*" >&2
}
log_info()  { _log "info"  "$*"; }
log_warn()  { _log "warn"  "$*"; }
log_error() { _log "error" "$*"; }

# ---------------------------------------------------------------------------
# Usage / help
# ---------------------------------------------------------------------------
usage() {
    cat >&2 <<'EOF'
cherry-pick-pre-flight.sh — graduation pre-flight check before cherry-pick

Usage:
  cherry-pick-pre-flight.sh OPTIONS

Required options:
  --pr-number <N>        Upstream PR number (integer)
  --head-sha <sha>       Commit SHA pinned at discovery
  --upstream-ref <ref>   Upstream remote ref, e.g. "upstream" or "dotnet/wpf"
  --ledger-path <path>   Absolute path to .if-fork/patch-ledger.jsonl

Optional options:
  --patch-path <path>    Use an existing patch file instead of fetching
                         from upstream (offline / test mode)
  --actor <id>           Actor identifier for ledger events
                         (default: cherry-pick-pre-flight)
  --target-root <path>   Root of the source tree to check against
                         (default: current git root)
  --help                 Print this message and exit 0

Exit codes:
  0  action=apply  (not-graduated; proceed with cherry-pick)
  0  action=skip   (graduated; already absorbed)
  1  error
  2  action=escalate (partial graduation)

Stdout: exactly one JSON object describing the recommended action.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
PR_NUMBER=""
HEAD_SHA=""
UPSTREAM_REF=""
LEDGER_PATH=""
PATCH_PATH_OVERRIDE=""
ACTOR="cherry-pick-pre-flight"
TARGET_ROOT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pr-number)
            PR_NUMBER="$2"
            shift 2
            ;;
        --head-sha)
            HEAD_SHA="$2"
            shift 2
            ;;
        --upstream-ref)
            UPSTREAM_REF="$2"
            shift 2
            ;;
        --ledger-path)
            LEDGER_PATH="$2"
            shift 2
            ;;
        --patch-path)
            PATCH_PATH_OVERRIDE="$2"
            shift 2
            ;;
        --actor)
            ACTOR="$2"
            shift 2
            ;;
        --target-root)
            TARGET_ROOT="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Validate required arguments
# ---------------------------------------------------------------------------
_missing=()
[[ -z "$PR_NUMBER"    ]] && _missing+=("--pr-number")
[[ -z "$HEAD_SHA"     ]] && _missing+=("--head-sha")
[[ -z "$UPSTREAM_REF" ]] && _missing+=("--upstream-ref")
[[ -z "$LEDGER_PATH"  ]] && _missing+=("--ledger-path")

if [[ ${#_missing[@]} -gt 0 ]]; then
    log_error "Missing required arguments: ${_missing[*]}"
    usage
    exit 1
fi

# Validate PR_NUMBER is a positive integer
if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
    log_error "Invalid --pr-number: must be a positive integer, got: $PR_NUMBER"
    exit 1
fi

# ---------------------------------------------------------------------------
# Resolve tools directory (relative to this script, regardless of cwd)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_GRADUATED="${SCRIPT_DIR}/check-graduated.py"
LEDGER_EVENT="${SCRIPT_DIR}/ledger-event.py"

if [[ ! -f "$CHECK_GRADUATED" ]]; then
    log_error "tools/check-graduated.py not found at: $CHECK_GRADUATED"
    exit 1
fi
if [[ ! -f "$LEDGER_EVENT" ]]; then
    log_error "tools/ledger-event.py not found at: $LEDGER_EVENT"
    exit 1
fi

# ---------------------------------------------------------------------------
# Resolve target root (default: git toplevel)
# ---------------------------------------------------------------------------
if [[ -z "$TARGET_ROOT" ]]; then
    if TARGET_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
        : # success
    else
        TARGET_ROOT="$(pwd)"
        log_warn "Not inside a git repo; using cwd as target-root: $TARGET_ROOT"
    fi
fi

# ---------------------------------------------------------------------------
# Temp file management
# ---------------------------------------------------------------------------
TEMP_PATCH=""

cleanup() {
    if [[ -n "$TEMP_PATCH" && -f "$TEMP_PATCH" ]]; then
        rm -f "$TEMP_PATCH"
        log_info "Removed temp patch file: $TEMP_PATCH"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 1 — Obtain the patch file
# ---------------------------------------------------------------------------
if [[ -n "$PATCH_PATH_OVERRIDE" ]]; then
    # Offline / test mode: caller supplies a pre-fetched patch
    log_info "Using caller-supplied patch: $PATCH_PATH_OVERRIDE"
    if [[ ! -f "$PATCH_PATH_OVERRIDE" ]]; then
        log_error "Patch file not found: $PATCH_PATH_OVERRIDE"
        exit 1
    fi
    PATCH_FILE="$PATCH_PATH_OVERRIDE"
    # We do NOT set TEMP_PATCH here so cleanup does not remove a caller-owned file
else
    # Online mode: fetch the diff from upstream via git
    log_info "Fetching PR #${PR_NUMBER} diff from upstream ref: ${UPSTREAM_REF}"

    TEMP_PATCH="$(mktemp --suffix=".patch" "/tmp/cherry-pick-pre-flight-XXXXXX")"
    log_info "Temp patch file: $TEMP_PATCH"

    # Fetch the PR's head ref into a local ref
    FETCH_REF="refs/remotes/pr/${PR_NUMBER}"
    if ! git fetch "${UPSTREAM_REF}" \
            "refs/pull/${PR_NUMBER}/head:${FETCH_REF}" 2>&1 | \
            while IFS= read -r line; do log_info "git-fetch: $line"; done; then
        log_error "Failed to fetch PR #${PR_NUMBER} from ${UPSTREAM_REF}"
        exit 1
    fi

    # Export the diff of the PR's head commit (single commit diff)
    if ! git format-patch -1 "${HEAD_SHA}" --stdout > "$TEMP_PATCH" 2>&1; then
        log_error "git format-patch failed for sha: $HEAD_SHA"
        exit 1
    fi

    if [[ ! -s "$TEMP_PATCH" ]]; then
        log_warn "Patch file is empty for sha: $HEAD_SHA — treating as not-graduated"
        printf '{"action":"apply","patch_path":"%s","note":"empty-patch"}\n' "$TEMP_PATCH"
        exit 0
    fi

    PATCH_FILE="$TEMP_PATCH"
fi

log_info "Patch file ready: $PATCH_FILE ($(wc -c < "$PATCH_FILE") bytes)"

# ---------------------------------------------------------------------------
# Step 2 — Run check-graduated.py
# ---------------------------------------------------------------------------
log_info "Running check-graduated.py against tree: $TARGET_ROOT"

GRADUATED_JSON_OUT="$(mktemp "/tmp/cherry-pick-graduated-out-XXXXXX.json")"
# check-graduated.py exits: 0=not-graduated, 1=graduated, 2=partial
GRADUATED_EXIT=0
python3 "$CHECK_GRADUATED" \
    --patch "$PATCH_FILE" \
    --target-root "$TARGET_ROOT" \
    --output "$GRADUATED_JSON_OUT" \
    || GRADUATED_EXIT=$?

VERDICT=""
case "$GRADUATED_EXIT" in
    0) VERDICT="not-graduated" ;;
    1) VERDICT="graduated"     ;;
    2) VERDICT="partial"       ;;
    *)
        log_error "check-graduated.py exited with unexpected code: $GRADUATED_EXIT"
        rm -f "$GRADUATED_JSON_OUT"
        exit 1
        ;;
esac

log_info "Graduation verdict: $VERDICT (check-graduated exit=$GRADUATED_EXIT)"

# ---------------------------------------------------------------------------
# Step 3 — Act on verdict
# ---------------------------------------------------------------------------

case "$VERDICT" in

    # -----------------------------------------------------------------------
    "graduated")
    # -----------------------------------------------------------------------
        log_info "PR #${PR_NUMBER} is already graduated; emitting ledger event and skipping"

        python3 "$LEDGER_EVENT" \
            --event "graduated_upstream" \
            --pr-number "$PR_NUMBER" \
            --head-sha "$HEAD_SHA" \
            --actor "$ACTOR" \
            --details-json '{"detection_method":"hunk_match","pre_flight":true}' \
            --ledger-path "$LEDGER_PATH" \
            >&2 \
            || {
                log_error "ledger-event.py failed while emitting graduated_upstream"
                rm -f "$GRADUATED_JSON_OUT"
                exit 1
            }

        log_info "Ledger event graduated_upstream emitted for PR #${PR_NUMBER}"
        rm -f "$GRADUATED_JSON_OUT"

        printf '{"action":"skip","reason":"already-graduated","pr_number":%s}\n' \
            "$PR_NUMBER"
        exit 0
        ;;

    # -----------------------------------------------------------------------
    "partial")
    # -----------------------------------------------------------------------
        log_warn "PR #${PR_NUMBER} is partially graduated; escalating to human review"

        python3 "$LEDGER_EVENT" \
            --event "escalated" \
            --pr-number "$PR_NUMBER" \
            --head-sha "$HEAD_SHA" \
            --actor "$ACTOR" \
            --details-json '{"reason":"partial_graduation","detection_method":"hunk_match","pre_flight":true}' \
            --ledger-path "$LEDGER_PATH" \
            >&2 \
            || {
                log_error "ledger-event.py failed while emitting escalated"
                rm -f "$GRADUATED_JSON_OUT"
                exit 2
            }

        log_warn "Ledger event escalated emitted for PR #${PR_NUMBER}"
        rm -f "$GRADUATED_JSON_OUT"

        printf '{"action":"escalate","reason":"partial-graduation","pr_number":%s}\n' \
            "$PR_NUMBER"
        exit 2
        ;;

    # -----------------------------------------------------------------------
    "not-graduated")
    # -----------------------------------------------------------------------
        log_info "PR #${PR_NUMBER} is not graduated; safe to apply"
        rm -f "$GRADUATED_JSON_OUT"

        printf '{"action":"apply","patch_path":"%s","pr_number":%s}\n' \
            "$PATCH_FILE" "$PR_NUMBER"
        exit 0
        ;;

    *)
        log_error "Unexpected verdict: $VERDICT"
        rm -f "$GRADUATED_JSON_OUT"
        exit 1
        ;;
esac
