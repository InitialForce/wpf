#!/usr/bin/env python3
"""Decide whether a new upstream dotnet/wpf stable release needs adopting.

Used by .github/workflows/upstream-release-poll.yml.

Subcommands
-----------
latest-tag --prefix v10.0. < ls-remote-output
    Read ``git ls-remote --tags`` output and print ``<tag> <commit-sha>`` for the
    newest stable tag ``<prefix><N>`` (N numeric, below 100). dotnet/wpf also
    carries SDK-band tags such as v10.0.104 that point at old commits; those
    are ignored. Prints nothing when no tag matches.

decide --tag v10.0.12 --adopted true|false < runs.json
    Read ``gh run list --json displayTitle,status,conclusion`` output for
    upstream-stable-adoption.yml and print one word:
      skip-adopted   the mirror branch already contains the tag
      skip-running   an adoption run for this tag is queued or running
      skip-gave-up   adoption already failed MAX_FAILED_ATTEMPTS times for it
      dispatch       send repository_dispatch upstream-stable-released
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

MAX_FAILED_ATTEMPTS = 3
SDK_BAND_FLOOR = 100


def latest_tag(ls_remote: str, prefix: str) -> tuple[str, str] | None:
    """Return (tag, commit sha) of the newest stable ``<prefix><N>`` tag."""
    pattern = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    shas: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in ls_remote.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        sha, ref = parts
        name = ref[len("refs/tags/") :]
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha
        else:
            shas[name] = sha
    best: tuple[int, str] | None = None
    for name in shas:
        m = pattern.match(name)
        if not m:
            continue
        patch = int(m.group(1))
        if patch >= SDK_BAND_FLOOR:
            continue
        if best is None or patch > best[0]:
            best = (patch, name)
    if best is None:
        return None
    tag = best[1]
    return tag, peeled.get(tag, shas[tag])


def decide(tag: str, adopted: bool, runs: list[dict[str, Any]]) -> str:
    """Return the action for *tag* given prior adoption runs."""
    if adopted:
        return "skip-adopted"
    mine = [r for r in runs if tag in str(r.get("displayTitle", "")).split()]
    if any(r.get("status") != "completed" for r in mine):
        return "skip-running"
    failed = [r for r in mine if r.get("conclusion") not in ("success", "skipped")]
    if len(failed) >= MAX_FAILED_ATTEMPTS:
        return "skip-gave-up"
    return "dispatch"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_latest = sub.add_parser("latest-tag")
    p_latest.add_argument("--prefix", required=True)
    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--tag", required=True)
    p_decide.add_argument("--adopted", choices=["true", "false"], required=True)
    args = parser.parse_args(argv)

    if args.cmd == "latest-tag":
        found = latest_tag(sys.stdin.read(), args.prefix)
        if found:
            print(f"{found[0]} {found[1]}")
        return 0

    raw = sys.stdin.read().strip()
    runs = json.loads(raw) if raw else []
    print(decide(args.tag, args.adopted == "true", runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
