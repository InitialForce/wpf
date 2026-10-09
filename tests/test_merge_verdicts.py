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

import hashlib
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
        "confidence": 0.99,
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


REPO_CONFIG = Path(__file__).parent.parent / ".if-fork" / "config.yaml"
CLEAN_FILE = "src/Microsoft.DotNet.Wpf/src/PresentationCore/Foo.cs"
CLEAN_DIFF = (
    f"diff --git a/{CLEAN_FILE} b/{CLEAN_FILE}\n"
    f"--- a/{CLEAN_FILE}\n+++ b/{CLEAN_FILE}\n"
    "@@ -1,2 +1,2 @@\n-var list = new ArrayList();\n+var list = new List<int>();\n"
)


def policy_args(
    tmp_path: Path, diff: str = CLEAN_DIFF, files: tuple[str, ...] = (CLEAN_FILE,)
) -> list[str]:
    """Return --config/--diff/--files args pointing at a pinned diff in tmp_path."""
    diff_path = tmp_path / "pr.diff"
    files_path = tmp_path / "files.txt"
    diff_path.write_text(diff, encoding="utf-8")
    files_path.write_text("\n".join(files) + "\n", encoding="utf-8")
    return ["--config", str(REPO_CONFIG), "--diff", str(diff_path), "--files", str(files_path)]


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
        *policy_args(tmp_path),
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
        *policy_args(tmp_path),
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
        *policy_args(tmp_path),
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


# ===========================================================================
# 17. A missing reviewer file is a review_error (retried), not an escalation,
#     until --max-review-errors attempts have failed
# ===========================================================================
def _main_missing_r1(tmp_path: Path, ledger: Path | None = None) -> tuple[int, dict[str, Any]]:
    r2_path = tmp_path / "r2.json"
    write_json(r2_path, _safe_rv("r2"))
    out_path = tmp_path / "output.json"
    argv = [
        "--review-1", str(tmp_path / "missing.json"),
        "--review-2", str(r2_path),
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(out_path),
        "--events-out", str(tmp_path / "events.jsonl"),
        "--retry-missing",
        *policy_args(tmp_path),
    ]
    if ledger is not None:
        argv += ["--ledger-path", str(ledger)]
    code = merge_verdicts.main(argv)  # type: ignore[attr-defined]
    return code, json.loads(out_path.read_text())


def test_missing_file_is_review_error(tmp_path: Path) -> None:
    code, output = _main_missing_r1(tmp_path)
    assert code == merge_verdicts.EXIT_REVIEW_ERROR  # type: ignore[attr-defined]
    assert output["verdict"] == "review_error"
    assert output["attempt"] == 1
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["review_error"]


def test_third_review_error_escalates(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(
        json.dumps({"event": "review_error", "pr_number": PR_NUMBER, "head_sha": HEAD_SHA}) + "\n"
        for _ in range(2)
    ))
    code, output = _main_missing_r1(tmp_path, ledger)
    assert code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"
    assert "no valid verdict" in output["review_1"]["reasoning"]


def test_bad_arguments_exit_3_not_escalated() -> None:
    with pytest.raises(SystemExit) as exc:
        merge_verdicts.main(["--review-1", "a"])  # type: ignore[attr-defined]
    assert exc.value.code == merge_verdicts.EXIT_ERROR  # type: ignore[attr-defined]


# ===========================================================================
# 18. Deterministic policy: model output alone never approves
# ===========================================================================
def _run_with(
    tmp_path: Path, r1: dict[str, Any], r2: dict[str, Any], **policy: Any
) -> tuple[int, dict[str, Any]]:
    write_json(tmp_path / "r1.json", r1)
    write_json(tmp_path / "r2.json", r2)
    out = tmp_path / "out.json"
    code = merge_verdicts.main([  # type: ignore[attr-defined]
        "--review-1", str(tmp_path / "r1.json"),
        "--review-2", str(tmp_path / "r2.json"),
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(out),
        *policy_args(tmp_path, **policy),
    ])
    return code, json.loads(out.read_text())


