#!/usr/bin/env python3
"""Combine two independent reviewer verdicts into a final merge verdict.

Decision matrix (PLAN.md §3.9):
  both safe              -> approved   (exit 0)
  both unsafe            -> rejected   (exit 1)
  any escalate-to-human,
  or safe vs unsafe mix  -> escalated  (exit 2)

Output JSON written to --output path (and to stdout for caller inspection):
  {
    "verdict": "approved" | "escalated" | "rejected",
    "pr_number": N,
    "head_sha": "...",
    "review_1": {...},
    "review_2": {...},
    "reasoning": "..."
  }

On escalation, the output also contains "escalation_action":
  "open_review_disagreement_issue"
so the caller (CI workflow) knows to create a GitHub issue.

--missing-as-escalate turns a missing or invalid reviewer file into an
escalate-to-human verdict, so every review run ends with a recorded outcome.
--events-out writes the ledger events for the run (review_1, review_2,
merged_verdict and the outcome event) as JSONL for ledger-event.py
--events-file, so the run adds one ledger commit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_REVIEWER_VERDICTS: frozenset[str] = frozenset(["safe", "unsafe", "escalate-to-human"])

EXIT_APPROVED = 0
EXIT_REJECTED = 1
EXIT_ESCALATED = 2
EXIT_ERROR = 3

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


def load_or_escalate(path: Path, label: str, missing_as_escalate: bool) -> ReviewerVerdict:
    """Load a verdict; with *missing_as_escalate*, a bad file becomes an escalation.

    A reviewer that crashed or ran out of turns leaves no valid verdict. In the
    pipeline that must still end in a recorded outcome (escalated to a human)
    rather than a failed job that discovery would re-dispatch every day.
    """
    try:
        return load_reviewer_json(path, label)
    except ReviewLoadError as exc:
        if not missing_as_escalate:
            die(EXIT_ERROR, str(exc), **exc.extra)
        return {
            "verdict": "escalate-to-human",
            "reasoning": f"{label} produced no valid verdict ({exc}).",
            "review_error": str(exc),
        }


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

    review_1, review_2 (unless single review), merged_verdict, then the outcome
    event (approved / rejected / escalated). pr-discovery treats merged_verdict
    as "this head_sha is reviewed"; pr-ingestion reads the review_N head_sha.
    """
    pr_number = output["pr_number"]
    head_sha = output["head_sha"]

    def event(name: str, details: dict[str, Any]) -> dict[str, Any]:
        return {"event": name, "pr_number": pr_number, "head_sha": head_sha, "details": details}

    events = [event("review_1", _review_details(output["review_1"], run_url))]
    if not single_review:
        events.append(event("review_2", _review_details(output["review_2"], run_url)))
    verdict = output["verdict"]
    summary: dict[str, Any] = {"verdict": verdict, "reasoning": output["reasoning"][:2000]}
    if run_url:
        summary["run_url"] = run_url
    events.append(event("merged_verdict", summary))
    events.append(event(_OUTCOME_EVENTS[verdict], {"source": "merged_verdict"}))
    return events


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine two independent reviewer verdicts into a final merge verdict."
    )
    parser.add_argument(
        "--review-1",
        required=True,
        metavar="PATH",
        help="Path to reviewer-1 verdict JSON file",
    )
    parser.add_argument(
        "--review-2",
        metavar="PATH",
        help="Path to reviewer-2 verdict JSON file (omit only with --single-review)",
    )
    parser.add_argument(
        "--single-review",
        action="store_true",
        help="Single-review fast path: decide on review-1 alone",
    )
    parser.add_argument(
        "--missing-as-escalate",
        action="store_true",
        help=(
            "Treat a missing or invalid reviewer file as an escalate-to-human "
            "verdict instead of failing (used by pr-review.yml)"
        ),
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
    if not args.single_review and not args.review_2:
        parser.error("--review-2 is required unless --single-review is set")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    r1 = load_or_escalate(Path(args.review_1), "review-1", args.missing_as_escalate)
    r2 = (
        None
        if args.single_review
        else load_or_escalate(Path(args.review_2), "review-2", args.missing_as_escalate)
    )

    output, exit_code = compute_verdict(
        r1=r1,
        r2=r2,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
    )

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
