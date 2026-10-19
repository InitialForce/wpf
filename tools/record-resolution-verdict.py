#!/usr/bin/env python3
"""Decide whether a Fable review approved a Claude conflict resolution.

The record job of resolution-review.yml runs this after the review job, for
a cherry-pick (pr-ingestion.yml) or a nightly sync merge (nightly-rebase.yml).
Model output alone never approves. The resolution is approved only when all
of these hold:
  - origin: the review job's upload step succeeded, the verdict file's
    SHA-256 equals the hash that job recorded as a job output, and its
    reviewer_id is that job's reviewer ID (the same checks merge-verdicts.py
    makes, so no other job can stand in for the reviewer)
  - the verdict file is one JSON object with verdict "approve",
    novel_lines_justified true and a numeric confidence of at least
    conflict_resolution.review_min_confidence from the config
  - no review_hard_fail_patterns entry matches a scanned line of --final-diff.
    For a cherry-pick that is every added and removed line of
    BASE_SHA..BRANCH_SHA (the upstream part was already scanned at review).
    For a sync it is the added lines of the diff from git's automatic merge
    (with conflict markers) to the result, which is what the resolution
    itself wrote; the rest is upstream release/10.0 (--added-only).
  - the novel-lines report from the prepare job (no secrets, no model)
    matches the SHA-256 that job recorded as a job output, and its
    novel_count is at most conflict_resolution.max_novel_lines (default 50).
    Above the cap the resolution is rejected whatever the reviewer says.
Anything else is a rejection.

Review tries: resolution-review.yml runs up to three review tries for the
same branch commit (prepare and review, with a delay before each retry) and
stops at the first try that leaves a verdict. --tries lists them. A try that
ran but left no valid verdict (the job failed, no verdict file, a failed
origin check, or a verdict that is neither approve nor reject) is recorded as
resolution_review_error. That is not a rejection and does not use up an
attempt. The first valid try decides. When no try left a valid verdict, the
resolution is rejected (resolution_rejected, which counts as an attempt).
That is after three tries, or fewer when a try's own check said it left a
verdict but the checks here disagree.

The decision is bound to the exact commit that was reviewed: the ledger event
(resolution_approved or resolution_rejected) carries pr_number and head_sha
and details.branch_sha. For a cherry-pick that pair is the approved upstream
PR and head; for a sync it is pr_number 0 and the upstream release/10.0 SHA,
also in details.upstream_sha. automerge-verify.sh accepts a
Conflict-Resolved-By commit only with a resolution_approved event for its own
SHA, so any new push to the branch needs a new review.

Attempts: with --ledger, the decision also says what the caller does next.
The attempt number is one more than the resolution_rejected events for the
same pair since its last resolution_approved (a rebuild after an approval
starts again at 1). A rejection below --max-attempts (default
conflict_resolution.max_resolution_attempts, else 2) is "retry": the caller
runs the resolver again from scratch. At the limit it is "give_up".

Usage:
  python tools/record-resolution-verdict.py --tries tries.json \\
      --reviewer-id ID --config .if-fork/config.yaml --final-diff final.diff \\
      --pr-number N --head-sha SHA --branch-sha SHA --branch NAME \\
      --output decision.json --events-out events.jsonl

tries.json is a list of {"try": n, "result": job result, "verdict": path,
"verdict_sha256": hex, "upload": outcome, "novel_lines": path,
"novel_sha256": hex}. A try whose result is "skipped" did not run. Instead
of --tries, one try can be given with --verdict, --verdict-sha256, --upload,
--novel-lines and --novel-sha256.

Exit codes: 0 approved, 1 rejected, 3 bad arguments or unreadable policy input.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

EXIT_APPROVED = 0
EXIT_REJECTED = 1
EXIT_ERROR = 3

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VALID_VERDICTS = frozenset(["approve", "reject"])
KINDS = ("cherry-pick", "sync")
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_MAX_NOVEL_LINES = 50


@dataclass(frozen=True)
class Try:
    """One review try: its job result and the files and hashes it left."""

    number: int
    result: str
    verdict: Path
    verdict_sha256: str
    upload: str
    novel_lines: Path
    novel_sha256: str


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


def _added_lines(diff_text: str) -> list[str]:
    return [
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def hard_fail_hits(
    diff_text: str, patterns: list[str], added_only: bool = False
) -> list[dict[str, str]]:
    """Return the first scanned line of the diff that each pattern matches."""
    lines = _added_lines(diff_text) if added_only else _changed_lines(diff_text)
    hits: list[dict[str, str]] = []
    for pattern in patterns:
        regex = re.compile(pattern)
        for line in lines:
            if regex.search(line):
                hits.append({"pattern": pattern, "line": line.strip()[:200]})
                break
    return hits


def load_novel(path: Path, sha256: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Return the novel-lines report if it is the one the prepare job hashed."""
    problem = ["the novel-lines report is missing or does not match the prepare job's hash"]
    if not sha256 or not path.is_file() or path.is_symlink():
        return None, problem
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256.lower():
        return None, problem
    try:
        obj: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ["the novel-lines report is not valid JSON"]
    if not isinstance(obj, dict):
        return None, ["the novel-lines report is not a JSON object"]
    return obj, []


