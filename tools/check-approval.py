#!/usr/bin/env python3
"""Check that an upstream PR head was approved by review, per the patch ledger.

Usage:
  python tools/check-approval.py --ledger .if-fork/patch-ledger.jsonl \\
      --pr-number 10245 --head-sha <40-hex sha> [--resolution-branch-sha <40-hex sha>]

The pair (pr_number, head_sha) is approved when the ledger has an ``approved``
event for exactly that pair and no later ``rejected`` or ``escalated`` event for
it. Events for other head SHAs of the same PR do not count.

With --resolution-branch-sha (automerge-verify.sh passes the PR head of a
cherry-pick whose conflicts Claude resolved), the pair must also have a
``resolution_approved`` event whose details.branch_sha is that commit, and no
later ``resolution_rejected`` event for that commit. An approval for any other
commit of the branch does not count, so a new push needs a new review.

--resolution-only checks just that resolution rule and skips the review
outcome. automerge-verify.sh uses it for a sync merge, recorded with
pr_number 0 and the upstream release/10.0 SHA as head_sha.

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
RESOLUTION_EVENTS = {"resolution_approved", "resolution_rejected"}
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


def resolution_outcome(
    ledger_text: str, pr_number: int, head_sha: str, branch_sha: str
) -> str | None:
    """Return the last resolution review event for *branch_sha*, or None.

    A resolution_approved counts only for the same (pr_number, head_sha); a
    resolution_rejected for that commit counts whatever pair it names.
    """
    outcome: str | None = None
    for line in ledger_text.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        event = entry.get("event")
        if event not in RESOLUTION_EVENTS:
            continue
        details = entry.get("details")
        if not isinstance(details, dict) or details.get("branch_sha") != branch_sha:
            continue
        if event == "resolution_rejected":
            outcome = event
        elif entry.get("pr_number") == pr_number and entry.get("head_sha") == head_sha:
            outcome = event
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--resolution-branch-sha")
    parser.add_argument("--resolution-only", action="store_true")
    args = parser.parse_args(argv)
    if args.resolution_only and args.resolution_branch_sha is None:
        parser.error("--resolution-only needs --resolution-branch-sha")

    branch_sha = args.resolution_branch_sha
    if (
        not PR_RE.match(args.pr_number)
        or not SHA_RE.match(args.head_sha)
        or (branch_sha is not None and not SHA_RE.match(branch_sha))
    ):
        print(json.dumps({"approved": False, "reason": "invalid pr_number or SHA"}))
        return 1
    try:
        text = args.ledger.read_text(encoding="utf-8")
        outcome = last_outcome(text, int(args.pr_number), args.head_sha)
        resolution = (
            None
            if branch_sha is None
            else resolution_outcome(text, int(args.pr_number), args.head_sha, branch_sha)
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"approved": False, "reason": f"cannot read ledger: {exc}"}))
        return 1

    if outcome != "approved" and not args.resolution_only:
        reason = (
            "no review outcome for this head" if outcome is None else f"last outcome is {outcome}"
        )
        print(json.dumps({"approved": False, "reason": reason}))
        return 2
    if branch_sha is not None and resolution != "resolution_approved":
        reason = (
            f"no resolution review approved commit {branch_sha}"
            if resolution is None
            else f"the resolution review of commit {branch_sha} was rejected"
        )
        print(json.dumps({"approved": False, "reason": reason}))
        return 2
    print(json.dumps({"approved": True, "reason": "approved"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