def test_hard_fail_pattern_in_diff_blocks_approval(tmp_path: Path) -> None:
    diff = CLEAN_DIFF + "+    BinaryFormatter f = new();\n"
    code, output = _run_with(tmp_path, _safe_rv(), _safe_rv("r2"), diff=diff)
    assert code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"
    assert output["policy"]["hard_fail_hits"][0]["pattern"] == "\\bBinaryFormatter\\b"


def test_hard_fail_on_removed_line_counts(tmp_path: Path) -> None:
    diff = CLEAN_DIFF + "-    [SecurityCritical]\n"
    code, output = _run_with(tmp_path, _safe_rv(), _safe_rv("r2"), diff=diff)
    assert output["verdict"] == "escalated"


def test_hard_fail_in_context_line_is_ignored(tmp_path: Path) -> None:
    diff = CLEAN_DIFF + "     BinaryFormatter untouched;\n"
    code, output = _run_with(tmp_path, _safe_rv(), _safe_rv("r2"), diff=diff)
    assert output["verdict"] == "approved"


def test_denylisted_file_blocks_approval(tmp_path: Path) -> None:
    code, output = _run_with(
        tmp_path, _safe_rv(), _safe_rv("r2"), files=(CLEAN_FILE, "global.json")
    )
    assert output["verdict"] == "escalated"
    assert output["policy"]["denylist_hits"][0]["file"] == "global.json"


def test_low_confidence_safe_is_not_approved(tmp_path: Path) -> None:
    low = {**_safe_rv("r2"), "confidence": 0.5}
    code, output = _run_with(tmp_path, _safe_rv(), low)
    assert output["verdict"] == "escalated"
    assert output["policy"]["tier"] == "S"
    assert output["review_2"]["model_verdict"] == "safe"


def test_safe_without_confidence_is_not_approved(tmp_path: Path) -> None:
    no_conf = {k: v for k, v in _safe_rv("r2").items() if k != "confidence"}
    code, output = _run_with(tmp_path, _safe_rv(), no_conf)
    assert output["verdict"] == "escalated"


def test_policy_never_softens_rejection(tmp_path: Path) -> None:
    diff = CLEAN_DIFF + "+    BinaryFormatter f = new();\n"
    code, output = _run_with(tmp_path, _unsafe_rv(), _unsafe_rv("r2"), diff=diff)
    assert output["verdict"] == "rejected"


def test_empty_diff_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _run_with(tmp_path, _safe_rv(), _safe_rv("r2"), diff="")
    assert exc.value.code == merge_verdicts.EXIT_ERROR  # type: ignore[attr-defined]


# ===========================================================================
# 19. --events-out: one batch of ledger events per review run
# ===========================================================================
@pytest.mark.parametrize(
    ("r1", "r2", "outcome", "extra"),
    [
        (_safe_rv(), _safe_rv("r2"), "approved", ["ingestion_requested"]),
        (_unsafe_rv(), _unsafe_rv("r2"), "rejected", []),
        (_safe_rv(), _escalate_rv("r2"), "escalated", []),
    ],
)
def test_events_out_records_reviews_verdict_and_outcome(
    tmp_path: Path, r1: dict[str, Any], r2: dict[str, Any], outcome: str, extra: list[str]
) -> None:
    write_json(tmp_path / "r1.json", r1)
    write_json(tmp_path / "r2.json", r2)
    events_path = tmp_path / "events.jsonl"
    merge_verdicts.main([  # type: ignore[attr-defined]
        "--review-1", str(tmp_path / "r1.json"),
        "--review-2", str(tmp_path / "r2.json"),
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(tmp_path / "out.json"),
        "--events-out", str(events_path),
        "--run-url", "https://example/run/1",
        *policy_args(tmp_path),
    ])
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [e["event"] for e in events] == [
        "review_1", "review_2", "merged_verdict", outcome, *extra
    ]
    assert all(e["pr_number"] == PR_NUMBER and e["head_sha"] == HEAD_SHA for e in events)
    assert events[2]["details"]["verdict"] == outcome
    assert events[2]["details"]["policy"]["tier"] == "S"
    assert events[0]["details"]["run_url"] == "https://example/run/1"


