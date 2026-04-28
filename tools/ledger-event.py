#!/usr/bin/env python3
"""Append a single signed event to .if-fork/patch-ledger.jsonl.

Every CI workflow that touches the patch ledger calls this script.
It validates the event type, constructs the JSON line with a hash-chain
linking it to the previous entry, appends it atomically, and creates a
signed git commit.  On failure it writes a structured JSON error to
stderr and exits nonzero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Event types — verbatim from exec-docs §10.5 event-types table
# ---------------------------------------------------------------------------
VALID_EVENTS: frozenset[str] = frozenset(
    [
        "discovered",
        "review_1",
        "review_2",
        "approved",
        "escalated",
        "cherry_picked",
        "build_failed",
        "build_passed",
        "smoke_failed",
        "smoke_passed",
        "perf_passed",
        "perf_failed",
        "published",
        "graduated_upstream",
        "rejected",
        "reverted",
        "autonomy_paused",
        "autonomy_resumed",
        "automerge_frozen",
        "automerge_thawed",
    ]
)

DEFAULT_LEDGER_PATH = ".if-fork/patch-ledger.jsonl"


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------
def die(code: int, message: str, **extra: object) -> None:
    """Print structured JSON error to stderr and exit with *code*."""
    payload: dict[str, object] = {"error": message, **extra}
    print(json.dumps(payload), file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------
def _sha256_of_line(raw_line: str) -> str:
    """Return SHA-256 hex digest of a UTF-8 encoded ledger line."""
    return hashlib.sha256(raw_line.encode()).hexdigest()


def _prev_hash_of_ledger(ledger_path: Path) -> str:
    """Return the line_hash of the last line, or empty string for genesis."""
    if not ledger_path.exists() or ledger_path.stat().st_size == 0:
        return ""
    last_line = ""
    with ledger_path.open(encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.rstrip("\n")
            if stripped:
                last_line = stripped
    if not last_line:
        return ""
    try:
        obj = json.loads(last_line)
    except json.JSONDecodeError:
        die(3, "Ledger is corrupt: last line is not valid JSON", last_line=last_line)
        raise  # unreachable — satisfies mypy
    line_hash = obj.get("line_hash")
    if not isinstance(line_hash, str) or not line_hash:
        die(3, "Ledger is corrupt: last line missing 'line_hash'", last_line=last_line)
        raise  # unreachable
    return line_hash


# ---------------------------------------------------------------------------
# Line construction
# ---------------------------------------------------------------------------
def build_line(
    *,
    event: str,
    pr_number: int,
    head_sha: str,
    actor: str,
    details: dict[str, object],
    prev_hash: str,
    timestamp: str | None = None,
) -> str:
    """Build a single JSON line (no trailing newline) for the ledger."""
    ts = timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Build record without line_hash first so we can hash it
    record: dict[str, object] = {
        "schema_version": 1,
        "ts": ts,
        "event": event,
        "actor": actor,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "details": details,
        "prev_hash": prev_hash,
    }
    body = json.dumps(record, separators=(",", ":"), sort_keys=True)
    line_hash = _sha256_of_line(body)
    record["line_hash"] = line_hash
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
def _git_commit(ledger_path: Path, message: str) -> None:
    """Stage *ledger_path* and create a signed git commit, falling back to unsigned."""
    # Stage the ledger file
    subprocess.run(
        ["git", "add", str(ledger_path)],
        check=True,
        capture_output=True,
    )

    # Try signed commit first
    try:
        result = subprocess.run(
            ["git", "commit", "-S", "-m", message],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        # GPG failure heuristic: exit code 128 with "gpg" in stderr
        if "gpg" in result.stderr.lower() or "signing" in result.stderr.lower():
            print(
                "WARNING: GPG signing failed; falling back to unsigned commit. "
                "Signed commits are required in production CI.",
                file=sys.stderr,
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                check=True,
                capture_output=True,
            )
            return
        # Some other error — propagate
        die(
            5,
            "git commit failed",
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )
    except FileNotFoundError:
        die(5, "git not found in PATH")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append a single signed event to patch-ledger.jsonl"
    )
    parser.add_argument(
        "--event",
        required=True,
        help=f"Event type. One of: {', '.join(sorted(VALID_EVENTS))}",
    )
    parser.add_argument("--pr-number", required=True, type=int, help="PR number")
    parser.add_argument("--head-sha", required=True, help="Commit SHA")
    parser.add_argument("--actor", required=True, help="Actor identifier")
    parser.add_argument(
        "--details-json",
        required=True,
        help="JSON object with event-specific details",
    )
    parser.add_argument(
        "--ledger-path",
        default=DEFAULT_LEDGER_PATH,
        help=f"Path to ledger file (default: {DEFAULT_LEDGER_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the line that would be appended, but do not write or commit",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help=(
            "Write the event to the ledger but skip the git commit step. "
            "Useful for bulk-import tools that want to make a single commit "
            "after processing all entries."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    args = parse_args(argv)

    # --- Validate event type ---
    if args.event not in VALID_EVENTS:
        die(
            2,
            f"Invalid event type: {args.event!r}",
            valid_events=sorted(VALID_EVENTS),
        )

    # --- Validate details JSON ---
    try:
        details: dict[str, object] = json.loads(args.details_json)
    except json.JSONDecodeError as exc:
        die(2, f"--details-json is not valid JSON: {exc}")
        raise  # unreachable
    if not isinstance(details, dict):
        die(2, "--details-json must be a JSON object (dict), not an array or scalar")

    ledger_path = Path(args.ledger_path)

    # --- Compute prev_hash ---
    prev_hash = _prev_hash_of_ledger(ledger_path)

    # --- Build the line ---
    line = build_line(
        event=args.event,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        actor=args.actor,
        details=details,
        prev_hash=prev_hash,
    )

    if args.dry_run:
        # Validate the line parses back cleanly
        parsed = json.loads(line)
        print(json.dumps(parsed, indent=2))
        return 0

    # --- Append to ledger ---
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    # --- Git commit (skipped when --no-commit is set) ---
    if not args.no_commit:
        commit_msg = (
            f"ledger: {args.event} pr#{args.pr_number} actor={args.actor}\n\n"
            f"head_sha={args.head_sha}"
        )
        _git_commit(ledger_path, commit_msg)

    # --- Confirm success ---
    parsed_out = json.loads(line)
    print(json.dumps({"status": "ok", "line_hash": parsed_out["line_hash"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
