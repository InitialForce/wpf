#!/usr/bin/env python3
"""Check that an upstream PR head was approved by review, per the patch ledger.

Usage:
  python tools/check-approval.py --ledger .if-fork/patch-ledger.jsonl \\
      --pr-number 10245 --head-sha <40-hex sha>

The pair (pr_number, head_sha) is approved when the ledger has an ``approved``
event for exactly that pair and no later ``rejected`` or ``escalated`` event for
it. Events for other head SHAs of the same PR do not count.

Exit codes:
  0  approved
  1  bad input or unreadable ledger
  2  not approved (never approved, or a later rejected/escalated event)

Stdout: one JSON object, {"approved": bool, "reason": "..."}.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

OUTCOME_EVENTS = {"approved", "rejected", "escalated"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PR_RE = re.compile(r"^[0-9]+$")


def last_outcome(ledger_text: str, pr_number: int, head_sha: str) -> str | None:
    """Return the last outcome event for the pair, or None if there is none."""
    outcome: str | None = None
    for line in ledger_text.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        if entry.get("event") not in OUTCOME_EVENTS:
            continue
        if entry.get("pr_number") != pr_number or entry.get("head_sha") != head_sha:
            continue
        outcome = str(entry["event"])
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)

    if not PR_RE.match(args.pr_number) or not SHA_RE.match(args.head_sha):
        print(json.dumps({"approved": False, "reason": "invalid pr_number or head_sha"}))
        return 1
    try:
        text = args.ledger.read_text(encoding="utf-8")
        outcome = last_outcome(text, int(args.pr_number), args.head_sha)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"approved": False, "reason": f"cannot read ledger: {exc}"}))
        return 1

    if outcome == "approved":
        print(json.dumps({"approved": True, "reason": "approved"}))
        return 0
    reason = "no review outcome for this head" if outcome is None else f"last outcome is {outcome}"
    print(json.dumps({"approved": False, "reason": reason}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
