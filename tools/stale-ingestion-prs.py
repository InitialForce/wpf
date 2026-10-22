#!/usr/bin/env python3
"""List open ingestion PRs whose upstream PR the pipeline has already finished.

Usage:
  python tools/stale-ingestion-prs.py --ledger .if-fork/patch-ledger.jsonl \\
      --open-prs <file>

<file> is ``gh pr list --base if/main --state open --json number,headRefName``.
Only PRs from ``claude/cherry-pick-pr-<n>`` count.

A PR is stale when the newest ledger event for upstream PR <n> that ends
ingestion (``graduated_upstream``, ``escalated``, ``rejected``) is later than
every event that keeps it going (``approved``, ``ingestion_requested``,
``cherry_picked``, ``resolution_approved``, ``resolution_rejected``). Such a
PR waits for nothing: no run will update it and nobody is asked to merge it.
pr-discovery.yml closes each one with tools/close-ingestion-pr.sh. This also
catches PRs left open before ingestion closed them itself (for example #27,
upstream #9215).

Stdout: a JSON list of ``{"fork_pr", "upstream_pr", "reason"}``.
Exit codes: 0 ok, 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

BRANCH_RE = re.compile(r"^claude/cherry-pick-pr-([0-9]+)$")
STOP_EVENTS = frozenset(["graduated_upstream", "escalated", "rejected"])
ACTIVE_EVENTS = frozenset(
    [
        "approved",
        "ingestion_requested",
        "cherry_picked",
        "resolution_approved",
        "resolution_rejected",
    ]
)


def stale_prs(
    ledger_entries: list[dict[str, Any]], open_prs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    last_stop: dict[int, tuple[int, str]] = {}
    last_active: dict[int, int] = {}
    for index, entry in enumerate(ledger_entries):
        number = entry.get("pr_number")
        if not isinstance(number, int) or number <= 0:
            continue
        event = entry.get("event")
        if event in STOP_EVENTS:
            details = entry.get("details") or {}
            why = details.get("reason") or details.get("detection_method") or ""
            last_stop[number] = (index, f"{event} ({why})" if why else str(event))
        elif event in ACTIVE_EVENTS:
            last_active[number] = index

    stale = []
    for pr in open_prs:
        m = BRANCH_RE.match(str(pr.get("headRefName", "")))
        fork_pr = pr.get("number")
        if not m or not isinstance(fork_pr, int):
            continue
        upstream = int(m.group(1))
        stop = last_stop.get(upstream)
        if stop is None or stop[0] < last_active.get(upstream, -1):
            continue
        stale.append({"fork_pr": fork_pr, "upstream_pr": upstream, "reason": stop[1]})
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--open-prs", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        entries = [
            json.loads(line)
            for line in args.ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        open_prs = json.loads(args.open_prs.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(open_prs, list) or not all(isinstance(p, dict) for p in open_prs):
        print("error: --open-prs must hold a JSON list of PRs", file=sys.stderr)
        return 2
    print(json.dumps(stale_prs(entries, open_prs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
