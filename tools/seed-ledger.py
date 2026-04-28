#!/usr/bin/env python3
"""Bulk-import PR candidates into patch-ledger.jsonl as 'discovered' events.

This is a one-time backfill tool.  It reads a JSON list of PR candidates
(from round-2-factcheck or any path), deduplicates against the existing
ledger, and calls tools/ledger-event.py for each new entry.

Usage::

    python tools/seed-ledger.py \\
        --input round-2-factcheck/h3xds1nz-all-prs.json \\
        --ledger-path .if-fork/patch-ledger.jsonl \\
        --actor seed-bulk-import \\
        --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_INPUT = (
    Path(__file__).parent.parent
    / "_wpf-fork-plan"
    / "round-2-factcheck"
    / "h3xds1nz-all-prs.json"
)
_DEFAULT_LEDGER = ".if-fork/patch-ledger.jsonl"
_DEFAULT_ACTOR = "seed-bulk-import"
_DEFAULT_BATCH_SIZE = 50

_LEDGER_EVENT_SCRIPT = Path(__file__).parent / "ledger-event.py"


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _load_existing_pr_numbers(ledger_path: Path) -> set[int]:
    """Return the set of pr_numbers already recorded in *ledger_path*."""
    if not ledger_path.exists():
        return set()
    existing: set[int] = set()
    with ledger_path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj: dict[str, Any] = json.loads(stripped)
            except json.JSONDecodeError as exc:
                # Non-fatal: report and keep going so we don't block a partial ledger.
                print(
                    f"WARNING: ledger line {lineno} is not valid JSON ({exc}); skipping.",
                    file=sys.stderr,
                )
                continue
            pr_num = obj.get("pr_number")
            if isinstance(pr_num, int):
                existing.add(pr_num)
    return existing


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def _load_candidates(input_path: Path) -> list[dict[str, Any]]:
    """Load and validate the JSON candidate list from *input_path*."""
    try:
        raw = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            json.dumps({"error": f"Cannot read input file: {exc}"}),
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            json.dumps({"error": f"Input file is not valid JSON: {exc}"}),
            file=sys.stderr,
        )
        sys.exit(2)

    if not isinstance(data, list):
        print(
            json.dumps({"error": "Input JSON must be a list of PR objects"}),
            file=sys.stderr,
        )
        sys.exit(2)

    return data


def _validate_candidate(pr: Any) -> str | None:
    """Return an error string if *pr* is not a valid candidate dict, else None."""
    if not isinstance(pr, dict):
        return f"expected a dict, got {type(pr).__name__}"
    if "pr_number" not in pr:
        return "missing 'pr_number'"
    if not isinstance(pr["pr_number"], int):
        return f"'pr_number' must be an int, got {type(pr['pr_number']).__name__}"
    if "head_sha" not in pr:
        return "missing 'head_sha'"
    if not isinstance(pr["head_sha"], str) or not pr["head_sha"]:
        return "'head_sha' must be a non-empty string"
    return None


# ---------------------------------------------------------------------------
# ledger-event invocation
# ---------------------------------------------------------------------------

def _build_details(pr: dict[str, Any]) -> dict[str, Any]:
    """Extract the subset of PR metadata to put into the 'details' field."""
    return {
        k: pr[k]
        for k in (
            "title",
            "url",
            "state",
            "additions",
            "deletions",
            "changed_files",
            "labels",
            "if_tier",
            "if_already_applied_in_local_main",
            "if_in_upstream_release_10_0",
            "if_in_upstream_main",
            "created_at",
            "merged_at",
        )
        if k in pr
    }


def _invoke_ledger_event(
    *,
    pr: dict[str, Any],
    ledger_path: Path,
    actor: str,
    dry_run: bool,
    no_commit: bool = False,
) -> str | None:
    """Call ledger-event.py for *pr*.  Returns error string on failure, else None."""
    details = _build_details(pr)
    cmd: list[str] = [
        sys.executable,
        str(_LEDGER_EVENT_SCRIPT),
        "--event", "discovered",
        "--pr-number", str(pr["pr_number"]),
        "--head-sha", str(pr["head_sha"]),
        "--actor", actor,
        "--details-json", json.dumps(details, separators=(",", ":")),
        "--ledger-path", str(ledger_path),
    ]
    if dry_run:
        cmd.append("--dry-run")
    if no_commit:
        cmd.append("--no-commit")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr_text = result.stderr.strip()
        return f"ledger-event.py exited {result.returncode}: {stderr_text or result.stdout.strip()}"
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bulk-import PR candidates into patch-ledger.jsonl as 'discovered' events."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            "Path to the JSON candidate list "
            f"(default: {_DEFAULT_INPUT} if it exists)"
        ),
    )
    parser.add_argument(
        "--ledger-path",
        type=Path,
        default=Path(_DEFAULT_LEDGER),
        help=f"Path to the ledger file (default: {_DEFAULT_LEDGER})",
    )
    parser.add_argument(
        "--actor",
        default=_DEFAULT_ACTOR,
        help=f"Actor identifier written into each ledger event (default: {_DEFAULT_ACTOR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do not write to the ledger; print what would be added. "
            "Passed through to ledger-event.py."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=(
            f"Progress is printed every N PRs (default: {_DEFAULT_BATCH_SIZE}). "
            "Does not affect parallelism — processing is sequential."
        ),
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help=(
            "Write events to the ledger but skip per-event git commits. "
            "The caller is responsible for making a single commit after the run. "
            "Passed through to ledger-event.py."
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:  # noqa: C901
    args = parse_args(argv)

    # Resolve input path
    input_path: Path
    if args.input is not None:
        input_path = args.input
    elif _DEFAULT_INPUT.exists():
        input_path = _DEFAULT_INPUT
    else:
        print(
            json.dumps(
                {
                    "error": (
                        "--input not specified and default path does not exist: "
                        + str(_DEFAULT_INPUT)
                    )
                }
            ),
            file=sys.stderr,
        )
        return 2

    ledger_path: Path = args.ledger_path

    # Load candidates
    candidates = _load_candidates(input_path)
    total = len(candidates)
    print(f"Loaded {total} candidate(s) from {input_path}")

    # Read existing ledger to deduplicate
    existing_pr_numbers = _load_existing_pr_numbers(ledger_path)
    print(
        f"Existing ledger has {len(existing_pr_numbers)} unique PR number(s) "
        f"(from {ledger_path})"
    )

    added = 0
    skipped = 0
    errors: list[str] = []

    for idx, pr in enumerate(candidates, start=1):
        # Validate shape
        validation_error = _validate_candidate(pr)
        if validation_error is not None:
            msg = f"candidate[{idx}] invalid: {validation_error}"
            errors.append(msg)
            print(f"  ERROR {msg}", file=sys.stderr)
            continue

        pr_number: int = pr["pr_number"]

        # Skip if already in ledger
        if pr_number in existing_pr_numbers:
            skipped += 1
            continue

        # Progress reporting
        if idx == 1 or idx % args.batch_size == 0 or idx == total:
            action = "dry-run" if args.dry_run else "discovered"
            print(f"[{idx}/{total}] {action} pr#{pr_number}")

        # Invoke ledger-event.py
        err = _invoke_ledger_event(
            pr=pr,
            ledger_path=ledger_path,
            actor=args.actor,
            dry_run=args.dry_run,
            no_commit=args.no_commit,
        )
        if err is not None:
            errors.append(f"pr#{pr_number}: {err}")
            print(f"  ERROR pr#{pr_number}: {err}", file=sys.stderr)
        else:
            added += 1
            # Track in-memory so we don't double-add within the same run
            # (only relevant if ledger-event fails to write and we re-encounter).
            existing_pr_numbers.add(pr_number)

    summary: dict[str, Any] = {
        "total": total,
        "added": added,
        "skipped": skipped,
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
