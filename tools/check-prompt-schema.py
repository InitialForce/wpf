#!/usr/bin/env python3
"""
check-prompt-schema.py — Validate .if-fork/prompts/*.md files against required schema.

Each prompt file (except preamble.md) must contain:
  - ## Inherits from preamble.md
  - ## Allowed tools
  - ## Inputs
  - ## Output contract

Exit codes:
  0 — all files valid
  2 — one or more files have schema violations
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REQUIRED_SECTIONS: list[str] = [
    "## Inherits from preamble.md",
    "## Allowed tools",
    "## Inputs",
    "## Output contract",
]

PREAMBLE_FILENAME = "preamble.md"
PREAMBLE_REQUIRED_HEADING = "## Preamble (Inheritable)"


def check_file(path: Path) -> list[str]:
    """Return a list of error strings for the given prompt file, or [] if valid."""
    errors: list[str] = []

    content = path.read_text(encoding="utf-8")
    if not content.strip():
        errors.append(f"{path.name}: file is empty")
        return errors

    if path.name == PREAMBLE_FILENAME:
        if PREAMBLE_REQUIRED_HEADING not in content:
            errors.append(
                f"{path.name}: preamble must contain '{PREAMBLE_REQUIRED_HEADING}'"
            )
        return errors

    for section in REQUIRED_SECTIONS:
        if section not in content:
            errors.append(f"{path.name}: missing required section '{section}'")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate .if-fork/prompts/*.md schema",
    )
    parser.add_argument(
        "--prompts-dir",
        default=".if-fork/prompts",
        help="Directory containing prompt .md files (default: .if-fork/prompts)",
    )
    args = parser.parse_args(argv)

    prompts_dir = Path(args.prompts_dir)
    if not prompts_dir.is_dir():
        result = {
            "valid": False,
            "files_checked": 0,
            "errors": [f"prompts directory not found: {prompts_dir}"],
        }
        print(json.dumps(result))
        return 2

    md_files = sorted(prompts_dir.glob("*.md"))
    if not md_files:
        result = {
            "valid": False,
            "files_checked": 0,
            "errors": ["no .md files found in prompts directory"],
        }
        print(json.dumps(result))
        return 2

    all_errors: list[str] = []
    for md_file in md_files:
        all_errors.extend(check_file(md_file))

    result = {
        "valid": len(all_errors) == 0,
        "files_checked": len(md_files),
        "errors": all_errors,
    }
    print(json.dumps(result))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
