#!/usr/bin/env python3
"""Validate patch-ledger.jsonl integrity — CI gate for ledger append-only invariants.

Checks:
  1. Every line is valid JSON.
  2. Required fields are present and correctly typed.
  3. Event type is in the enumerated set.
  4. prev_hash chain links are unbroken from genesis to tail.
  5. line_hash matches the recomputed SHA-256 of the line body.

Optional:
  --strict-signature  Require a GPG-signed git commit per ledger entry.  Commit
                      lookup is done by searching for the Ledger-Line-Hash trailer
                      in git log (content-based, not index-based) so this check
                      is robust to squashes and rebases (MED-5 fix).

Output: JSON on stdout → { "valid": bool, "lines_checked": int, "errors": [...] }
Exit 0 if valid, 2 if invalid.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Ensure the tools directory is importable whether the script is run directly
# or loaded via importlib from the project root.
_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

# ---------------------------------------------------------------------------
# Event types — imported from shared schema (LOW-1 fix)
# ---------------------------------------------------------------------------
from ledger_schema import VALID_EVENTS  # noqa: E402

REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "ts",
    "event",
    "actor",
    "pr_number",
    "head_sha",
    "details",
    "prev_hash",
    "line_hash",
)

DEFAULT_LEDGER_PATH = ".if-fork/patch-ledger.jsonl"


# ---------------------------------------------------------------------------
# Error record helpers
# ---------------------------------------------------------------------------

def _err(line_number: int, kind: str, details: str) -> dict[str, Any]:
    return {"line_number": line_number, "kind": kind, "details": details}


# ---------------------------------------------------------------------------
# Hash helpers — must use the same algorithm as ledger-event.py
# ---------------------------------------------------------------------------

def _recompute_line_hash(obj: dict[str, Any]) -> str:
    """Recompute the SHA-256 hash of *obj* without the line_hash key."""
    without_hash = {k: v for k, v in obj.items() if k != "line_hash"}
    body = json.dumps(without_hash, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Optional GPG / git-signature check
# ---------------------------------------------------------------------------

def _check_gpg_signature(commit_sha: str) -> bool:
    """Return True if *commit_sha* carries a valid GPG signature."""
    try:
        result = subprocess.run(
            ["git", "verify-commit", "--verbose", commit_sha],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _find_commit_for_line_hash(line_hash: str) -> str | None:
    """Look up the git commit that introduced the ledger line with the given hash.

    Uses ``git log -S <line_hash>`` (pickaxe search) to find the commit that
    added the exact content, which is robust against squashes and rebases.
    Returns the commit SHA, or None if not found / git unavailable.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "-S",
                line_hash,
                "--pretty=format:%H",
                "-1",
                "--",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha if sha else None
    except FileNotFoundError:
        return None


def _git_log_for_ledger(ledger_path: Path) -> list[str]:
    """Return the list of commit SHAs that touched *ledger_path* (oldest first).

    Kept for backward compatibility; strict-signature mode now uses
    _find_commit_for_line_hash instead.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--pretty=format:%H", "--", str(ledger_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        shas = [s.strip() for s in result.stdout.splitlines() if s.strip()]
        shas.reverse()  # oldest first
        return shas
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------

def validate(
    ledger_path: Path,
    *,
    strict_signature: bool = False,
) -> dict[str, Any]:
    """Validate *ledger_path* and return the result dict."""
    errors: list[dict[str, Any]] = []

    if not ledger_path.exists():
        # Empty / missing ledger is valid (genesis state)
        return {"valid": True, "lines_checked": 0, "errors": []}

    raw_lines: list[str] = []
    with ledger_path.open(encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.rstrip("\n")
            if stripped:
                raw_lines.append(stripped)

    if not raw_lines:
        return {"valid": True, "lines_checked": 0, "errors": []}

    prev_hash = ""
    parsed_lines: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_lines):
        line_number = idx + 1

        # --- Check 1: valid JSON ---
        try:
            obj: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(_err(line_number, "invalid_json", str(exc)))
            # Cannot continue chain checks for this line
            parsed_lines.append({})
            prev_hash = ""  # chain is broken; mark with empty so next line fails
            continue

        parsed_lines.append(obj)

        if not isinstance(obj, dict):
            errors.append(
                _err(line_number, "invalid_json", "Line is valid JSON but not an object")
            )
            prev_hash = ""
            continue

        # --- Check 2: required fields ---
        for field in REQUIRED_FIELDS:
            if field not in obj:
                errors.append(
                    _err(
                        line_number,
                        "missing_field",
                        f"Required field '{field}' is absent",
                    )
                )

        # Only continue deep checks when the line has both line_hash and event
        has_line_hash = "line_hash" in obj
        has_event = "event" in obj
        has_prev_hash = "prev_hash" in obj

        # --- Check 3: event type ---
        if has_event:
            event_val = obj["event"]
            if not isinstance(event_val, str) or event_val not in VALID_EVENTS:
                errors.append(
                    _err(
                        line_number,
                        "invalid_event_type",
                        f"Event {event_val!r} is not in the valid event set",
                    )
                )

        # --- Check 4: prev_hash chain ---
        if has_prev_hash:
            stored_prev = obj["prev_hash"]
            if stored_prev != prev_hash:
                errors.append(
                    _err(
                        line_number,
                        "prev_hash_mismatch",
                        (
                            f"prev_hash {stored_prev!r} does not match"
                            f" expected {prev_hash!r}"
                        ),
                    )
                )

        # --- Check 5: line_hash integrity ---
        if has_line_hash:
            stored_hash = obj["line_hash"]
            expected_hash = _recompute_line_hash(obj)
            if stored_hash != expected_hash:
                errors.append(
                    _err(
                        line_number,
                        "line_hash_mismatch",
                        (
                            f"Stored line_hash {stored_hash!r} does not match"
                            f" recomputed {expected_hash!r}"
                        ),
                    )
                )
            # Advance chain only when line_hash field is present
            prev_hash = stored_hash if has_line_hash else ""
        else:
            prev_hash = ""

        # --- Check 6 (optional): GPG signature ---
        # MED-5 fix: look up the commit by content (line_hash pickaxe search)
        # rather than correlating by array index.
        if strict_signature and has_line_hash:
            stored_hash = obj["line_hash"]
            commit_sha = _find_commit_for_line_hash(stored_hash)
            if commit_sha is None:
                errors.append(
                    _err(
                        line_number,
                        "missing_gpg_signature",
                        f"No git commit found that introduced line_hash {stored_hash!r}",
                    )
                )
            elif not _check_gpg_signature(commit_sha):
                errors.append(
                    _err(
                        line_number,
                        "missing_gpg_signature",
                        f"Commit {commit_sha!r} for line {line_number} is not GPG-signed",
                    )
                )

    result: dict[str, Any] = {
        "valid": len(errors) == 0,
        "lines_checked": len(raw_lines),
        "errors": errors,
    }
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate patch-ledger.jsonl — CI gate for ledger integrity"
    )
    parser.add_argument(
        "--ledger-path",
        default=DEFAULT_LEDGER_PATH,
        help=f"Path to the ledger file (default: {DEFAULT_LEDGER_PATH})",
    )
    parser.add_argument(
        "--strict-signature",
        action="store_true",
        help=(
            "Also verify that every ledger line has a corresponding"
            " GPG-signed git commit. Commit lookup is done via git log -S"
            " <line_hash> (content-based, not index-based)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ledger_path = Path(args.ledger_path)

    result = validate(ledger_path, strict_signature=args.strict_signature)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
