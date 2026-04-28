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


def load_reviewer_json(path: Path, label: str) -> ReviewerVerdict:
    """Read and validate a reviewer-verdict JSON file."""
    if not path.exists():
        die(EXIT_ERROR, f"{label} file not found", path=str(path))

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        die(EXIT_ERROR, f"Cannot read {label}", path=str(path), detail=str(exc))

    try:
        obj: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(
            EXIT_ERROR,
            f"{label} is not valid JSON",
            path=str(path),
            detail=str(exc),
        )

    if not isinstance(obj, dict):
        die(
            EXIT_ERROR,
            f"{label} must be a JSON object",
            path=str(path),
            actual_type=type(obj).__name__,
        )

    verdict = obj.get("verdict")
    if not isinstance(verdict, str):
        die(
            EXIT_ERROR,
            f"{label} missing required 'verdict' field",
            path=str(path),
        )
    if verdict not in VALID_REVIEWER_VERDICTS:
        die(
            EXIT_ERROR,
            f"{label} has unrecognised 'verdict' value",
            path=str(path),
            value=verdict,
            valid=sorted(VALID_REVIEWER_VERDICTS),
        )

    for required_field in ("reasoning", "concerns", "categories"):
        if required_field not in obj:
            # Tolerate missing optional structured fields — normalise to defaults.
            # "reasoning" is also acceptable under the alias "rationale" used by review prompts.
            pass

    return dict(obj)


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
    r2: ReviewerVerdict,
    pr_number: int,
    head_sha: str,
) -> tuple[OutputVerdict, int]:
    """Apply the decision matrix and return (output_dict, exit_code)."""
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
        required=True,
        metavar="PATH",
        help="Path to reviewer-2 verdict JSON file",
    )
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    r1 = load_reviewer_json(Path(args.review_1), "review-1")
    r2 = load_reviewer_json(Path(args.review_2), "review-2")

    output, exit_code = compute_verdict(
        r1=r1,
        r2=r2,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    # Echo to stdout for CI log visibility
    print(json.dumps(output, indent=2))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
