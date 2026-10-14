#!/usr/bin/env python3
"""Find new upstream dotnet/wpf PR candidates and pick the ones to review today.

Used by pr-discovery.yml. Deterministic: no model call.

Reads the upstream PR lists (open, and merged within the lookback window) with
``gh``, applies the ``discovery`` block of config.yaml, compares the result with
the patch ledger and writes two files:

``--events-out``  JSONL of new ledger events: one ``discovered`` event per (pr_number, head_sha)
                  that the ledger does not have yet. Feed it to
                  ``ledger-event.py --events-file`` so the whole run is one commit.
``--queue-out``   JSON list of ``{"pr_number", "head_sha"}`` to dispatch to
                  pr-review.yml, at most ``discovery.max_reviews_per_run`` long.
``--ingest-out``  JSON list of ``{"pr_number", "head_sha"}`` of approved SHAs to
                  send to pr-ingestion.yml again (no model call in this tool).

Rules:
- A PR is skipped for good once the ledger shows it applied (``cherry_picked``,
  ``published``, or seeded with ``if_already_applied_in_local_main``),
  ``graduated_upstream``, ``rejected``, or a ``merged_verdict`` of ``rejected``.
- A (pr_number, head_sha) pair is recorded once. A new head_sha on a known PR
  is a new candidate.
- A pair is queued for review until the ledger has a ``merged_verdict`` or an
  ``escalated`` event for that exact head_sha. ``review_error`` does not count;
  merge-verdicts.py escalates after 3 of them.
- Upstream-merged PRs seeded at bootstrap stay in the queue even when they are
  older than the lookback window, so the bootstrap backlog drains. The seed
  stored the merge commit SHA, not the PR head, so before queueing one the tool
  looks up the real head with ``gh api`` and records it as a new
  ``discovered`` event.
- Lost handoff: an approved pair with no ingestion outcome event after the
  approval is sent to pr-ingestion again, with an ``ingestion_requested``
  event per attempt. Not within 6 hours of the last attempt; after 3 attempts
  the pair is recorded as ``escalated`` (reason ``ingestion_retries_exhausted``).

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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

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
# Ledger events that end an ingestion attempt for one (pr_number, head_sha).
INGESTION_OUTCOME_EVENTS = frozenset(
    [
        "cherry_picked",
        "graduated_upstream",
        "escalated",
        "rejected",
        "pre_flight_failed",
        "published",
    ]
)
MAX_INGESTION_ATTEMPTS = 3
INGESTION_RETRY_AFTER = timedelta(hours=6)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

TIER_ORDER = {"S": 0, "A": 1, "B": 2, "SUSPECT": 3}

PR = dict[str, Any]


def die(code: int, message: str, **extra: object) -> NoReturn:
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


def _parse_ts(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class LedgerView:
    """What the ledger already knows, keyed by PR number and head SHA."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.discovered: set[tuple[int, str]] = set()
        self.reviewed: set[tuple[int, str]] = set()
        self.closed_prs: set[int] = set()
        # Seeded upstream-merged PRs: pr_number -> (seed head_sha, details)
        self.seeded_merged: dict[int, tuple[str, dict[str, Any]]] = {}
        # Latest head_sha recorded by a non-seed discovered event, per PR
        self.latest_sha: dict[int, str] = {}
        # Handoff tracking per (pr_number, head_sha), by ledger position
        self.approved_at: dict[tuple[int, str], int] = {}
        self.outcome_at: dict[tuple[int, str], int] = {}
        self.ingestion_requests: dict[tuple[int, str], list[tuple[int, datetime | None]]] = {}

        for index, entry in enumerate(entries):
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
                else:
                    self.latest_sha[number] = sha
            if event in TERMINAL_EVENTS:
                self.closed_prs.add(number)
            if event in REVIEWED_EVENTS:
                self.reviewed.add(key)
                if event == "merged_verdict" and details.get("verdict") == "rejected":
                    self.closed_prs.add(number)
            if event == "approved" or (
                event == "merged_verdict" and details.get("verdict") == "approved"
            ):
                self.approved_at[key] = index
            if event in INGESTION_OUTCOME_EVENTS:
                self.outcome_at[key] = index
            if event == "ingestion_requested":
                self.ingestion_requests.setdefault(key, []).append(
                    (index, _parse_ts(entry.get("ts")))
                )


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def discover(
    open_prs: list[PR],
    merged_prs: list[PR],
    entries: list[dict[str, Any]],
    config: dict[str, Any],
    now: datetime,
    resolve_head: Callable[[int], str | None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Return (new ledger event specs, review queue, ingestion queue, counts).

    *resolve_head* returns the real head SHA of an upstream PR; it is called
    only for seeded merged PRs that are about to be queued.
    """
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
        if not SHA_RE.match(sha):
            continue
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
    for number, (seed_sha, details) in ledger.seeded_merged.items():
        if number in ledger.closed_prs or number in queued_numbers:
            continue
        # A later discovered event holds the real head; otherwise look it up
        # when the PR reaches the front of the queue.
        sha = ledger.latest_sha.get(number, "")
        if sha and (number, sha) in ledger.reviewed:
            continue
        candidates.append(
            {
                "pr_number": number,
                "head_sha": sha,
                "seed_sha": seed_sha,
                "seed_details": details,
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
    queue: list[dict[str, Any]] = []
    for c in candidates:
        if len(queue) >= cap:
            break
        number, sha = c["pr_number"], c["head_sha"]
        if not sha:
            real = resolve_head(number) if resolve_head else None
            if not real or not SHA_RE.match(real):
                continue
            sha = real
            if (number, sha) not in ledger.discovered:
                events.append(
                    {
                        "event": "discovered",
                        "pr_number": number,
                        "head_sha": sha,
                        "details": {
                            **{
                                k: v
                                for k, v in c["seed_details"].items()
                                if k
                                in (
                                    "title",
                                    "url",
                                    "state",
                                    "merged_at",
                                    "labels",
                                    "if_tier",
                                    "additions",
                                    "deletions",
                                    "changed_files",
                                )
                            },
                            "seed_head_sha": c["seed_sha"],
                            "note": "real PR head; the bootstrap seed stored the merge commit",
                        },
                    }
                )
            if (number, sha) in ledger.reviewed:
                continue
        queue.append({"pr_number": number, "head_sha": sha})

    ingest = _ingestion_retries(ledger, now, events)
    counts = {
        "upstream_considered": len(upstream),
        "new_discovered": sum(1 for e in events if e["event"] == "discovered"),
        "pending_review": len(candidates),
        "queued_for_review": len(queue),
        "ingestion_resent": len(ingest),
        "ingestion_given_up": sum(1 for e in events if e["event"] == "escalated"),
    }
    return events, queue, ingest, counts


def _ingestion_retries(
    ledger: LedgerView, now: datetime, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Find approved SHAs whose handoff to pr-ingestion was lost.

    Appends ``ingestion_requested`` (or, after MAX_INGESTION_ATTEMPTS,
    ``escalated``) events to *events* and returns the pairs to send again.
    """
    ingest: list[dict[str, Any]] = []
    for (number, sha), approved_index in sorted(ledger.approved_at.items()):
        if number in ledger.closed_prs:
            continue
        if ledger.outcome_at.get((number, sha), -1) > approved_index:
            continue
        requests = [
            ts
            for index, ts in ledger.ingestion_requests.get((number, sha), [])
            if index > approved_index
        ]
        if len(requests) >= MAX_INGESTION_ATTEMPTS:
            events.append(
                {
                    "event": "escalated",
                    "pr_number": number,
                    "head_sha": sha,
                    "details": {
                        "reason": "ingestion_retries_exhausted",
                        "attempts": len(requests),
                    },
                }
            )
            continue
        last = max((ts for ts in requests if ts is not None), default=None)
        if last is not None and now - last < INGESTION_RETRY_AFTER:
            continue
        events.append(
            {
                "event": "ingestion_requested",
                "pr_number": number,
                "head_sha": sha,
                "details": {"attempt": len(requests) + 1, "source": "pr-discovery"},
            }
        )
        ingest.append({"pr_number": number, "head_sha": sha})
    return ingest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--ledger-path", default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--events-out", required=True, help="JSONL of new discovered events")
    parser.add_argument("--queue-out", required=True, help="JSON list of PRs to review")
    parser.add_argument(
        "--ingest-out", required=True, help="JSON list of approved PRs to send to ingestion"
    )
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

    def resolve_head(number: int) -> str | None:
        if args.open_json:  # offline / test mode: no network lookups
            return None
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{number}", "--jq", ".head.sha"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    events, queue, ingest, counts = discover(
        open_prs, merged_prs, read_ledger(Path(args.ledger_path)), config, now, resolve_head
    )

    Path(args.events_out).write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8"
    )
    Path(args.queue_out).write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
    Path(args.ingest_out).write_text(json.dumps(ingest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
