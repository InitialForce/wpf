#!/usr/bin/env python3
"""Find new upstream dotnet/wpf PR candidates and pick the ones to review today.

Used by pr-discovery.yml. Deterministic: no model call.

Reads the upstream PR lists (open, and merged within the lookback window) with
``gh``, applies the ``discovery`` block of config.yaml, compares the result with
the patch ledger and writes two files:

``--events-out``  JSONL, one ``discovered`` event per (pr_number, head_sha)
                  that the ledger does not have yet. Feed it to
                  ``ledger-event.py --events-file`` so the whole run is one commit.
``--queue-out``   JSON list of ``{"pr_number", "head_sha"}`` to dispatch to
                  pr-review.yml, at most ``discovery.max_reviews_per_run`` long.

Rules:
- A PR is skipped for good once the ledger shows it applied (``cherry_picked``,
  ``published``, or seeded with ``if_already_applied_in_local_main``),
  ``graduated_upstream``, ``rejected``, or a ``merged_verdict`` of ``rejected``.
- A (pr_number, head_sha) pair is recorded once. A new head_sha on a known PR
  is a new candidate.
- A pair is queued for review until the ledger has a ``merged_verdict`` or an
  ``escalated`` event for that exact head_sha.
- Upstream-merged PRs seeded at bootstrap stay in the queue even when they are
  older than the lookback window, so the bootstrap backlog drains.

Queue order: merged upstream first, then allowlisted authors, then tier
S, A, B, SUSPECT, then oldest PR number first.

Exit codes: 0 success, 2 bad input or ``gh`` failure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = ".if-fork/config.yaml"
DEFAULT_LEDGER_PATH = ".if-fork/patch-ledger.jsonl"

GH_FIELDS = (
    "number,title,author,url,headRefOid,changedFiles,additions,deletions,"
    "labels,isDraft,baseRefName,state,mergedAt"
)

# Ledger events that end a PR's journey through the pipeline.
TERMINAL_EVENTS = frozenset(["cherry_picked", "published", "graduated_upstream", "rejected"])
# Ledger events that close one (pr_number, head_sha) review.
REVIEWED_EVENTS = frozenset(["merged_verdict", "escalated"])

TIER_ORDER = {"S": 0, "A": 1, "B": 2, "SUSPECT": 3}

PR = dict[str, Any]


def die(code: int, message: str, **extra: object) -> None:
    print(json.dumps({"error": message, **extra}), file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if not isinstance(config, dict) or not isinstance(config.get("discovery"), dict):
        die(2, "config has no 'discovery' block", path=str(path))
    return config


def gh_pr_list(repo: str, state: str, base: str, limit: int) -> list[PR]:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--base",
            base,
            "--limit",
            str(limit),
            "--json",
            GH_FIELDS,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(2, f"gh pr list --state {state} --base {base} failed", stderr=result.stderr)
    prs = json.loads(result.stdout)
    if not isinstance(prs, list):
        die(2, "gh pr list did not return a JSON list")
    return prs


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            entries.append(json.loads(raw))
    return entries


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _login(pr: PR) -> str:
    author = pr.get("author") or {}
    return str(author.get("login") or "")


def _labels(pr: PR) -> list[str]:
    return [str(label.get("name")) for label in pr.get("labels") or []]


def is_eligible(pr: PR, config: dict[str, Any]) -> bool:
    """Return True when *pr* is a community PR the fork may want."""
    discovery = config["discovery"]
    login = _login(pr)
    if pr.get("isDraft"):
        return False
    if pr.get("baseRefName") not in discovery.get("base_branches", ["main"]):
        return False
    if login.startswith("app/") or login.endswith("[bot]"):
        return False
    if login in discovery.get("exclude_authors", []):
        return False
    if login in config.get("author_allowlist", []):
        return True
    wanted = set(discovery.get("include_labels", []))
    return bool(wanted & set(_labels(pr)))


def compute_tier(pr: PR, config: dict[str, Any]) -> str:
    """Preliminary tier from config.tier_predicates; SUSPECT on a hard-fail title."""
    title = str(pr.get("title") or "")
    for pattern in config.get("review_hard_fail_patterns", []):
        if re.search(pattern, title):
            return "SUSPECT"
    files = int(pr.get("changedFiles") or 0)
    loc = int(pr.get("additions") or 0) + int(pr.get("deletions") or 0)
    tiers = config.get("tier_predicates", {})
    for name in ("s", "a"):
        predicate = tiers.get(name, {})
        if files <= predicate.get("max_files_touched", -1) and loc <= predicate.get(
            "max_loc_delta", -1
        ):
            return name.upper()
    return "B"


# ---------------------------------------------------------------------------
# Ledger view
# ---------------------------------------------------------------------------


class LedgerView:
    """What the ledger already knows, keyed by PR number and head SHA."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.discovered: set[tuple[int, str]] = set()
        self.reviewed: set[tuple[int, str]] = set()
        self.closed_prs: set[int] = set()
        # Seeded upstream-merged PRs: pr_number -> (head_sha, details)
        self.seeded_merged: dict[int, tuple[str, dict[str, Any]]] = {}

        for entry in entries:
            event = entry.get("event")
            number = entry.get("pr_number")
            sha = entry.get("head_sha") or ""
            details = entry.get("details") or {}
            if not isinstance(number, int) or number <= 0:
                continue
            key = (number, sha)
            if event == "discovered":
                self.discovered.add(key)
                if details.get("if_already_applied_in_local_main"):
                    self.closed_prs.add(number)
                elif entry.get("actor") == "seed-bulk-import" and details.get("state") == "MERGED":
                    self.seeded_merged[number] = (sha, details)
            elif event in TERMINAL_EVENTS:
                self.closed_prs.add(number)
            elif event in REVIEWED_EVENTS:
                self.reviewed.add(key)
                if event == "merged_verdict" and details.get("verdict") == "rejected":
                    self.closed_prs.add(number)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def discover(
    open_prs: list[PR],
    merged_prs: list[PR],
    entries: list[dict[str, Any]],
    config: dict[str, Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Return (new discovered event specs, review queue, counts)."""
    discovery = config["discovery"]
    allowlist = set(config.get("author_allowlist", []))
    cutoff = now - timedelta(days=int(discovery.get("merged_lookback_days", 90)))
    ledger = LedgerView(entries)

    upstream: dict[int, PR] = {}
    for pr in open_prs:
        upstream[int(pr["number"])] = pr
    for pr in merged_prs:
        merged_at = pr.get("mergedAt")
        if not merged_at:
            continue
        if datetime.fromisoformat(str(merged_at).replace("Z", "+00:00")) < cutoff:
            continue
        upstream[int(pr["number"])] = pr

    events: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for number in sorted(upstream):
        pr = upstream[number]
        if not is_eligible(pr, config) or number in ledger.closed_prs:
            continue
        sha = str(pr["headRefOid"])
        tier = compute_tier(pr, config)
        merged = bool(pr.get("mergedAt"))
        if (number, sha) not in ledger.discovered:
            events.append(
                {
                    "event": "discovered",
                    "pr_number": number,
                    "head_sha": sha,
                    "details": {
                        "author": _login(pr),
                        "title": pr.get("title"),
                        "url": pr.get("url"),
                        "state": "MERGED" if merged else "OPEN",
                        "merged_at": pr.get("mergedAt"),
                        "changed_files": pr.get("changedFiles"),
                        "additions": pr.get("additions"),
                        "deletions": pr.get("deletions"),
                        "labels": _labels(pr),
                        "if_tier": tier,
                    },
                }
            )
        if (number, sha) not in ledger.reviewed:
            candidates.append(
                {
                    "pr_number": number,
                    "head_sha": sha,
                    "tier": tier,
                    "merged": merged,
                    "allowlisted": _login(pr) in allowlist,
                }
            )

    queued_numbers = {c["pr_number"] for c in candidates}
    for number, (sha, details) in ledger.seeded_merged.items():
        if number in ledger.closed_prs or number in queued_numbers:
            continue
        if (number, sha) in ledger.reviewed:
            continue
        candidates.append(
            {
                "pr_number": number,
                "head_sha": sha,
                "tier": str(details.get("if_tier") or "B").upper(),
                "merged": True,
                "allowlisted": True,
            }
        )

    candidates.sort(
        key=lambda c: (
            not c["merged"],
            not c["allowlisted"],
            TIER_ORDER.get(c["tier"], 9),
            c["pr_number"],
        )
    )
    cap = int(discovery.get("max_reviews_per_run", 10))
    queue = [{"pr_number": c["pr_number"], "head_sha": c["head_sha"]} for c in candidates[:cap]]
    counts = {
        "upstream_considered": len(upstream),
        "new_discovered": len(events),
        "pending_review": len(candidates),
        "queued_for_review": len(queue),
    }
    return events, queue, counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--ledger-path", default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--events-out", required=True, help="JSONL of new discovered events")
    parser.add_argument("--queue-out", required=True, help="JSON list of PRs to review")
    parser.add_argument("--open-json", help="Use this gh JSON for open PRs instead of calling gh")
    parser.add_argument(
        "--merged-json", help="Use this gh JSON for merged PRs instead of calling gh"
    )
    parser.add_argument("--now", help="ISO timestamp to use as 'now' (tests)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    discovery = config["discovery"]
    repo = config["upstream"]["repo"]

    if args.open_json:
        open_prs = json.loads(Path(args.open_json).read_text(encoding="utf-8"))
    else:
        open_prs = []
        for base in discovery.get("base_branches", ["main"]):
            open_prs += gh_pr_list(repo, "open", base, int(discovery.get("open_query_limit", 1000)))
    if args.merged_json:
        merged_prs = json.loads(Path(args.merged_json).read_text(encoding="utf-8"))
    else:
        merged_prs = []
        for base in discovery.get("base_branches", ["main"]):
            merged_prs += gh_pr_list(
                repo, "merged", base, int(discovery.get("merged_query_limit", 500))
            )

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(UTC)
    events, queue, counts = discover(
        open_prs, merged_prs, read_ledger(Path(args.ledger_path)), config, now
    )

    Path(args.events_out).write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8"
    )
    Path(args.queue_out).write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
