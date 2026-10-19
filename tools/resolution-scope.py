#!/usr/bin/env python3
"""Write the part of a branch diff that a conflict resolution wrote.

The resolution review applies its file and line rules to this scope only.
  cherry-pick: the whole branch diff BASE_SHA..BRANCH_SHA. The branch is one
               commit, the upstream PR applied by the pipeline, so all of it
               is the resolution's responsibility.
  sync:        git's own merge of BASE_SHA and UPSTREAM_SHA (with conflict
               markers in the conflicted files) -> BRANCH_SHA. Everything
               else in BASE_SHA..BRANCH_SHA is upstream release/10.0, merged
               cleanly by git; the reviewer sees it as context only.

resolution-review.yml runs this in the prepare job (for the reviewer) and in
the record job (for the hard-fail scan), so both use the same scope.

Usage:
  python tools/resolution-scope.py --kind sync --base-sha SHA --upstream-sha SHA \\
      --branch-sha SHA --out DIR

Writes DIR/resolution.diff and DIR/resolution-files.txt (one path per line).
Run it in a clone that has all three commits. Exit codes: 0 written,
1 bad input or a git failure.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIFF = ["diff", "--no-color", "--no-ext-diff", "--no-renames"]


def _git(*args: str, ok: tuple[int, ...] = (0,)) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode not in ok:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()}")
    return r.stdout


def scope_start(kind: str, base: str, upstream: str) -> str:
    """The tree or commit the resolution diff starts from."""
    if kind == "cherry-pick":
        return base
    # Exit 1 means the merge has conflicts, which is the case here.
    out = _git("merge-tree", "--write-tree", base, upstream, ok=(0, 1))
    auto = out.splitlines()[0] if out else ""
    if not SHA_RE.match(auto):
        raise RuntimeError("could not recreate git's own merge")
    return auto


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", choices=("cherry-pick", "sync"), required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--branch-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    for sha in (args.base_sha, args.upstream_sha, args.branch_sha):
        if not SHA_RE.match(sha):
            print("SHAs must be 40 lowercase hex characters", file=sys.stderr)
            return 1
    try:
        start = scope_start(args.kind, args.base_sha, args.upstream_sha)
        diff = _git(*DIFF, start, args.branch_sha)
        files = _git(*DIFF, "--name-only", start, args.branch_sha)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "resolution.diff").write_text(diff, encoding="utf-8")
    (args.out / "resolution-files.txt").write_text(files, encoding="utf-8")
    print(f"{len(files.splitlines())} files in the resolution scope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
