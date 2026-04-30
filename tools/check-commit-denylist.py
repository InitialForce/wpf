#!/usr/bin/env python3
"""check-commit-denylist.py — Refuse cherry-pick when the upstream commit (or any
chained "(cherry picked from commit ...)" ancestor) appears in commit_denylist.

Used by cherry-pick automation as a hard gate BEFORE applying any patch. Catches:
  1. A direct cherry-pick whose --from SHA is on the list.
  2. A re-pick of a fork-side commit whose body contains
     "(cherry picked from commit <sha>)" pointing at a denied SHA.
  3. A re-pick after the upstream commit was rebased/squashed: any of the parent
     SHAs threaded through the cherry-pick trailer chain.

Exit codes:
  0 — not on denylist (safe to proceed)
  1 — error (bad config, missing file, subprocess failure)
  2 — denied (one or more SHAs in the chain match the denylist)

Output (stdout): JSON with either:
  { "verdict": "ok", "checked_shas": [...] }
  { "verdict": "denied",
    "matched": {"sha": "...", "pr": ..., "reason": "..."},
    "checked_shas": [...] }
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

CHERRY_PICK_TRAILER = re.compile(
    r"\(cherry picked from commit ([0-9a-f]{7,40})\)",
    re.IGNORECASE,
)


def _die(msg: str, code: int = 1) -> None:
    print(json.dumps({"verdict": "error", "error": msg}))
    sys.exit(code)


def load_denylist(config_path: Path) -> list[dict[str, Any]]:
    if not config_path.is_file():
        _die(f"config file not found: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        _die(f"failed to parse {config_path}: {exc}")
    raw = data.get("commit_denylist") if isinstance(data, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        _die("commit_denylist must be a list")
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            _die(f"commit_denylist entry not a mapping: {entry!r}")
        sha = entry.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{12,40}", sha):
            _die(f"commit_denylist entry has invalid sha: {entry!r}")
        out.append(entry)
    return out


def collect_chain(sha: str, repo: Path) -> list[str]:
    """Return [sha, parents...] including any (cherry picked from commit X) trailers
    found by walking the local commit message and any reachable upstream copies."""
    seen: list[str] = []
    pending = [sha.lower()]
    while pending:
        cur = pending.pop()
        if cur in seen:
            continue
        seen.append(cur)
        # Try to read commit message; if SHA is unknown locally, skip — we still keep
        # it in seen so caller can match against denylist by prefix.
        try:
            msg = subprocess.check_output(
                ["git", "log", "-1", "--format=%B", cur],
                cwd=repo,
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            continue
        for m in CHERRY_PICK_TRAILER.finditer(msg):
            pending.append(m.group(1).lower())
    return seen


def is_denied(sha: str, denylist: list[dict[str, Any]]) -> dict[str, Any] | None:
    sha = sha.lower()
    for entry in denylist:
        denied = entry["sha"].lower()
        if sha.startswith(denied[:12]) or denied.startswith(sha[:12]):
            return entry
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check whether a candidate commit (and its cherry-pick ancestry) is on "
            "the commit_denylist in .if-fork/config.yaml. Hard gate for cherry-pick "
            "automation."
        )
    )
    parser.add_argument(
        "sha",
        help="Candidate commit SHA (the upstream commit you would cherry-pick, or a "
             "local commit that re-picks one). Walks (cherry picked from commit ...) "
             "trailers up the chain.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(".if-fork/config.yaml"),
        help="Path to config.yaml (default: .if-fork/config.yaml)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Path to git repo root (default: current dir)",
    )
    args = parser.parse_args()

    denylist = load_denylist(args.config)
    if not denylist:
        print(json.dumps({"verdict": "ok", "checked_shas": [args.sha], "note": "denylist empty"}))
        return 0

    chain = collect_chain(args.sha, args.repo)
    for candidate in chain:
        match = is_denied(candidate, denylist)
        if match is not None:
            print(json.dumps({
                "verdict": "denied",
                "matched": {
                    "sha": match["sha"],
                    "pr": match.get("pr"),
                    "reason": match["reason"].strip(),
                },
                "matched_via": candidate,
                "checked_shas": chain,
            }))
            return 2

    print(json.dumps({"verdict": "ok", "checked_shas": chain}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