def test_single_review_uses_review_1_only(tmp_path: Path) -> None:
    write_json(tmp_path / "r1.json", _safe_rv())
    events_path = tmp_path / "events.jsonl"
    code = merge_verdicts.main([  # type: ignore[attr-defined]
        "--review-1", str(tmp_path / "r1.json"),
        "--single-review",
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(tmp_path / "out.json"),
        "--events-out", str(events_path),
        *policy_args(tmp_path),
    ])
    events = [json.loads(line)["event"] for line in events_path.read_text().splitlines()]
    assert code == 0
    assert events == ["review_1", "merged_verdict", "approved", "ingestion_requested"]


def test_review_2_required_without_single_review() -> None:
    with pytest.raises(SystemExit) as exc:
        merge_verdicts.parse_args([  # type: ignore[attr-defined]
            "--review-1", "a", "--pr-number", "1", "--head-sha", "x", "--output", "o",
            "--config", "c", "--diff", "d", "--files", "f",
        ])
    assert exc.value.code == merge_verdicts.EXIT_ERROR  # type: ignore[attr-defined]


# ===========================================================================
# 19. Origin checks: a verdict counts only when its own review job produced it
# ===========================================================================
def _origin_args(
    tmp_path: Path, n: int, rv: dict[str, Any], **override: str
) -> list[str]:
    path = tmp_path / f"r{n}.json"
    write_json(path, rv)
    origin = {
        "id": f"model-review-{n}",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "upload": "success",
    }
    origin.update(override)
    return [
        f"--review-{n}", str(path),
        f"--review-{n}-id", origin["id"],
        f"--review-{n}-sha256", origin["sha256"],
        f"--review-{n}-upload", origin["upload"],
    ]


