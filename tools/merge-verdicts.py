#!/usr/bin/env python3
"""Combine two independent reviewer verdicts into a final merge verdict.

Decision matrix (PLAN.md §3.9):
  both safe              -> approved   (exit 0)
  both unsafe            -> rejected   (exit 1)
  any escalate-to-human,
  or safe vs unsafe mix  -> escalated  (exit 2)
  bad arguments or input -> error      (exit 3)
  a reviewer left no valid verdict
                         -> review_error (exit 4)

Deterministic policy, applied on top of the model verdicts. Model output alone
never produces "approved":
  - A "safe" verdict counts only when its confidence is at least the tier's
    min_review_confidence (tier from the pinned diff size, config
    tier_predicates). Below that, or with no confidence, it counts as
    escalate-to-human.
  - The pinned diff (--diff) is scanned for config review_hard_fail_patterns
    on added and removed lines, and the file list (--files) is matched against
    config file_denylist. Any hit turns "approved" into "escalated".

Origin checks (pr-review.yml passes --review-N-id, --review-N-sha256 and
--review-N-upload from each review job's outputs). A verdict counts only when
its review job's upload step succeeded, the file's SHA-256 equals the hash that
job recorded, and its reviewer_id is the job's reviewer ID. Otherwise the
verdict is treated as missing and the problem is listed in "origin_errors",
so one reviewer cannot stand in for the other.

A reviewer that crashed or ran out of turns is not a human escalation. The run
ends as "review_error", which pr-discovery retries. After --max-review-errors
review_error events for the same (pr_number, head_sha) in --ledger-path, the
run escalates instead.

--prepare-failed records the same kind of attempt for a run whose prepare job
failed (no reviewer ran): a review_error with cause "prepare_failed", or on
the third attempt an escalation. It needs no verdicts, config or diff. Without
it a prepare that always fails (unusable base SHA, empty diff, a file name
with a newline) would be queued again by pr-discovery every day.

Output JSON written to --output path (and to stdout for caller inspection):
  {
    "verdict": "approved" | "escalated" | "rejected" | "review_error",
    "pr_number": N,
    "head_sha": "...",
    "review_1": {...},
    "review_2": {...},
    "policy": {...},
    "reasoning": "..."
  }

On escalation, the output also contains "escalation_action":
  "open_review_disagreement_issue"
so the caller (CI workflow) knows to open or update the GitHub issue.

--events-out writes the ledger events for the run as JSONL for ledger-event.py
--events-file, so the run adds one ledger commit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_REVIEWER_VERDICTS: frozenset[str] = frozenset(["safe", "unsafe", "escalate-to-human"])

EXIT_APPROVED = 0
EXIT_REJECTED = 1
EXIT_ESCALATED = 2
EXIT_ERROR = 3
EXIT_REVIEW_ERROR = 4

DEFAULT_MAX_REVIEW_ERRORS = 3

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ReviewerVerdict = dict[str, Any]
OutputVerdict = dict[str, Any]

# ---------------------------------------------------------------------------
# Error helper
# ---------------------------------------------------------------------------


def die(code: int, message: str, **extra: Any) -> None:
    """Write structured JSON error to stderr and exit with *code*."""
    payload: dict[str, Any] = {"error": message, **extra}
    print(json.dumps(payload), file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ReviewLoadError(Exception):
    """A reviewer verdict file is missing or invalid."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


