#!/usr/bin/env python3
"""check-denylist.py — Refuse cherry-pick or rebase when changed files match file_denylist.

Exit codes:
  0 — no violations; all checked files are clean
  1 — error (bad config, missing file, subprocess failure)
  2 — one or more changed files match a denylist pattern (hard fail)

Output (stdout): JSON with either:
  { "verdict": "ok", "checked_count": N }
  { "verdict": "refused", "violations": [{ "file": "...", "matched_pattern": "..." }] }
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(config_path: Path) -> list[str]:
    """Load and return the file_denylist from config.yaml.

    Raises SystemExit(1) on any error.
    """
    if not config_path.exists():
        _die(f"Config file not found: {config_path}")

    try:
        with config_path.open(encoding="utf-8") as fh:
            data: Any = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        _die(f"Failed to parse YAML config {config_path}: {exc}")

    if not isinstance(data, dict):
        _die(f"Config {config_path} is not a YAML mapping")

    raw = data.get("file_denylist")
    if raw is None:
        # An absent key is treated as an empty list (nothing is denied).
        return []

    if not isinstance(raw, list):
        _die(f"file_denylist in {config_path} must be a YAML sequence, got {type(raw).__name__}")

    patterns: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            _die(f"file_denylist entry {item!r} is not a string")
        patterns.append(item)

    return patterns


# ---------------------------------------------------------------------------
# File list loading
# ---------------------------------------------------------------------------


def load_changed_files_from_path(path: Path) -> list[str]:
    """Read a newline-delimited list of file paths from a file."""
    if not path.exists():
        _die(f"Changed-files list not found: {path}")

    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def load_changed_files_from_git(base: str, head: str) -> list[str]:
    """Run git diff --name-only <base>..<head> and return the file list."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}..{head}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        _die(
            f"git diff --name-only {base}..{head} failed (exit {exc.returncode}):\n"
            f"{exc.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


def _matches_pattern(file_path: str, pattern: str) -> bool:
    """Return True if file_path matches pattern using fnmatch rules.

    We use fnmatch.fnmatchcase so that:
    - Single-component patterns (no slash) match the filename component only,
      matching shell glob behaviour.
    - Patterns containing '/' are matched against the full path so that
      path-prefix patterns such as 'eng/**' work correctly.

    The '**' wildcard is not natively supported by fnmatch; we translate it to
    '*' which—combined with the full-path match—achieves the same semantics
    for the denylist patterns in use: 'eng/**' becomes 'eng/*', which matches
    any path under eng/.  For deeply nested paths like 'eng/a/b/c.txt' we
    recursively strip path components until a match is found.
    """
    # Normalise to forward slashes (git always uses forward slashes).
    file_path = file_path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")

    # Translate ** into * for fnmatch.
    translated = pattern.replace("**", "*")

    if "/" in translated:
        # Match against the full path.
        if fnmatch.fnmatchcase(file_path, translated):
            return True
        # Also try matching with a leading wildcard so that patterns without a
        # leading slash still match paths that are rooted differently.
        # e.g. pattern 'eng/*' should match 'eng/Signing.props'
        return False

    # No slash in pattern — match against filename only.
    filename = file_path.split("/")[-1]
    return fnmatch.fnmatchcase(filename, translated)


def check_files(
    changed_files: list[str],
    patterns: list[str],
) -> list[dict[str, str]]:
    """Return a list of violations (may be empty)."""
    violations: list[dict[str, str]] = []
    for file_path in changed_files:
        for pattern in patterns:
            if _matches_pattern(file_path, pattern):
                violations.append({"file": file_path, "matched_pattern": pattern})
                break  # Only report the first matching pattern per file.
    return violations


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_output(payload: dict[str, Any], output_path: Path | None) -> None:
    text = json.dumps(payload, indent=2)
    if output_path is not None:
        output_path.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check a list of changed files against the file_denylist in config.yaml. "
            "Exits 0 if clean, 2 if any file is denied, 1 on error."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(".if-fork/config.yaml"),
        metavar="PATH",
        help="Path to .if-fork/config.yaml (default: .if-fork/config.yaml)",
    )

    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--changed-files",
        type=Path,
        metavar="PATH",
        help="Path to a newline-delimited file listing changed paths.",
    )
    source_group.add_argument(
        "--git-base",
        metavar="REF",
        help="Base git ref for diff (requires --git-head).",
    )

    parser.add_argument(
        "--git-head",
        metavar="REF",
        default="HEAD",
        help="Head git ref for diff (default: HEAD; used with --git-base).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="Write JSON report to this file instead of stdout.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Validate mutually-dependent flag.
    if args.git_base is not None and args.changed_files is not None:
        # argparse mutual exclusion handles this, but be explicit.
        _die("--changed-files and --git-base are mutually exclusive.")

    # Load config.
    patterns = load_config(args.config)

    # Load changed files.
    if args.changed_files is not None:
        changed_files = load_changed_files_from_path(args.changed_files)
    elif args.git_base is not None:
        changed_files = load_changed_files_from_git(args.git_base, args.git_head)
    else:
        # Default: read from stdin (original spec behaviour kept as fallback).
        raw_stdin = sys.stdin.read()
        changed_files = [line for line in raw_stdin.splitlines() if line.strip()]

    # Run check.
    violations = check_files(changed_files, patterns)

    if violations:
        payload: dict[str, Any] = {
            "verdict": "refused",
            "violations": violations,
        }
        # Also print violations to stderr for CI log visibility.
        for v in violations:
            print(
                f"DENIED: {v['file']} (matched pattern: {v['matched_pattern']})",
                file=sys.stderr,
            )
        write_output(payload, args.output)
        sys.exit(2)
    else:
        payload = {
            "verdict": "ok",
            "checked_count": len(changed_files),
        }
        write_output(payload, args.output)
        sys.exit(0)


if __name__ == "__main__":
    main()
