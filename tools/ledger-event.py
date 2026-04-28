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
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Ensure the tools directory is importable whether the script is run directly
# or loaded via importlib from the project root.
_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

# ---------------------------------------------------------------------------
# Event types — imported from shared schema (LOW-1 fix)
# ---------------------------------------------------------------------------
from ledger_schema import VALID_EVENTS  # noqa: E402

DEFAULT_LEDGER_PATH = ".if-fork/patch-ledger.jsonl"

# Maximum number of push retries on non-fast-forward (CRIT-3 fix)
MAX_PUSH_RETRIES = 3


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
def _is_ci() -> bool:
    """Return True when running inside a CI environment (GitHub Actions etc.)."""
    return os.environ.get("CI", "").lower() in ("true", "1", "yes")


def _current_branch() -> str:
    """Return the current git branch name, or 'HEAD' if detached."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or "HEAD"
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "HEAD"


def _git_commit(ledger_path: Path, message: str, line_hash: str) -> None:
    """Stage *ledger_path* and create a signed git commit.

    In CI environments (CI=true) a GPG-signing failure is fatal — we do NOT
    fall back to unsigned commits (HIGH-2 fix).  Outside CI the existing
    fallback-with-warning behaviour is preserved.

    The commit message includes a ``Ledger-Line-Hash:`` trailer so that
    ledger-validate.py can later locate each entry's commit by content rather
    than by array index (MED-5 fix).
    """
    # Build the full commit message with trailer
    full_message = (
        f"{message}\n\n"
        f"Ledger-Line-Hash: {line_hash}"
    )

    # Stage the ledger file
    subprocess.run(
        ["git", "add", str(ledger_path)],
        check=True,
        capture_output=True,
    )

    # Try signed commit first
    try:
        result = subprocess.run(
            ["git", "commit", "-S", "-m", full_message],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        # GPG failure heuristic: exit code 128 with "gpg" / "signing" in stderr
        is_gpg_failure = (
            "gpg" in result.stderr.lower() or "signing" in result.stderr.lower()
        )
        if is_gpg_failure:
            if _is_ci():
                # HIGH-2 fix: fail closed in CI
                die(
                    5,
                    "GPG signing failed in CI environment; unsigned commits are not "
                    "permitted in production. Ensure a GPG key is available to the runner.",
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )
            # Outside CI: warn and fall back to unsigned
            print(
                "WARNING: GPG signing failed; falling back to unsigned commit. "
                "Signed commits are required in production CI.",
                file=sys.stderr,
            )
            subprocess.run(
                ["git", "commit", "-m", full_message],
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


def _git_push(branch: str) -> subprocess.CompletedProcess[str]:
    """Push the current HEAD to *branch* on origin."""
    return subprocess.run(
        ["git", "push", "origin", f"HEAD:{branch}"],
        capture_output=True,
        text=True,
    )


def _fetch_origin(branch: str) -> bool:
    """Fetch *branch* from origin. Returns True on success."""
    result = subprocess.run(
        ["git", "fetch", "origin", branch],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _try_rebase(branch: str) -> bool:
    """Attempt to rebase HEAD onto origin/*branch*.

    Returns True if the rebase succeeds cleanly (our commit replays on top).
    On conflict, aborts the rebase so the repository is left in a clean state,
    then returns False.
    """
    rebase = subprocess.run(
        ["git", "rebase", f"origin/{branch}"],
        capture_output=True,
        text=True,
    )
    if rebase.returncode != 0:
        # Abort so the repo is not left mid-rebase
        subprocess.run(
            ["git", "rebase", "--abort"],
            capture_output=True,
        )
        return False
    return True


def _reset_to_remote(branch: str) -> None:
    """Hard-reset HEAD to origin/*branch*, discarding any local commits."""
    subprocess.run(
        ["git", "reset", "--hard", f"origin/{branch}"],
        check=True,
        capture_output=True,
    )


def _push_with_retry(
    ledger_path: Path,
    event: str,
    pr_number: int,
    head_sha: str,
    actor: str,
    details: dict[str, object],
    do_push: bool,
) -> None:
    """Push after commit; on non-FF, rebase and retry.

    Algorithm (Review-4 fix):
      1. Attempt push.
      2. If push succeeds: done.
      3. If push rejected (non-FF):
         a. Fetch + attempt rebase of our commit onto origin/<branch>.
         b. If rebase succeeds cleanly: our commit is now on top of the
            remote tip — retry the push directly (no ledger rewrite needed).
         c. If rebase conflicts: abort rebase, hard-reset to origin/<branch>
            (which restores the remote ledger state), recompute prev_hash from
            the remote ledger tip, build a fresh ledger line, append, commit,
            and retry the push.
      4. Cap at MAX_PUSH_RETRIES total push attempts.

    This avoids the single-line zeroing bug (we never manually strip the
    ledger) and the rebase-conflict fatal exit (we recover via reset+recompute).
    """
    if not do_push:
        return

    branch = _current_branch()
    for attempt in range(1, MAX_PUSH_RETRIES + 1):
        push_result = _git_push(branch)
        if push_result.returncode == 0:
            return

        is_non_ff = (
            "non-fast-forward" in push_result.stderr.lower()
            or "rejected" in push_result.stderr.lower()
            or "[rejected]" in push_result.stdout.lower()
        )
        if not is_non_ff or attempt == MAX_PUSH_RETRIES:
            die(
                6,
                f"git push failed after {attempt} attempt(s)",
                stdout=push_result.stdout,
                stderr=push_result.stderr,
                returncode=push_result.returncode,
            )

        # Non-FF: fetch the latest remote state first
        if not _fetch_origin(branch):
            die(6, "Failed to fetch origin after non-fast-forward push rejection")

        # Try to rebase our commit cleanly on top of the remote tip.
        if _try_rebase(branch):
            # Rebase succeeded — our commit is now on top of the remote tip.
            # No ledger manipulation needed; just retry the push.
            time.sleep(1)
            continue

        # Rebase conflicted.  Recover by:
        #   1. Hard-reset to the remote tip (restores the remote ledger state).
        #   2. Recompute prev_hash from the remote ledger tip.
        #   3. Build a fresh ledger line and commit it.
        _reset_to_remote(branch)

        prev_hash = _prev_hash_of_ledger(ledger_path)
        new_line = build_line(
            event=event,
            pr_number=pr_number,
            head_sha=head_sha,
            actor=actor,
            details=details,
            prev_hash=prev_hash,
        )

        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(new_line + "\n")

        new_line_hash = json.loads(new_line)["line_hash"]
        commit_msg = (
            f"ledger: {event} pr#{pr_number} actor={actor}\n\n"
            f"head_sha={head_sha}"
        )
        _git_commit(ledger_path, commit_msg, new_line_hash)

        # Small back-off before retrying the push
        time.sleep(1)


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
    parser.add_argument(
        "--push",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Push the ledger commit to origin after appending. "
            "Defaults to True in CI (CI=true) and False outside CI. "
            "Use --no-push to disable explicitly."
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

    # --- Resolve --push default ---
    # Explicit --push / --no-push wins; otherwise default True in CI, False elsewhere.
    if args.push is None:
        do_push = _is_ci()
    else:
        do_push = args.push

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
        line_hash_value = json.loads(line)["line_hash"]
        _git_commit(ledger_path, commit_msg, line_hash_value)

        # --- Push (CRIT-3 fix) ---
        _push_with_retry(
            ledger_path=ledger_path,
            event=args.event,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            actor=args.actor,
            details=details,
            do_push=do_push,
        )

    # --- Confirm success ---
    parsed_out = json.loads(line)
    print(json.dumps({"status": "ok", "line_hash": parsed_out["line_hash"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
