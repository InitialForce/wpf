#!/usr/bin/env python3
"""Check a change's file list against the pipeline's path rules.

One rule, used in two places so they cannot drift apart:
  - pr-ingestion (finish-ingestion-branch.sh) and nightly-rebase (finalize)
    run it before they push, and record an escalation instead of opening a
    PR that automerge would refuse;
  - automerge-verify.sh runs it again on the PR it is about to merge.

Rules:
  fork_owned          (cherry-pick and sync) no path the fork owns: the
                      pipeline's workflows, code, policy, ledger, bot key,
                      tests, docs and packaging, and agent configuration.
                      Upstream dotnet/wpf has none of them (it has .github/,
                      but the fork deleted upstream's workflows), so an
                      upstream change must never touch them.
  outside_allowlist   (cherry-pick only) every path is under src/ or eng/.
                      eng/ is still subject to file_denylist.

Usage:
  python tools/path-policy.py --kind cherry-pick|sync --changed-files FILE

Prints one JSON object: {"ok": bool, "reason": str, "fork_owned": [...],
"outside_allowlist": [...]}. "reason" is fork_owned_paths,
paths_outside_allowlist or "" (fork-owned wins when both hit).
Exit codes: 0 ok, 2 a rule failed, 1 bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Same paths as the fork-owned part of file_denylist in .if-fork/config.yaml.
FORK_OWNED = re.compile(
    r"^(\.github/|\.if-fork/|tools/|tests/|test/|docs/|perf/|data/"
    r"|packaging/InitialForce\.[^/]*/|FORK\.md$|NOTICE\.md$|pyproject\.toml$"
    r"|(.*/)?\.claude/|(.*/)?(CLAUDE\.md|CLAUDE\.local\.md|AGENTS\.md|\.mcp\.json)$)"
)
CHERRY_PICK_ALLOWED = re.compile(r"^(src|eng)/")
KINDS = ("cherry-pick", "sync")


def check(paths: list[str], kind: str) -> dict[str, object]:
    """Apply the rules for *kind* to *paths*."""
    fork_owned = [p for p in paths if FORK_OWNED.match(p)]
    outside = (
        [p for p in paths if not CHERRY_PICK_ALLOWED.match(p)] if kind == "cherry-pick" else []
    )
    if fork_owned:
        reason = "fork_owned_paths"
    elif outside:
        reason = "paths_outside_allowlist"
    else:
        reason = ""
    return {
        "ok": not reason,
        "reason": reason,
        "fork_owned": fork_owned,
        "outside_allowlist": outside,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--changed-files", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        text = args.changed_files.read_text(encoding="utf-8")
    except OSError as exc:
        print(json.dumps({"ok": False, "reason": f"cannot read file list: {exc}"}))
        return 1
    paths = [line for line in text.splitlines() if line.strip()]
    result = check(paths, args.kind)
    print(json.dumps(result))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