def _run_origin(
    tmp_path: Path, r1_args: list[str], r2_args: list[str]
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    out = tmp_path / "out.json"
    events_path = tmp_path / "events.jsonl"
    code = merge_verdicts.main([  # type: ignore[attr-defined]
        *r1_args, *r2_args,
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(out),
        "--events-out", str(events_path),
        "--retry-missing",
        *policy_args(tmp_path),
    ])
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    return code, json.loads(out.read_text()), events


def test_origin_checks_pass_for_genuine_verdicts(tmp_path: Path) -> None:
    code, output, _ = _run_origin(
        tmp_path,
        _origin_args(tmp_path, 1, _safe_rv("model-review-1")),
        _origin_args(tmp_path, 2, _safe_rv("model-review-2")),
    )
    assert code == merge_verdicts.EXIT_APPROVED  # type: ignore[attr-defined]
    assert "origin_errors" not in output


def test_reviewer_id_of_other_job_is_not_counted(tmp_path: Path) -> None:
    """Regression (Opus NEW-3): review-1 writing a review-2 verdict must not
    count as the second review."""
    code, output, events = _run_origin(
        tmp_path,
        _origin_args(tmp_path, 1, _safe_rv("model-review-1")),
        _origin_args(tmp_path, 2, _safe_rv("model-review-1")),
    )
    assert code == merge_verdicts.EXIT_REVIEW_ERROR  # type: ignore[attr-defined]
    assert output["verdict"] == "review_error"
    assert "reviewer_id" in output["origin_errors"][0]
    assert events[0]["details"]["origin_errors"] == output["origin_errors"]


def test_failed_upload_is_not_counted(tmp_path: Path) -> None:
    """Regression (Opus NEW-3): if review-2's own upload failed (for example
    because another job took the artifact name first), the file is not its."""
    code, output, _ = _run_origin(
        tmp_path,
        _origin_args(tmp_path, 1, _safe_rv("model-review-1")),
        _origin_args(tmp_path, 2, _safe_rv("model-review-2"), upload="failure"),
    )
    assert code == merge_verdicts.EXIT_REVIEW_ERROR  # type: ignore[attr-defined]
    assert output["origin_errors"] == ["review-2 upload step did not succeed (failure)"]


@pytest.mark.parametrize("sha", ["", "0" * 64])
def test_verdict_file_not_matching_job_hash_is_not_counted(tmp_path: Path, sha: str) -> None:
    code, output, _ = _run_origin(
        tmp_path,
        _origin_args(tmp_path, 1, _safe_rv("model-review-1")),
        _origin_args(tmp_path, 2, _safe_rv("model-review-2"), sha256=sha),
    )
    assert code == merge_verdicts.EXIT_REVIEW_ERROR  # type: ignore[attr-defined]
    assert "hash" in output["origin_errors"][0]


def test_origin_failure_escalates_on_third_attempt(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(
        json.dumps({"event": "review_error", "pr_number": PR_NUMBER, "head_sha": HEAD_SHA}) + "\n"
        for _ in range(2)
    ))
    out = tmp_path / "out.json"
    code = merge_verdicts.main([  # type: ignore[attr-defined]
        *_origin_args(tmp_path, 1, _safe_rv("model-review-1")),
        *_origin_args(tmp_path, 2, _safe_rv("model-review-1")),
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(out),
        "--retry-missing",
        "--ledger-path", str(ledger),
        *policy_args(tmp_path),
    ])
    output = json.loads(out.read_text())
    assert code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"
    assert output["origin_errors"]


# ===========================================================================
# 20. A failed prepare is a review attempt (Opus NEW-7)
# ===========================================================================
def _run_prepare_failed(tmp_path: Path, earlier_errors: int) -> tuple[int, dict[str, Any], list]:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(
        json.dumps({"event": "review_error", "pr_number": PR_NUMBER, "head_sha": HEAD_SHA}) + "\n"
        for _ in range(earlier_errors)
    ))
    out = tmp_path / "out.json"
    events_path = tmp_path / "events.jsonl"
    code = merge_verdicts.main([  # type: ignore[attr-defined]
        "--prepare-failed",
        "--ledger-path", str(ledger),
        "--pr-number", str(PR_NUMBER),
        "--head-sha", HEAD_SHA,
        "--output", str(out),
        "--events-out", str(events_path),
    ])
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    return code, json.loads(out.read_text()), events


def test_prepare_failed_records_review_error(tmp_path: Path) -> None:
    code, output, events = _run_prepare_failed(tmp_path, 0)
    assert code == merge_verdicts.EXIT_REVIEW_ERROR  # type: ignore[attr-defined]
    assert output["verdict"] == "review_error"
    assert [e["event"] for e in events] == ["review_error"]
    assert events[0]["details"]["cause"] == "prepare_failed"
    assert events[0]["details"]["attempt"] == 1


def test_third_prepare_failure_escalates_without_review_events(tmp_path: Path) -> None:
    """Regression: a prepare that always fails must end, not be queued daily."""
    code, output, events = _run_prepare_failed(tmp_path, 2)
    assert code == merge_verdicts.EXIT_ESCALATED  # type: ignore[attr-defined]
    assert output["verdict"] == "escalated"
    assert output["escalation_action"] == "open_review_disagreement_issue"
    assert [e["event"] for e in events] == ["merged_verdict", "escalated"]
    assert events[0]["details"]["cause"] == "prepare_failed"


def test_review_inputs_required_without_prepare_failed() -> None:
    with pytest.raises(SystemExit) as exc:
        merge_verdicts.parse_args([  # type: ignore[attr-defined]
            "--review-2", "b", "--pr-number", "1", "--head-sha", "x", "--output", "o",
        ])
    assert exc.value.code == merge_verdicts.EXIT_ERROR  # type: ignore[attr-defined]
