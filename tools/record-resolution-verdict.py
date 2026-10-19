#!/usr/bin/env python3
"""Decide whether a Fable review approved a Claude conflict resolution.

pr-ingestion.yml runs this in the trusted record-resolution-verdict job,
after the review-resolution job. Model output alone never approves. The
resolution is approved only when all of these hold:
  - origin: the review job's upload step succeeded, the verdict file's
    SHA-256 equals the hash that job recorded as a job output, and its
    reviewer_id is that job's reviewer ID (the same checks merge-verdicts.py
    makes, so no other job can stand in for the reviewer)
  - the verdict file is one JSON object with verdict "approve",
    novel_lines_justified true and a numeric confidence of at least
    conflict_resolution.review_min_confidence from the config
  - no review_hard_fail_patterns entry matches an added or removed line of
    the final branch diff (BASE_SHA..BRANCH_SHA)
Anything else, including a missing or malformed verdict, is a rejection.

The decision is bound to the exact commit that was reviewed: the ledger event
(resolution_approved or resolution_rejected) carries pr_number and head_sha
(the approved upstream pair) and details.branch_sha. automerge-verify.sh
accepts a Conflict-Resolved-By commit only with a resolution_approved event
for its own SHA, so any new push to the branch needs a new review.

Usage:
  python tools/record-resolution-verdict.py --verdict resolution.json \\
      --reviewer-id ID --verdict-sha256 HEX --upload success \\
      --config .if-fork/config.yaml --final-diff final.diff \\
      --pr-number N --head-sha SHA --branch-sha SHA --branch NAME \\
      --output decision.json --events-out events.jsonl [--novel-lines report.json]

Exit codes: 0 approved, 1 rejected, 3 bad arguments or unreadable policy input.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

EXIT_APPROVED = 0
EXIT_REJECTED = 1
EXIT_ERROR = 3

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VALID_VERDICTS = frozenset(["approve", "reject"])


def die(message: str) -> None:
    print(json.dumps({"error": message}), file=sys.stderr)
    sys.exit(EXIT_ERROR)


def _changed_lines(diff_text: str) -> list[str]:
    """changed_lines() from merge-verdicts.py, so both checks read a diff the same way."""
    path = Path(__file__).parent / "merge-verdicts.py"
    spec = importlib.util.spec_from_file_location("merge_verdicts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lines: list[str] = module.changed_lines(diff_text)
    return lines


def load_verdict(
    path: Path, reviewer_id: str, sha256: str, upload: str
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return the verdict that passed the origin checks, or None and the reasons."""
    if upload != "success":
        return None, [f"the review job's upload step did not succeed ({upload or 'no outcome'})"]
    if not path.is_file() or path.is_symlink():
        return None, ["the review left no verdict file"]
    raw = path.read_bytes()
    if not sha256:
        return None, ["the review job recorded no verdict hash"]
    if hashlib.sha256(raw).hexdigest() != sha256.lower():
        return None, ["the verdict file does not match the hash its review job recorded"]
    try:
        obj: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ["the verdict file is not valid JSON"]
    if not isinstance(obj, dict):
        return None, ["the verdict file is not a JSON object"]
    if obj.get("reviewer_id") != reviewer_id:
        return None, [f"the verdict's reviewer_id is not the review job's ID {reviewer_id!r}"]
    return obj, []


def verdict_problems(verdict: dict[str, Any], threshold: float) -> list[str]:
    """Return why a verdict that passed the origin checks does not approve."""
    problems: list[str] = []
    value = verdict.get("verdict")
    if value not in VALID_VERDICTS:
        problems.append("the verdict is neither approve nor reject")
    elif value != "approve":
        problems.append("the reviewer rejected the resolution")
    if verdict.get("novel_lines_justified") is not True:
        problems.append("the reviewer did not justify every novel line")
    confidence = verdict.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        problems.append("the verdict has no numeric confidence")
    elif not 0 <= confidence <= 1:
        problems.append("the verdict's confidence is outside 0 to 1")
    elif confidence < threshold:
        problems.append(f"confidence {confidence} is below the threshold {threshold}")
    return problems


