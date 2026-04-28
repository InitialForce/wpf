"""Tests for tools/merge-verdicts.py.

Covers all validation gates from bead wpf-1kh:
  1.  Both safe -> approved, exit 0
  2.  Both unsafe -> rejected, exit 1
  3.  Mixed safe+escalate-to-human -> escalated, exit 2
  4.  Mixed unsafe+escalate-to-human -> escalated, exit 2
  5.  Mixed safe+unsafe -> escalated, exit 2
  6.  Mixed unsafe+safe (reversed order) -> escalated, exit 2
  7.  Missing review file -> nonzero exit with structured error
  8.  Malformed JSON -> nonzero exit with structured error
  9.  JSON array (not object) -> nonzero exit with structured error
  10. Missing 'verdict' field -> nonzero exit with structured error
  11. Unknown verdict value -> nonzero exit with structured error
  12. Output file is written with correct top-level keys
  13. Escalated output contains escalation_action field
  14. Fixture files: safe+safe -> approved
  15. Fixture files: safe+risky(escalate-to-human) -> escalated
  16. Fixture files: safe+reject(unsafe) -> escalated
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Load the module under test (hyphen in filename requires importlib)
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "tools" / "merge-verdicts.py"
_spec = importlib.util.spec_from_file_location("merge_verdicts", _SCRIPT)
assert _spec is not None and _spec.loader is not None
merge_verdicts: types.ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_verdicts)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Fixture directory
# ---------------------------------------------------------------------------
FIXTURES = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PR_NUMBER = 6511
HEAD_SHA = "abc1234def5678"


def _safe_rv(reviewer_id: str = "r1") -> dict[str, Any]:
    return {
        "verdict": "safe",
        "reasoning": "Looks good.",
        "concerns": [],
        "categories": {"public_api": "unchanged", "native": "none", "security": "ok"},
        "reviewer_id": reviewer_id,
    }


def _unsafe_rv(reviewer_id: str = "r1") -> dict[str, Any]:
    return {
        "verdict": "unsafe",
        "reasoning": "Race condition detected.",
        "concerns": ["thread safety"],
        "categories": {"public_api": "unchanged", "native": "none", "security": "risky"},
        "reviewer_id": reviewer_id,
    }


def _escalate_rv(reviewer_id: str = "r1") -> dict[str, Any]:
    return {
        "verdict": "escalate-to-human",
        "reasoning": "Ambiguous lifetime semantics.",
        "concerns": ["GC pressure"],
        "categories": {"public_api": "unknown", "native": "none", "security": "unclear"},
        "reviewer_id": reviewer_id,
    }


def run_compute(
    r1: dict[str, Any], r2: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Call compute_verdict with test PR / SHA."""
    return merge_verdicts.compute_verdict(  # type: ignore[attr-defined]
        r1=r1, r2=r2, pr_number=PR_NUMBER, head_sha=HEAD_SHA
    )


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


def run_main(
    tmp_path: Path,
    r1_obj: Any,
    r2_obj: Any,
    pr_number: int = PR_NUMBER,
    head_sha: str = HEAD_SHA,
) -> tuple[int, dict[str, Any]]:
    """Write r1/r2 to temp files, run main(), return (exit_code, output_json)."""
    r1_path = tmp_path / "r1.json"
    r2_path = tmp_path / "r2.json"
    out_path = tmp_path / "output.json"
    write_json(r1_path, r1_obj)
    write_json(r2_path, r2_obj)

    argv = [
        "--review-1", str(r1_path),
        "--review-2", str(r2_path),
        "--pr-number", str(pr_number),
        "--head-sha", head_sha,
        "--output", str(out_path),
    ]

    try:
        exit_code = merge_verdicts.main(argv)  # type: ignore[attr-defined]
    except SystemExit as exc:
        exit_code = int(exc.code) if exc.code is not None else 0

    output: dict[str, Any] = {}
    if out_path.exists():
        output = json.loads(out_path.read_text(encoding="utf-8"))

    return exit_code, output


# ===========================================================================
# 1. Both safe -> approved, exit 0
# ===========================================================================
def test_both_safe_approved() -> None:
    output, code = run_compute(_safe_rv("r1"), _safe_rv("r2"))
    assert code == merge_verdicts.EXIT_APPROVED  # type: ignore[attr-defined]
    assert output["verdict"] == "approved"


# ===========================================================================
# 2. Both unsafe -> rejected, exit 1
# ===========================================================================
def test_both_unsafe_rejected() -> None:
    output, code = run_compute(_unsafe_rv("r1"), _unsafe_rv("r2"))
    assert code == merge_verdicts.EXIT_REJECTED  # type: ignore[attr-defined]
    assert output["verdict"] == "rejected"


# ===========================================================================
# 3. Mixed safe + escalate-to-human -> escalated, exit 2
# ===========================================================================
def test_safe_escalate_is_escalated() -> None:
    output, code = run_compute(_safe_rv("r1"), _escalate_rv("r2"))
    assert code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"


# ===========================================================================
# 4. Mixed unsafe + escalate-to-human -> escalated, exit 2
# ===========================================================================
def test_unsafe_escalate_is_escalated() -> None:
    output, code = run_compute(_unsafe_rv("r1"), _escalate_rv("r2"))
    assert code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"


