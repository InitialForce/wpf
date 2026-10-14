#!/usr/bin/env python3
"""Append signed events to .if-fork/patch-ledger.jsonl.

Every CI workflow that touches the patch ledger calls this script.
It validates the event type, constructs the JSON line with a hash-chain
linking it to the previous entry, appends it atomically, and creates a
signed git commit.  On failure it writes a structured JSON error to
stderr and exits nonzero.

``--events-file`` appends many events (one JSON object per line, each with
``event``, ``pr_number``, ``head_sha``, ``actor`` and ``details``) in a single
commit and a single push, so a workflow run adds one commit to if/main no
matter how many events it records.

Ledger commits carry ``[skip ci]`` in the subject: they only touch
``.if-fork/`` and must never start a build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
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

# Maximum number of push attempts on non-fast-forward (CRIT-3 fix). Several
# pr-review runs can finish at the same minute, so allow a few rounds.
MAX_PUSH_RETRIES = 8

# Every ledger commit subject ends with this so GitHub skips push-triggered
# workflows (build.yml) for ledger-only commits.
SKIP_CI_MARKER = "[skip ci]"


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


def _git_commit(ledger_path: Path, message: str, line_hashes: list[str]) -> None:
    """Stage *ledger_path* and create a signed git commit.

    In CI environments (CI=true) a GPG-signing failure is fatal — we do NOT
    fall back to unsigned commits (HIGH-2 fix).  Outside CI the existing
    fallback-with-warning behaviour is preserved.

    The commit message includes one ``Ledger-Line-Hash:`` trailer per
    appended line so that ledger-validate.py can later locate each entry's
    commit by content rather than by array index (MED-5 fix).
    """
    trailers = "\n".join(f"Ledger-Line-Hash: {h}" for h in line_hashes)
    full_message = f"{message}\n\n{trailers}"

    # Ensure git has a committer identity. Workflows already set GIT_AUTHOR_*
    # for attribution; mirror those into GIT_COMMITTER_* if not already set so
    # `git commit` never fails on "Committer identity unknown".
    commit_env = os.environ.copy()
    if "GIT_COMMITTER_NAME" not in commit_env and "GIT_AUTHOR_NAME" in commit_env:
        commit_env["GIT_COMMITTER_NAME"] = commit_env["GIT_AUTHOR_NAME"]
    if "GIT_COMMITTER_EMAIL" not in commit_env and "GIT_AUTHOR_EMAIL" in commit_env:
        commit_env["GIT_COMMITTER_EMAIL"] = commit_env["GIT_AUTHOR_EMAIL"]

    # Stage the ledger file
    subprocess.run(
        ["git", "add", str(ledger_path)],
        check=True,
        capture_output=True,
        env=commit_env,
    )

    # Try signed commit first
    try:
        result = subprocess.run(
            ["git", "commit", "-S", "-m", full_message],
            capture_output=True,
            text=True,
            env=commit_env,
        )
        if result.returncode == 0:
            return
        # GPG failure heuristic: exit code 128 with "gpg" / "signing" in stderr
        is_gpg_failure = (
            "gpg" in result.stderr.lower() or "signing" in result.stderr.lower()
        )
        if is_gpg_failure:
            # Phase-0 escape hatch: explicit opt-in env var permits unsigned
            # commits in CI when GPG signing isn't yet provisioned. Remove the
            # opt-in once a bot GPG key is wired up; production CI must sign.
            allow_unsigned = (
                os.environ.get("IF_FORK_ALLOW_UNSIGNED_LEDGER", "").lower()
                in ("1", "true", "yes")
            )
            if _is_ci() and not allow_unsigned:
                # HIGH-2 fix: fail closed in CI unless the operator explicitly
                # opted in via IF_FORK_ALLOW_UNSIGNED_LEDGER.
                die(
                    5,
                    "GPG signing failed in CI environment; unsigned commits are not "
                    "permitted in production. Set IF_FORK_ALLOW_UNSIGNED_LEDGER=true "
                    "for explicit phase-0 opt-in, or provision a GPG key.",
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )
            # Outside CI, or with the phase-0 opt-in: warn and fall back to unsigned
            print(
                "WARNING: GPG signing failed; falling back to unsigned commit. "
                "Signed commits are required in production CI once GPG is provisioned.",
                file=sys.stderr,
            )
            subprocess.run(
                ["git", "commit", "-m", full_message],
                check=True,
                capture_output=True,
                env=commit_env,
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


def _append_events(ledger_path: Path, specs: list[dict[str, object]]) -> list[str]:
    """Append one chained line per spec to *ledger_path*; return the line hashes."""
    prev_hash = _prev_hash_of_ledger(ledger_path)
    lines: list[str] = []
    hashes: list[str] = []
    for spec in specs:
        line = build_line(
            event=str(spec["event"]),
            pr_number=int(spec["pr_number"]),  # type: ignore[call-overload]
            head_sha=str(spec["head_sha"]),
            actor=str(spec["actor"]),
            details=spec["details"],  # type: ignore[arg-type]
            prev_hash=prev_hash,
        )
        prev_hash = json.loads(line)["line_hash"]
        lines.append(line)
        hashes.append(prev_hash)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))
    return hashes


def _commit_message(specs: list[dict[str, object]], message: str | None) -> str:
    """Return the commit message for *specs* (subject always has SKIP_CI_MARKER)."""
    if message:
        subject = message
    elif len(specs) == 1:
        spec = specs[0]
        subject = f"ledger: {spec['event']} pr#{spec['pr_number']} actor={spec['actor']}"
    else:
        events = sorted({str(spec["event"]) for spec in specs})
        subject = f"ledger: {len(specs)} events ({', '.join(events)})"
    if SKIP_CI_MARKER not in subject:
        subject = f"{subject} {SKIP_CI_MARKER}"
    body = "\n".join(
        f"{spec['event']} pr#{spec['pr_number']} head_sha={spec['head_sha']}"
        for spec in specs
    )
    return f"{subject}\n\n{body}"


def _push_with_retry(
    ledger_path: Path,
    specs: list[dict[str, object]],
    message: str,
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
            (which restores the remote ledger state), rebuild every line of
            this batch on top of the remote ledger tip, commit, and retry.
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

        # Back off with jitter so concurrent writers do not collide again.
        time.sleep(attempt + random.uniform(0, 2))

        # Try to rebase our commit cleanly on top of the remote tip.
        if _try_rebase(branch):
            continue

        # Rebase conflicted (the usual case: both sides appended to the ledger).
        _reset_to_remote(branch)
        line_hashes = _append_events(ledger_path, specs)
        _git_commit(ledger_path, message, line_hashes)


def _load_events_file(path: Path, default_actor: str | None) -> list[dict[str, object]]:
    """Read and validate a JSONL file of event specs."""
    specs: list[dict[str, object]] = []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        die(2, f"Cannot read --events-file: {exc}")
        raise  # unreachable
    for number, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            die(2, f"--events-file line {number} is not valid JSON: {exc}")
            raise  # unreachable
        if not isinstance(obj, dict):
            die(2, f"--events-file line {number} must be a JSON object")
        spec = {
            "event": obj.get("event"),
            "pr_number": obj.get("pr_number"),
            "head_sha": obj.get("head_sha"),
            "actor": obj.get("actor") or default_actor,
            "details": obj.get("details", {}),
        }
        _validate_spec(spec, f"--events-file line {number}")
        specs.append(spec)
    return specs


def _validate_spec(spec: dict[str, object], where: str) -> None:
    if spec["event"] not in VALID_EVENTS:
        die(
            2,
            f"Invalid event type in {where}: {spec['event']!r}",
            valid_events=sorted(VALID_EVENTS),
        )
    if not isinstance(spec["pr_number"], int) or isinstance(spec["pr_number"], bool):
        die(2, f"{where}: pr_number must be an integer")
    if not isinstance(spec["head_sha"], str) or not spec["head_sha"]:
        die(2, f"{where}: head_sha must be a non-empty string")
    if not isinstance(spec["actor"], str) or not spec["actor"]:
        die(2, f"{where}: actor must be a non-empty string")
    if not isinstance(spec["details"], dict):
        die(2, f"{where}: details must be a JSON object (dict), not an array or scalar")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append signed events to patch-ledger.jsonl"
    )
    parser.add_argument(
        "--event",
        help=f"Event type. One of: {', '.join(sorted(VALID_EVENTS))}",
    )
    parser.add_argument("--pr-number", type=int, help="PR number")
    parser.add_argument("--head-sha", help="Commit SHA")
    parser.add_argument("--actor", help="Actor identifier")
    parser.add_argument(
        "--details-json",
        help="JSON object with event-specific details",
    )
    parser.add_argument(
        "--events-file",
        help=(
            "JSONL file with one event object per line (event, pr_number, "
            "head_sha, actor, details). All events go into one commit. "
            "Replaces --event/--pr-number/--head-sha/--details-json; --actor "
            "is the default for lines without one."
        ),
    )
    parser.add_argument(
        "--message",
        help="Commit subject (default: derived from the events)",
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
    args = parser.parse_args(argv)
    if args.events_file is None:
        missing = [
            flag
            for flag, value in (
                ("--event", args.event),
                ("--pr-number", args.pr_number),
                ("--head-sha", args.head_sha),
                ("--actor", args.actor),
                ("--details-json", args.details_json),
            )
            if value is None
        ]
        if missing:
            parser.error(
                f"the following arguments are required: {', '.join(missing)} "
                "(or pass --events-file)"
            )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.events_file is not None:
        specs = _load_events_file(Path(args.events_file), args.actor)
        if not specs:
            print(json.dumps({"status": "ok", "appended": 0}))
            return 0
    else:
        # --- Validate event type ---
        if args.event not in VALID_EVENTS:
            die(
                2,
                f"Invalid event type: {args.event!r}",
                valid_events=sorted(VALID_EVENTS),
            )

        # --- Validate details JSON ---
        try:
            details: object = json.loads(args.details_json)
        except json.JSONDecodeError as exc:
            die(2, f"--details-json is not valid JSON: {exc}")
            raise  # unreachable
        if not isinstance(details, dict):
            die(2, "--details-json must be a JSON object (dict), not an array or scalar")
        specs = [
            {
                "event": args.event,
                "pr_number": args.pr_number,
                "head_sha": args.head_sha,
                "actor": args.actor,
                "details": details,
            }
        ]

    ledger_path = Path(args.ledger_path)

    # --- Resolve --push default ---
    # Explicit --push / --no-push wins; otherwise default True in CI, False elsewhere.
    if args.push is None:
        do_push = _is_ci()
    else:
        do_push = args.push

    if args.dry_run:
        prev_hash = _prev_hash_of_ledger(ledger_path)
        for spec in specs:
            line = build_line(
                event=str(spec["event"]),
                pr_number=int(spec["pr_number"]),  # type: ignore[call-overload]
                head_sha=str(spec["head_sha"]),
                actor=str(spec["actor"]),
                details=spec["details"],  # type: ignore[arg-type]
                prev_hash=prev_hash,
            )
            parsed = json.loads(line)
            prev_hash = parsed["line_hash"]
            print(json.dumps(parsed, indent=2))
        return 0

    # --- Append to ledger ---
    line_hashes = _append_events(ledger_path, specs)

    # --- Git commit (skipped when --no-commit is set) ---
    if not args.no_commit:
        message = _commit_message(specs, args.message)
        _git_commit(ledger_path, message, line_hashes)

        # --- Push (CRIT-3 fix) ---
        _push_with_retry(
            ledger_path=ledger_path,
            specs=specs,
            message=message,
            do_push=do_push,
        )

    # --- Confirm success ---
    if len(line_hashes) == 1:
        print(json.dumps({"status": "ok", "line_hash": line_hashes[0]}))
    else:
        print(json.dumps({"status": "ok", "appended": len(line_hashes),
                          "line_hashes": line_hashes}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