def load_reviewer_json(path: Path, label: str) -> ReviewerVerdict:
    """Read and validate a reviewer-verdict JSON file.

    Raises ReviewLoadError when the file is missing or not a valid verdict.
    """
    if not path.exists():
        raise ReviewLoadError(f"{label} file not found", path=str(path))

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewLoadError(f"Cannot read {label}", path=str(path), detail=str(exc)) from exc

    try:
        obj: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewLoadError(
            f"{label} is not valid JSON", path=str(path), detail=str(exc)
        ) from exc

    if not isinstance(obj, dict):
        raise ReviewLoadError(
            f"{label} must be a JSON object",
            path=str(path),
            actual_type=type(obj).__name__,
        )

    verdict = obj.get("verdict")
    if not isinstance(verdict, str):
        raise ReviewLoadError(f"{label} missing required 'verdict' field", path=str(path))
    if verdict not in VALID_REVIEWER_VERDICTS:
        raise ReviewLoadError(
            f"{label} has unrecognised 'verdict' value",
            path=str(path),
            value=verdict,
            valid=sorted(VALID_REVIEWER_VERDICTS),
        )

    # "reasoning", "concerns" and "categories" are optional; "rationale" is
    # accepted as an alias for "reasoning" (the review prompts use it).
    return dict(obj)


class Origin(NamedTuple):
    """What a review job recorded about its own verdict. None means not checked."""

    reviewer_id: str | None = None
    sha256: str | None = None
    upload: str | None = None


def origin_problem(path: Path, rv: ReviewerVerdict, label: str, origin: Origin) -> str | None:
    """Return why a loaded verdict did not come from its own review job, or None."""
    if origin.sha256 is not None:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if not origin.sha256:
            return f"{label} job recorded no verdict hash, but a verdict file was downloaded"
        if actual != origin.sha256.lower():
            return f"{label} file does not match the hash its review job recorded"
    if origin.reviewer_id is not None and rv.get("reviewer_id") != origin.reviewer_id:
        return (
            f"{label} reviewer_id {str(rv.get('reviewer_id'))[:100]!r} "
            f"is not its job's reviewer ID {origin.reviewer_id!r}"
        )
    return None


def count_review_errors(ledger_path: Path | None, pr_number: int, head_sha: str) -> int:
    """Return how many review_error events the ledger has for (pr_number, head_sha)."""
    if ledger_path is None or not ledger_path.exists():
        return 0
    count = 0
    for raw in ledger_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        entry = json.loads(raw)
        if (
            entry.get("event") == "review_error"
            and entry.get("pr_number") == pr_number
            and entry.get("head_sha") == head_sha
        ):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Deterministic policy
# ---------------------------------------------------------------------------