def novel_cap_problems(novel: dict[str, Any], cap: int) -> list[str]:
    """The code cap on novel lines, which no verdict can override."""
    count = novel.get("novel_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return ["the novel-lines report has no valid novel_count"]
    if count > cap:
        return [
            f"{count} novel lines, above the cap of {cap} (conflict_resolution.max_novel_lines)"
        ]
    return []


def prior_rejections(ledger_text: str, pr_number: int, head_sha: str) -> int:
    """resolution_rejected events for the pair since its last resolution_approved."""
    count = 0
    for line in ledger_text.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        if entry.get("pr_number") != pr_number or entry.get("head_sha") != head_sha:
            continue
        if entry.get("event") == "resolution_approved":
            count = 0
        elif entry.get("event") == "resolution_rejected":
            count += 1
    return count


def next_action(rejected: bool, attempt: int, max_attempts: int) -> str:
    if not rejected:
        return "approved"
    return "retry" if attempt < max_attempts else "give_up"


def first_valid_try(
    tries: list[Try], reviewer_id: str
) -> tuple[Try | None, dict[str, Any] | None, list[tuple[Try, list[str]]]]:
    """Return the first try with a valid verdict, and every invalid try before it.

    Valid means the origin checks pass and the verdict is approve or reject.
    Skipped tries did not run and are ignored.
    """
    errors: list[tuple[Try, list[str]]] = []
    for t in tries:
        if t.result == "skipped":
            continue
        verdict, reasons = load_verdict(t.verdict, reviewer_id, t.verdict_sha256, t.upload)
        if verdict is not None and verdict.get("verdict") not in VALID_VERDICTS:
            verdict, reasons = None, ["the verdict is neither approve nor reject"]
        if verdict is None:
            if t.result not in ("success", ""):
                reasons = [f"the review try's jobs did not succeed ({t.result})", *reasons]
            errors.append((t, reasons))
            continue
        return t, verdict, errors
    return None, None, errors


def decide(
    verdict: dict[str, Any] | None,
    errors: list[tuple[Try, list[str]]],
    hits: list[dict[str, str]],
    threshold: float,
) -> list[str]:
    """Return every reason the resolution is not approved; empty means approved."""
    reasons: list[str] = []
    if verdict is None:
        for _, why in errors:
            reasons += why
        reasons.append(f"no valid review verdict in {len(errors)} tries")
    else:
        reasons += verdict_problems(verdict, threshold)
    reasons += [f"hard-fail pattern in the final diff: {hit['pattern']}" for hit in hits]
    return reasons


def build_review_error(args: argparse.Namespace, t: Try, reasons: list[str]) -> dict[str, Any]:
    """The resolution_review_error event for one try that left no valid verdict."""
    details: dict[str, Any] = {
        "kind": args.kind,
        "branch": args.branch,
        "branch_sha": args.branch_sha,
        "reviewer_id": args.reviewer_id,
        "try": t.number,
        "reasons": reasons,
    }
    if args.kind == "sync":
        details["upstream_sha"] = args.head_sha
    if args.run_url:
        details["run_url"] = args.run_url
    return {
        "event": "resolution_review_error",
        "pr_number": args.pr_number,
        "head_sha": args.head_sha,
        "details": details,
    }


def load_tries(args: argparse.Namespace) -> list[Try]:
    """Read --tries, or build the single try from the per-try arguments."""
    if args.tries is None:
        if args.verdict is None or args.novel_lines is None:
            die("give --tries, or --verdict and --novel-lines")
        return [Try(1, "success", args.verdict, args.verdict_sha256, args.upload,
                    args.novel_lines, args.novel_sha256)]
    try:
        raw: Any = json.loads(args.tries.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            raise ValueError("not a non-empty list")
        tries = [
            Try(int(item["try"]), str(item["result"]), Path(str(item["verdict"])),
                str(item["verdict_sha256"]), str(item["upload"]),
                Path(str(item["novel_lines"])), str(item["novel_sha256"]))
            for item in raw
        ]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        die(f"cannot read --tries: {exc}")
        raise  # unreachable
    return tries


def build_event(
    args: argparse.Namespace,
    verdict: dict[str, Any] | None,
    reasons: list[str],
    hits: list[dict[str, str]],
    novel: dict[str, Any] | None,
    attempt: int,
    review_try: int | None,
    review_errors: int,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "kind": args.kind,
        "branch": args.branch,
        "branch_sha": args.branch_sha,
        "reviewer_id": args.reviewer_id,
        "attempt": attempt,
        "review_try": review_try,
        "review_errors": review_errors,
        "reasons": reasons,
    }
    if args.kind == "sync":
        details["upstream_sha"] = args.head_sha
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
        details["reordered_count"] = novel.get("reordered_count")
        details["duplicated_count"] = novel.get("duplicated_count")
        details["max_novel_lines"] = args.max_novel_lines
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
    parser.add_argument("--kind", choices=KINDS, default="cherry-pick")
    parser.add_argument("--tries", type=Path,
                        help="JSON list of review tries (see the module docstring)")
    parser.add_argument("--verdict", type=Path)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--verdict-sha256", default="")
    parser.add_argument("--upload", default="")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--final-diff", type=Path, required=True)
    parser.add_argument("--added-only", action="store_true",
                        help="Scan only added lines of --final-diff (sync)")
    parser.add_argument("--novel-lines", type=Path)
    parser.add_argument("--novel-sha256", default="",
                        help="SHA-256 of --novel-lines, from the prepare job's output")
    parser.add_argument("--max-novel-lines", type=int)
    parser.add_argument("--ledger", type=Path,
                        help="Ledger to count earlier resolution attempts for the pair")
    parser.add_argument("--max-attempts", type=int)
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
    if args.kind == "sync" and args.pr_number != 0:
        parser.error("a sync resolution is recorded with --pr-number 0")
    if args.kind == "cherry-pick" and args.pr_number <= 0:
        parser.error("a cherry-pick needs the upstream PR number")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        threshold = float(config["conflict_resolution"]["review_min_confidence"])
        max_attempts = int(
            args.max_attempts
            or config["conflict_resolution"].get("max_resolution_attempts", DEFAULT_MAX_ATTEMPTS)
        )
        ledger_text = args.ledger.read_text(encoding="utf-8") if args.ledger else ""
        cap = args.max_novel_lines
        if cap is None:
            cap = int(config["conflict_resolution"].get("max_novel_lines", DEFAULT_MAX_NOVEL_LINES))
        args.max_novel_lines = cap
        prior = prior_rejections(ledger_text, args.pr_number, args.head_sha)
        patterns = list(config.get("review_hard_fail_patterns", []))
        diff_text = args.final_diff.read_text(encoding="utf-8", errors="replace")
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        die(f"cannot read policy inputs: {exc}")
        raise  # unreachable
    if not diff_text.strip():
        die("final diff is empty")

    tries = load_tries(args)
    chosen, verdict, errors = first_valid_try(tries, args.reviewer_id)
    novel: dict[str, Any] | None = None
    novel_reasons: list[str] = []
    if chosen is not None:
        # The novel-lines report of the same try's prepare job.
        novel, novel_reasons = load_novel(chosen.novel_lines, chosen.novel_sha256)
        if novel is not None:
            novel_reasons = novel_cap_problems(novel, cap)

    hits = hard_fail_hits(diff_text, patterns, added_only=args.added_only)
    reasons = decide(verdict, errors, hits, threshold) + novel_reasons
    attempt = prior + 1
    events = [build_review_error(args, t, why) for t, why in errors]
    events.append(build_event(
        args, verdict, reasons, hits, novel, attempt,
        chosen.number if chosen else None, len(errors),
    ))

    decision = {
        "decision": "rejected" if reasons else "approved",
        "next_action": next_action(bool(reasons), attempt, max_attempts),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "kind": args.kind,
        "pr_number": args.pr_number,
        "head_sha": args.head_sha,
        "branch_sha": args.branch_sha,
        "review_try": chosen.number if chosen else None,
        "review_errors": len(errors),
        "reasons": reasons,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    args.events_out.write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8"
    )
    print(json.dumps(decision, indent=2))
    return EXIT_REJECTED if reasons else EXIT_APPROVED


if __name__ == "__main__":
    sys.exit(main())