# ===========================================================================
# 5. Mixed safe + unsafe (r1 safe, r2 unsafe) -> escalated, exit 2
# ===========================================================================
def test_safe_unsafe_is_escalated() -> None:
    output, code = run_compute(_safe_rv("r1"), _unsafe_rv("r2"))
    assert code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"


# ===========================================================================
# 6. Mixed unsafe + safe (reversed) -> escalated, exit 2
# ===========================================================================
def test_unsafe_safe_is_escalated() -> None:
    output, code = run_compute(_unsafe_rv("r1"), _safe_rv("r2"))
    assert code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"


# ===========================================================================
# 7. Missing review file -> error exit
# ===========================================================================
def test_missing_review_file_errors(tmp_path: Path) -> None:
    r2_path = tmp_path / "r2.json"
    write_json(r2_path, _safe_rv("r2"))
    out_path = tmp_path / "output.json"

    argv = [
        "--review-1", str(tmp_path / "nonexistent.json"),
        "--review-2", str(r2_path),
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(out_path),
    ]
    with pytest.raises(SystemExit) as exc_info:
        merge_verdicts.main(argv)  # type: ignore[attr-defined]
    assert exc_info.value.code != 0


# ===========================================================================
# 8. Malformed JSON -> error exit
# ===========================================================================
def test_malformed_json_errors(tmp_path: Path) -> None:
    r1_path = tmp_path / "r1.json"
    r2_path = tmp_path / "r2.json"
    r1_path.write_text("not valid { json }", encoding="utf-8")
    write_json(r2_path, _safe_rv("r2"))
    out_path = tmp_path / "output.json"

    argv = [
        "--review-1", str(r1_path),
        "--review-2", str(r2_path),
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(out_path),
    ]
    with pytest.raises(SystemExit) as exc_info:
        merge_verdicts.main(argv)  # type: ignore[attr-defined]
    assert exc_info.value.code != 0


# ===========================================================================
# 9. JSON array (not object) -> error exit
# ===========================================================================
def test_json_array_not_object_errors(tmp_path: Path) -> None:
    exit_code, _ = run_main(tmp_path, [1, 2, 3], _safe_rv("r2"))
    assert exit_code != 0


# ===========================================================================
# 10. Missing 'verdict' field -> error exit
# ===========================================================================
def test_missing_verdict_field_errors(tmp_path: Path) -> None:
    rv_no_verdict = {"reasoning": "no verdict here", "concerns": []}
    exit_code, _ = run_main(tmp_path, rv_no_verdict, _safe_rv("r2"))
    assert exit_code != 0


# ===========================================================================
# 11. Unknown verdict value -> error exit
# ===========================================================================
def test_unknown_verdict_value_errors(tmp_path: Path) -> None:
    rv_bad = {**_safe_rv("r1"), "verdict": "maybe"}
    exit_code, _ = run_main(tmp_path, rv_bad, _safe_rv("r2"))
    assert exit_code != 0


# ===========================================================================
# 12. Output file contains correct top-level keys
# ===========================================================================
def test_output_contains_required_keys(tmp_path: Path) -> None:
    exit_code, output = run_main(tmp_path, _safe_rv("r1"), _safe_rv("r2"))
    assert exit_code == 0
    for key in ("verdict", "pr_number", "head_sha", "review_1", "review_2", "reasoning"):
        assert key in output, f"Missing key: {key}"
    assert output["pr_number"] == PR_NUMBER
    assert output["head_sha"] == HEAD_SHA


# ===========================================================================
# 13. Escalated output contains escalation_action field
# ===========================================================================
def test_escalated_output_has_escalation_action(tmp_path: Path) -> None:
    exit_code, output = run_main(tmp_path, _safe_rv("r1"), _escalate_rv("r2"))
    assert exit_code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output.get("escalation_action") == "open_review_disagreement_issue"


# ===========================================================================
# 14. Fixture: safe + safe -> approved
# ===========================================================================
def test_fixture_safe_safe_approved(tmp_path: Path) -> None:
    safe_json = json.loads((FIXTURES / "verdict-safe.json").read_text())
    exit_code, output = run_main(tmp_path, safe_json, safe_json)
    assert exit_code == 0
    assert output["verdict"] == "approved"


# ===========================================================================
# 15. Fixture: safe + risky (escalate-to-human) -> escalated
# ===========================================================================
def test_fixture_safe_risky_escalated(tmp_path: Path) -> None:
    safe_json = json.loads((FIXTURES / "verdict-safe.json").read_text())
    risky_json = json.loads((FIXTURES / "verdict-risky.json").read_text())
    exit_code, output = run_main(tmp_path, safe_json, risky_json)
    assert exit_code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"


# ===========================================================================
# 16. Fixture: safe + reject (unsafe) -> escalated
# ===========================================================================
def test_fixture_safe_reject_escalated(tmp_path: Path) -> None:
    safe_json = json.loads((FIXTURES / "verdict-safe.json").read_text())
    reject_json = json.loads((FIXTURES / "verdict-reject.json").read_text())
    exit_code, output = run_main(tmp_path, safe_json, reject_json)
    assert exit_code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"