def _load_denylist_matcher() -> Any:
    """Load check_files() from tools/check-denylist.py (same glob rules as ingestion)."""
    path = Path(__file__).parent / "check-denylist.py"
    spec = importlib.util.spec_from_file_location("check_denylist", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_files


def changed_lines(diff_text: str) -> list[str]:
    """Return added and removed lines of a unified diff, without the +/- marker."""
    lines = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            lines.append(line[1:])
    return lines


def compute_tier(files: list[str], diff_text: str, config: dict[str, Any]) -> str:
    """Tier S/A/B from file count and changed-line count (config.tier_predicates)."""
    loc = len(changed_lines(diff_text))
    tiers = config.get("tier_predicates", {})
    for name in ("s", "a"):
        predicate = tiers.get(name, {})
        if len(files) <= predicate.get("max_files_touched", -1) and loc <= predicate.get(
            "max_loc_delta", -1
        ):
            return name
    return "b"


def evaluate_policy(diff_text: str, files: list[str], config: dict[str, Any]) -> dict[str, Any]:
    """Run the deterministic checks on the pinned diff and file list."""
    hard_fail_hits: list[dict[str, str]] = []
    lines = changed_lines(diff_text)
    for pattern in config.get("review_hard_fail_patterns", []):
        regex = re.compile(pattern)
        for line in lines:
            if regex.search(line):
                hard_fail_hits.append({"pattern": pattern, "line": line.strip()[:200]})
                break
    denylist_hits = _load_denylist_matcher()(files, list(config.get("file_denylist", [])))
    tier = compute_tier(files, diff_text, config)
    threshold = float(
        config.get("tier_predicates", {}).get(tier, {}).get("min_review_confidence", 1.0)
    )
    return {
        "tier": tier.upper(),
        "min_review_confidence": threshold,
        "files_touched": len(files),
        "changed_lines": len(lines),
        "hard_fail_hits": hard_fail_hits,
        "denylist_hits": denylist_hits,
    }


def gate_confidence(rv: ReviewerVerdict, threshold: float) -> ReviewerVerdict:
    """A "safe" verdict below *threshold* (or without a confidence) counts as escalate."""
    if rv.get("verdict") != "safe":
        return rv
    confidence = rv.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        if confidence >= threshold:
            return rv
    return {
        **rv,
        "verdict": "escalate-to-human",
        "model_verdict": "safe",
        "reasoning": (
            f"safe with confidence {confidence!r}, below the tier threshold {threshold}. "
            + _normalise_reasoning(rv)
        ),
    }


def apply_policy(
    output: OutputVerdict, exit_code: int, policy: dict[str, Any]
) -> tuple[OutputVerdict, int]:
    """Attach *policy* and turn "approved" into "escalated" on any deterministic hit."""
    output = {**output, "policy": policy}
    if output["verdict"] != "approved":
        return output, exit_code
    reasons = []
    if policy["hard_fail_hits"]:
        patterns = ", ".join(hit["pattern"] for hit in policy["hard_fail_hits"])
        reasons.append(f"hard-fail patterns in the pinned diff: {patterns}")
    if policy["denylist_hits"]:
        files = ", ".join(hit["file"] for hit in policy["denylist_hits"])
        reasons.append(f"denylisted files: {files}")
    if not reasons:
        return output, exit_code
    output.update({
        "verdict": "escalated",
        "escalation_action": "open_review_disagreement_issue",
        "reasoning": (
            "Both reviewers said safe, but deterministic checks found "
            + "; ".join(reasons) + ". " + output["reasoning"]
        ),
    })
    return output, EXIT_ESCALATED


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------


def _normalise_reasoning(rv: ReviewerVerdict) -> str:
    """Return best available reasoning/rationale string from a reviewer dict."""
    for key in ("reasoning", "rationale"):
        val = rv.get(key)
        if isinstance(val, str) and val:
            return val
    return "(no reasoning provided)"


def compute_verdict(
    r1: ReviewerVerdict,
    r2: ReviewerVerdict | None,
    pr_number: int,
    head_sha: str,
) -> tuple[OutputVerdict, int]:
    """Apply the decision matrix and return (output_dict, exit_code).

    *r2* is None only on the single-review fast path (IF_REVIEW_DOUBLE_REQUIRED
    = false, human-approved); review-1 then counts for both.
    """
    if r2 is None:
        r2 = {**r1, "reasoning": "(single-review fast path: review-2 skipped)"}
    v1: str = r1["verdict"]
    v2: str = r2["verdict"]

    reasoning_r1 = _normalise_reasoning(r1)
    reasoning_r2 = _normalise_reasoning(r2)

    base: OutputVerdict = {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "review_1": r1,
        "review_2": r2,
    }

    # --- Both safe ---
    if v1 == "safe" and v2 == "safe":
        result: OutputVerdict = {
            **base,
            "verdict": "approved",
            "reasoning": (
                f"Both reviewers independently assessed the change as safe. "
                f"Review-1: {reasoning_r1} "
                f"Review-2: {reasoning_r2}"
            ),
        }
        return result, EXIT_APPROVED

    # --- Both unsafe ---
    if v1 == "unsafe" and v2 == "unsafe":
        result = {
            **base,
            "verdict": "rejected",
            "reasoning": (
                f"Both reviewers independently assessed the change as unsafe. "
                f"Review-1: {reasoning_r1} "
                f"Review-2: {reasoning_r2}"
            ),
        }
        return result, EXIT_REJECTED

    # --- Any escalate-to-human OR safe/unsafe mismatch -> escalated ---
    if v1 == "escalate-to-human" or v2 == "escalate-to-human":
        escalation_reason = "At least one reviewer escalated to human review."
    else:
        # One safe, one unsafe
        escalation_reason = (
            f"Reviewers disagreed: review-1 verdict={v1!r}, review-2 verdict={v2!r}."
        )

    result = {
        **base,
        "verdict": "escalated",
        "escalation_action": "open_review_disagreement_issue",
        "reasoning": (
            f"{escalation_reason} "
            f"Review-1 ({v1}): {reasoning_r1} "
            f"Review-2 ({v2}): {reasoning_r2}"
        ),
    }
    return result, EXIT_ESCALATED


# ---------------------------------------------------------------------------
# Ledger events
# ---------------------------------------------------------------------------

# merged verdict -> state event that regenerate-state.py understands
_OUTCOME_EVENTS = {"approved": "approved", "rejected": "rejected", "escalated": "escalated"}


def _review_details(rv: ReviewerVerdict, run_url: str) -> dict[str, Any]:
    details: dict[str, Any] = {
        "verdict": rv.get("verdict"),
        "model_verdict": rv.get("model_verdict", rv.get("verdict")),
        "confidence": rv.get("confidence"),
        "reasoning": _normalise_reasoning(rv)[:2000],
        "reviewer_id": rv.get("reviewer_id"),
    }
    if rv.get("hard_fail_pattern"):
        details["hard_fail_pattern"] = rv["hard_fail_pattern"]
    if rv.get("review_error"):
        details["review_error"] = rv["review_error"]
    if run_url:
        details["run_url"] = run_url
    return details


def build_ledger_events(
    output: OutputVerdict, single_review: bool, run_url: str
) -> list[dict[str, Any]]:
    """Return the ledger events for one review run, in order.

    Normal run: review_1, review_2 (unless single review), merged_verdict, the
    outcome event (approved / rejected / escalated) and, when approved, the
    first ingestion_requested. pr-discovery treats merged_verdict as "this
    head_sha is reviewed"; pr-ingestion reads the review_N head_sha.

    review_error run: one review_error event, which pr-discovery does not
    count as reviewed.
    """
    pr_number = output["pr_number"]
    head_sha = output["head_sha"]

    def event(name: str, details: dict[str, Any]) -> dict[str, Any]:
        if run_url:
            details = {**details, "run_url": run_url}
        return {"event": name, "pr_number": pr_number, "head_sha": head_sha, "details": details}

    verdict = output["verdict"]
    if verdict == "review_error":
        details: dict[str, Any] = {"attempt": output["attempt"], "errors": output["errors"]}
        for key in ("cause", "origin_errors"):
            if output.get(key):
                details[key] = output[key]
        return [event("review_error", details)]

    events: list[dict[str, Any]] = []
    # A run that escalates after failed prepares has no reviews to record.
    if "review_1" in output:
        events.append(event("review_1", _review_details(output["review_1"], "")))
        if not single_review:
            events.append(event("review_2", _review_details(output["review_2"], "")))
    summary: dict[str, Any] = {"verdict": verdict, "reasoning": output["reasoning"][:2000]}
    if output.get("cause"):
        summary["cause"] = output["cause"]
    if "policy" in output:
        summary["policy"] = output["policy"]
    if output.get("origin_errors"):
        summary["origin_errors"] = output["origin_errors"]
    events.append(event("merged_verdict", summary))
    events.append(event(_OUTCOME_EVENTS[verdict], {"source": "merged_verdict"}))
    if verdict == "approved":
        events.append(event("ingestion_requested", {"attempt": 1, "source": "pr-review"}))
    return events


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on bad arguments, which callers read as "escalated"."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        die(EXIT_ERROR, f"bad arguments: {message}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _Parser(
        description="Combine two independent reviewer verdicts into a final merge verdict."
    )
    parser.add_argument(
        "--review-1",
        metavar="PATH",
        help="Path to reviewer-1 verdict JSON file (required unless --prepare-failed)",
    )
    parser.add_argument(
        "--review-2",
        metavar="PATH",
        help="Path to reviewer-2 verdict JSON file (omit only with --single-review)",
    )
    for n in (1, 2):
        parser.add_argument(
            f"--review-{n}-id",
            metavar="ID",
            help=f"Reviewer ID of the review-{n} job; the verdict's reviewer_id must match",
        )
        parser.add_argument(
            f"--review-{n}-sha256",
            metavar="HEX",
            help=f"SHA-256 the review-{n} job recorded for its verdict file (may be empty)",
        )
        parser.add_argument(
            f"--review-{n}-upload",
            metavar="OUTCOME",
            help=f"Outcome of the review-{n} job's upload step; anything but success fails",
        )
    parser.add_argument(
        "--single-review",
        action="store_true",
        help="Single-review fast path: decide on review-1 alone",
    )
    parser.add_argument(
        "--prepare-failed",
        action="store_true",
        help=(
            "The prepare job failed and no reviewer ran: record a review_error "
            "with cause prepare_failed (escalated on the last attempt)"
        ),
    )
    parser.add_argument("--config", metavar="PATH", help="Path to .if-fork/config.yaml")
    parser.add_argument(
        "--diff", metavar="PATH", help="Pinned unified diff that was reviewed"
    )
    parser.add_argument(
        "--files", metavar="PATH", help="Changed file list, one path per line"
    )
    parser.add_argument(
        "--retry-missing",
        action="store_true",
        help=(
            "A missing or invalid reviewer file ends the run as review_error "
            "(exit 4) instead of failing; used by pr-review.yml"
        ),
    )
    parser.add_argument(
        "--ledger-path",
        metavar="PATH",
        help="Ledger to count earlier review_error events for this PR and SHA",
    )
    parser.add_argument(
        "--max-review-errors",
        type=int,
        default=DEFAULT_MAX_REVIEW_ERRORS,
        help="Escalate instead of review_error once this many attempts failed",
    )
    parser.add_argument(
        "--events-out",
        metavar="PATH",
        help="Also write this run's ledger events as JSONL (ledger-event.py --events-file)",
    )
    parser.add_argument("--run-url", default="", help="Workflow run URL for ledger details")
    parser.add_argument(
        "--pr-number",
        required=True,
        type=int,
        help="PR number (embedded in output for traceability)",
    )
    parser.add_argument(
        "--head-sha",
        required=True,
        help="Pinned commit SHA (embedded in output for traceability)",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="Path to write the merged-verdict JSON output",
    )
    args = parser.parse_args(argv)
    if args.prepare_failed:
        return args
    missing = [
        flag
        for flag, value in (
            ("--review-1", args.review_1),
            ("--config", args.config),
            ("--diff", args.diff),
            ("--files", args.files),
        )
        if not value
    ]
    if missing:
        parser.error(f"{', '.join(missing)} required unless --prepare-failed is set")
    if not args.single_review and not args.review_2:
        parser.error("--review-2 is required unless --single-review is set")
    return args


def _origin(args: argparse.Namespace, n: int) -> Origin:
    return Origin(
        reviewer_id=getattr(args, f"review_{n}_id"),
        sha256=getattr(args, f"review_{n}_sha256"),
        upload=getattr(args, f"review_{n}_upload"),
    )


def _load_or_error(
    path: Path, label: str, origin: Origin, errors: list[str], origin_errors: list[str]
) -> ReviewerVerdict | None:
    """Load a verdict that passed the origin checks, or record why it did not."""
    if origin.upload is not None and origin.upload != "success":
        problem = f"{label} upload step did not succeed ({origin.upload or 'no outcome'})"
        errors.append(problem)
        origin_errors.append(problem)
        return None
    try:
        rv = load_reviewer_json(path, label)
    except ReviewLoadError as exc:
        errors.append(str(exc))
        return None
    origin_issue = origin_problem(path, rv, label, origin)
    if origin_issue:
        errors.append(origin_issue)
        origin_errors.append(origin_issue)
        return None
    return rv


def _failed_review(label: str, error: str) -> ReviewerVerdict:
    return {
        "verdict": "escalate-to-human",
        "reasoning": f"{label} produced no valid verdict ({error}).",
        "review_error": error,
    }


def _prepare_failed(args: argparse.Namespace) -> int:
    """Record a failed prepare as one review attempt; escalate on the last one."""
    ledger_path = Path(args.ledger_path) if args.ledger_path else None
    attempt = count_review_errors(ledger_path, args.pr_number, args.head_sha) + 1
    base: OutputVerdict = {
        "pr_number": args.pr_number,
        "head_sha": args.head_sha,
        "cause": "prepare_failed",
    }
    if attempt < args.max_review_errors:
        output: OutputVerdict = {
            **base,
            "verdict": "review_error",
            "attempt": attempt,
            "errors": ["prepare job failed; the review input was not pinned"],
            "reasoning": f"Review attempt {attempt}: the review input could not be prepared.",
        }
        return _write_outputs(args, output, EXIT_REVIEW_ERROR)
    output = {
        **base,
        "verdict": "escalated",
        "escalation_action": "open_review_disagreement_issue",
        "reasoning": (
            f"The review input could not be prepared in {attempt} attempts, so no "
            "reviewer ran. Check the prepare job log of the latest run: for example "
            "the PR head moved, the base SHA is unusable, the diff is empty, or a "
            "file name contains a newline."
        ),
    }
    return _write_outputs(args, output, EXIT_ESCALATED)


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    args = parse_args(argv)
    if args.prepare_failed:
        return _prepare_failed(args)

    try:
        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        diff_text = Path(args.diff).read_text(encoding="utf-8")
        files = [
            line.strip()
            for line in Path(args.files).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, yaml.YAMLError) as exc:
        die(EXIT_ERROR, f"cannot read policy inputs: {exc}")
        raise  # unreachable
    if not diff_text.strip() or not files:
        die(EXIT_ERROR, "pinned diff or file list is empty")

    errors: list[str] = []
    origin_errors: list[str] = []
    r1 = _load_or_error(Path(args.review_1), "review-1", _origin(args, 1), errors, origin_errors)
    r2 = (
        None
        if args.single_review
        else _load_or_error(
            Path(args.review_2), "review-2", _origin(args, 2), errors, origin_errors
        )
    )

    if errors and not args.retry_missing:
        die(EXIT_ERROR, "; ".join(errors))

    if errors:
        ledger_path = Path(args.ledger_path) if args.ledger_path else None
        attempt = count_review_errors(ledger_path, args.pr_number, args.head_sha) + 1
        if attempt < args.max_review_errors:
            output: OutputVerdict = {
                "verdict": "review_error",
                "pr_number": args.pr_number,
                "head_sha": args.head_sha,
                "attempt": attempt,
                "errors": errors,
                "reasoning": f"Review attempt {attempt} produced no valid verdict.",
            }
            if origin_errors:
                output["origin_errors"] = origin_errors
            return _write_outputs(args, output, EXIT_REVIEW_ERROR)
        # Out of retries: hand it to a human.
        if r1 is None:
            r1 = _failed_review("review-1", errors[0])
        if r2 is None and not args.single_review:
            r2 = _failed_review("review-2", errors[-1])

    assert r1 is not None
    policy = evaluate_policy(diff_text, files, config)
    threshold = policy["min_review_confidence"]
    output, exit_code = compute_verdict(
        r1=gate_confidence(r1, threshold),
        r2=None if r2 is None else gate_confidence(r2, threshold),
        pr_number=args.pr_number,
        head_sha=args.head_sha,
    )
    output, exit_code = apply_policy(output, exit_code, policy)
    if origin_errors:
        output["origin_errors"] = origin_errors
    return _write_outputs(args, output, exit_code)


def _write_outputs(args: argparse.Namespace, output: OutputVerdict, exit_code: int) -> int:
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    if args.events_out:
        events = build_ledger_events(output, args.single_review, args.run_url)
        Path(args.events_out).write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8"
        )

    # Echo to stdout for CI log visibility
    print(json.dumps(output, indent=2))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