def hard_fail_hits(diff_text: str, patterns: list[str]) -> list[dict[str, str]]:
    """Return the first changed line of the diff that each pattern matches."""
    lines = _changed_lines(diff_text)
    hits: list[dict[str, str]] = []
    for pattern in patterns:
        regex = re.compile(pattern)
        for line in lines:
            if regex.search(line):
                hits.append({"pattern": pattern, "line": line.strip()[:200]})
                break
    return hits


def decide(
    verdict: dict[str, Any] | None,
    origin_reasons: list[str],
    hits: list[dict[str, str]],
    threshold: float,
) -> list[str]:
    """Return every reason the resolution is not approved; empty means approved."""
    reasons = list(origin_reasons)
    if verdict is not None:
        reasons += verdict_problems(verdict, threshold)
    reasons += [f"hard-fail pattern in the final diff: {hit['pattern']}" for hit in hits]
    return reasons


def build_event(
    args: argparse.Namespace,
    verdict: dict[str, Any] | None,
    reasons: list[str],
    hits: list[dict[str, str]],
    novel: dict[str, Any] | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "branch": args.branch,
        "branch_sha": args.branch_sha,
        "reviewer_id": args.reviewer_id,
        "reasons": reasons,
    }
    if verdict is not None:
        details["verdict"] = verdict.get("verdict")
        details["confidence"] = verdict.get("confidence")
        details["novel_lines_justified"] = verdict.get("novel_lines_justified")
        reasoning = verdict.get("reasoning")
        if isinstance(reasoning, str):
            details["reasoning"] = reasoning[:2000]
    if hits:
        details["hard_fail_hits"] = hits
    if novel is not None:
        details["novel_count"] = novel.get("novel_count")
        details["dropped_count"] = novel.get("dropped_count")
    if args.run_url:
        details["run_url"] = args.run_url
    return {
        "event": "resolution_rejected" if reasons else "resolution_approved",
        "pr_number": args.pr_number,
        "head_sha": args.head_sha,
        "details": details,
    }


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on bad arguments; callers read only 0 and 1 as a decision."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        die(f"bad arguments: {message}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(description=__doc__.splitlines()[0])
    parser.add_argument("--verdict", type=Path, required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--verdict-sha256", default="")
    parser.add_argument("--upload", default="")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--final-diff", type=Path, required=True)
    parser.add_argument("--novel-lines", type=Path)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--branch-sha", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--events-out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not SHA_RE.match(args.head_sha) or not SHA_RE.match(args.branch_sha):
        parser.error("--head-sha and --branch-sha must be 40 lowercase hex characters")
    if not args.reviewer_id:
        parser.error("--reviewer-id must not be empty")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        threshold = float(config["conflict_resolution"]["review_min_confidence"])
        patterns = list(config.get("review_hard_fail_patterns", []))
        diff_text = args.final_diff.read_text(encoding="utf-8", errors="replace")
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        die(f"cannot read policy inputs: {exc}")
        raise  # unreachable
    if not diff_text.strip():
        die("final diff is empty")

    novel: dict[str, Any] | None = None
    if args.novel_lines is not None:
        try:
            loaded = json.loads(args.novel_lines.read_text(encoding="utf-8"))
            novel = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            novel = None

    verdict, origin_reasons = load_verdict(
        args.verdict, args.reviewer_id, args.verdict_sha256, args.upload
    )
    hits = hard_fail_hits(diff_text, patterns)
    reasons = decide(verdict, origin_reasons, hits, threshold)
    event = build_event(args, verdict, reasons, hits, novel)

    decision = {
        "decision": "rejected" if reasons else "approved",
        "pr_number": args.pr_number,
        "head_sha": args.head_sha,
        "branch_sha": args.branch_sha,
        "reasons": reasons,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    args.events_out.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(decision, indent=2))
    return EXIT_REJECTED if reasons else EXIT_APPROVED


if __name__ == "__main__":
    sys.exit(main())
