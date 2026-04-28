"""Tests for tools/regenerate-state.py.

Coverage targets (≥ 12 required, targeting ≥ 20):
  - discovered-only state
  - review_1 state with verdict capture
  - review_2 state with verdict capture
  - review disagreement detection
  - approved state
  - cherry_picked state with applied_commit / applied_branch
  - build_failed / build_passed states
  - smoke_failed / smoke_passed states
  - perf_failed / perf_passed states
  - published state with nuget version
  - graduated_upstream flag
  - rejected state
  - escalated state
  - reverted state (terminal bad)
  - full pipeline: discovered→review_1→review_2→approved→cherry_picked ends in cherry_picked
  - malformed-line tolerance
  - blank-line tolerance
  - idempotency: same input → same byte-for-byte output
  - --dry-run outputs valid JSON to stdout
  - --dry-run does not write any file
  - --check pass when state matches
  - --check fail when state does not match
  - graduation override: published PR can graduate
  - partial state: PR with only review_1 shows review_1 state
  - multiple PRs in one ledger produce correct per-PR records
  - first_seen is set from the first event timestamp
  - head_sha locked from discovered event
  - tier captured from discovered event
  - applied_commit and applied_branch captured from cherry_picked
  - first_published_in set from first published event only
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import module under test (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "tools" / "regenerate-state.py"
_spec = importlib.util.spec_from_file_location("regenerate_state", _SCRIPT)
assert _spec is not None and _spec.loader is not None
regen: types.ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(regen)  # type: ignore[union-attr]

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_line(
    event: str,
    pr_number: int,
    head_sha: str = "abc123",
    ts: str = "2026-04-01T10:00:00Z",
    details: dict | None = None,
    if_tier: str | None = None,
) -> str:
    obj: dict = {
        "schema_version": 1,
        "ts": ts,
        "event": event,
        "actor": "test-bot",
        "pr_number": pr_number,
        "head_sha": head_sha,
        "details": details or {},
    }
    if if_tier is not None:
        obj["if_tier"] = if_tier
    return json.dumps(obj)


def replay(lines: list[str]) -> dict[int, dict]:
    return regen.replay_ledger("\n".join(lines))


def replay_fixture(filename: str) -> dict[int, dict]:
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    return regen.replay_ledger(text)


# ---------------------------------------------------------------------------
# 1. discovered-only state
# ---------------------------------------------------------------------------


def test_discovered_only() -> None:
    state = replay([make_line("discovered", 101, if_tier="S")])
    assert 101 in state
    assert state[101]["state"] == "discovered"
    assert state[101]["head_sha"] == "abc123"
    assert state[101]["tier"] == "S"


# ---------------------------------------------------------------------------
# 2. review_1 state and verdict capture
# ---------------------------------------------------------------------------


def test_review_1_state_and_verdict() -> None:
    lines = [
        make_line("discovered", 102),
        make_line("review_1", 102, details={
            "verdict": "safe", "confidence": 0.95, "model": "claude-opus-4-7",
        }),
    ]
    state = replay(lines)
    assert state[102]["state"] == "review_1"
    assert state[102]["review_1_verdict"] == "safe"
    assert len(state[102]["review_verdicts"]) == 1
    assert state[102]["review_verdicts"][0]["verdict"] == "safe"
    assert state[102]["review_verdicts"][0]["confidence"] == 0.95


# ---------------------------------------------------------------------------
# 3. review_2 state and verdict capture
# ---------------------------------------------------------------------------


def test_review_2_state_and_verdict() -> None:
    lines = [
        make_line("discovered", 103),
        make_line("review_1", 103, details={"verdict": "safe", "confidence": 0.9}),
        make_line("review_2", 103, details={"verdict": "safe", "confidence": 0.88}),
    ]
    state = replay(lines)
    assert state[103]["state"] == "review_2"
    assert state[103]["review_2_verdict"] == "safe"
    assert len(state[103]["review_verdicts"]) == 2


# ---------------------------------------------------------------------------
# 4. review disagreement detection
# ---------------------------------------------------------------------------


def test_review_disagreement_detected() -> None:
    lines = [
        make_line("discovered", 104),
        make_line("review_1", 104, details={"verdict": "safe"}),
        make_line("review_2", 104, details={"verdict": "unsafe"}),
    ]
    state = replay(lines)
    assert state[104]["review_disagreement"] is True


def test_no_review_disagreement_when_both_safe() -> None:
    lines = [
        make_line("discovered", 105),
        make_line("review_1", 105, details={"verdict": "safe"}),
        make_line("review_2", 105, details={"verdict": "safe"}),
    ]
    state = replay(lines)
    assert state[105]["review_disagreement"] is False


# ---------------------------------------------------------------------------
# 5. approved state
# ---------------------------------------------------------------------------


def test_approved_state() -> None:
    lines = [
        make_line("discovered", 106),
        make_line("review_1", 106, details={"verdict": "safe"}),
        make_line("review_2", 106, details={"verdict": "safe"}),
        make_line("approved", 106, details={"review_1_event_ts": "t1", "review_2_event_ts": "t2"}),
    ]
    state = replay(lines)
    assert state[106]["state"] == "approved"


# ---------------------------------------------------------------------------
# 6. cherry_picked state with applied_commit and applied_branch
# ---------------------------------------------------------------------------


def test_cherry_picked_captures_applied_info() -> None:
    lines = [
        make_line("discovered", 107),
        make_line("review_1", 107, details={"verdict": "safe"}),
        make_line("review_2", 107, details={"verdict": "safe"}),
        make_line("approved", 107, details={}),
        make_line("cherry_picked", 107, details={
            "applied_branch": "if/release/10.0", "applied_commit": "def456abc",
        }),
    ]
    state = replay(lines)
    assert state[107]["state"] == "cherry_picked"
    assert state[107]["applied_commit"] == "def456abc"
    assert state[107]["applied_branch"] == "if/release/10.0"


# ---------------------------------------------------------------------------
# 7. Full pipeline: discovered→review_1→review_2→approved→cherry_picked
# ---------------------------------------------------------------------------


def test_full_pipeline_ends_in_cherry_picked() -> None:
    lines = [
        make_line("discovered", 200),
        make_line("review_1", 200, details={"verdict": "safe"}),
        make_line("review_2", 200, details={"verdict": "safe"}),
        make_line("approved", 200, details={}),
        make_line("cherry_picked", 200, details={
            "applied_branch": "if/release/10.0", "applied_commit": "xyz",
        }),
    ]
    state = replay(lines)
    assert state[200]["state"] == "cherry_picked"


# ---------------------------------------------------------------------------
# 8. build_failed / build_passed states
# ---------------------------------------------------------------------------


def test_build_failed_state() -> None:
    lines = [
        make_line("discovered", 108),
        make_line("cherry_picked", 108, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("build_failed", 108, details={
            "workflow_run_url": "u", "failed_target": "PresentationCore",
        }),
    ]
    state = replay(lines)
    assert state[108]["state"] == "build_failed"


def test_build_passed_state() -> None:
    lines = [
        make_line("discovered", 109),
        make_line("cherry_picked", 109, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("build_passed", 109, details={"workflow_run_url": "u"}),
    ]
    state = replay(lines)
    assert state[109]["state"] == "build_passed"


# ---------------------------------------------------------------------------
# 9. smoke_failed / smoke_passed states
# ---------------------------------------------------------------------------


def test_smoke_failed_state() -> None:
    lines = [
        make_line("discovered", 110),
        make_line("cherry_picked", 110, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("build_passed", 110, details={}),
        make_line("smoke_failed", 110, details={"failed_scenarios": ["basic-render"]}),
    ]
    state = replay(lines)
    assert state[110]["state"] == "smoke_failed"


def test_smoke_passed_state() -> None:
    lines = [
        make_line("discovered", 111),
        make_line("cherry_picked", 111, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("build_passed", 111, details={}),
        make_line("smoke_passed", 111, details={}),
    ]
    state = replay(lines)
    assert state[111]["state"] == "smoke_passed"


# ---------------------------------------------------------------------------
# 10. perf_failed / perf_passed states
# ---------------------------------------------------------------------------


def test_perf_failed_state() -> None:
    lines = [
        make_line("discovered", 112),
        make_line("cherry_picked", 112, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("build_passed", 112, details={}),
        make_line("smoke_passed", 112, details={}),
        make_line("perf_failed", 112, details={
            "regressions": [{"metric": "render", "delta_pct": 7.5}],
        }),
    ]
    state = replay(lines)
    assert state[112]["state"] == "perf_failed"


def test_perf_passed_state() -> None:
    lines = [
        make_line("discovered", 113),
        make_line("cherry_picked", 113, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("build_passed", 113, details={}),
        make_line("smoke_passed", 113, details={}),
        make_line("perf_passed", 113, details={"regressions": []}),
    ]
    state = replay(lines)
    assert state[113]["state"] == "perf_passed"


# ---------------------------------------------------------------------------
# 11. published state and first_published_in
# ---------------------------------------------------------------------------


def test_published_state_and_nuget_version() -> None:
    lines = [
        make_line("discovered", 114),
        make_line("cherry_picked", 114, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("build_passed", 114, details={}),
        make_line("smoke_passed", 114, details={}),
        make_line("perf_passed", 114, details={}),
        make_line("published", 114, details={
            "nuget_version": "10.0.4-if.20260401.1", "nuget_url": "u", "git_tag": "t",
        }),
    ]
    state = replay(lines)
    assert state[114]["state"] == "published"
    assert state[114]["first_published_in"] == "10.0.4-if.20260401.1"


def test_first_published_in_not_overwritten() -> None:
    """A second published event must not overwrite first_published_in."""
    lines = [
        make_line("discovered", 115),
        make_line("cherry_picked", 115, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("published", 115, details={
            "nuget_version": "10.0.4-if.20260401.1", "nuget_url": "u", "git_tag": "t1",
        }),
        make_line("published", 115, details={
            "nuget_version": "10.0.4-if.20260402.1", "nuget_url": "u2", "git_tag": "t2",
        }),
    ]
    state = replay(lines)
    assert state[115]["first_published_in"] == "10.0.4-if.20260401.1"


# ---------------------------------------------------------------------------
# 12. graduated_upstream flag and state
# ---------------------------------------------------------------------------


def test_graduated_upstream_flag() -> None:
    lines = [
        make_line("discovered", 116),
        make_line("cherry_picked", 116, details={"applied_branch": "b", "applied_commit": "c"}),
        make_line("published", 116, details={
            "nuget_version": "10.0.4-if.20260401.1", "nuget_url": "u", "git_tag": "t",
        }),
        make_line("graduated_upstream", 116, details={
            "upstream_commit_sha": "usha", "detection_method": "patch_id",
        }),
    ]
    state = replay(lines)
    assert state[116]["state"] == "graduated_upstream"
    assert state[116]["graduated_upstream"] is True


# ---------------------------------------------------------------------------
# 13. rejected state
# ---------------------------------------------------------------------------


def test_rejected_state() -> None:
    lines = [
        make_line("discovered", 117),
        make_line("review_1", 117, details={"verdict": "unsafe"}),
        make_line("review_2", 117, details={"verdict": "unsafe"}),
        make_line("rejected", 117, details={"unsafe_reasons": ["reason1", "reason2"]}),
    ]
    state = replay(lines)
    assert state[117]["state"] == "rejected"


# ---------------------------------------------------------------------------
# 14. escalated state
# ---------------------------------------------------------------------------


def test_escalated_state() -> None:
    lines = [
        make_line("discovered", 118),
        make_line("review_1", 118, details={"verdict": "escalate-to-human"}),
        make_line("escalated", 118, details={"issue_url": "u", "reason": "r"}),
    ]
    state = replay(lines)
    assert state[118]["state"] == "escalated"


# ---------------------------------------------------------------------------
# 15. reverted state (terminal bad)
# ---------------------------------------------------------------------------


def test_reverted_state() -> None:
    lines = [
        make_line("discovered", 119),
        make_line("published", 119, details={
            "nuget_version": "v1", "nuget_url": "u", "git_tag": "t",
        }),
        make_line("reverted", 119, details={"reason": "regression", "incident_issue_url": "u"}),
    ]
    state = replay(lines)
    assert state[119]["state"] == "reverted"


# ---------------------------------------------------------------------------
# 16. Malformed-line tolerance
# ---------------------------------------------------------------------------


def test_malformed_line_skipped() -> None:
    lines = [
        make_line("discovered", 120),
        "THIS IS NOT JSON AT ALL",
        make_line("review_1", 120, details={"verdict": "safe"}),
    ]
    state = replay(lines)
    # Malformed line should not abort; review_1 is still applied
    assert state[120]["state"] == "review_1"


def test_blank_lines_skipped() -> None:
    lines = [
        "",
        make_line("discovered", 121),
        "",
        make_line("review_1", 121, details={"verdict": "safe"}),
        "",
    ]
    state = replay(lines)
    assert state[121]["state"] == "review_1"


def test_non_dict_json_line_skipped() -> None:
    lines = [
        make_line("discovered", 122),
        "[1, 2, 3]",
        make_line("review_1", 122, details={"verdict": "safe"}),
    ]
    state = replay(lines)
    assert state[122]["state"] == "review_1"


# ---------------------------------------------------------------------------
# 17. Idempotency: same input → same byte-for-byte output
# ---------------------------------------------------------------------------


def test_idempotency(tmp_path: Path) -> None:
    fixture = FIXTURES / "ledger-multistate.jsonl"
    state1 = regen.replay_ledger(fixture.read_text(encoding="utf-8"))
    state2 = regen.replay_ledger(fixture.read_text(encoding="utf-8"))
    json1 = regen.build_state_json(state1)
    json2 = regen.build_state_json(state2)
    assert json1 == json2, "Same ledger must produce identical JSON output"


def test_build_state_json_is_valid_json() -> None:
    lines = [make_line("discovered", 123, if_tier="S")]
    state = replay(lines)
    result = regen.build_state_json(state)
    parsed = json.loads(result)
    assert "123" in parsed


# ---------------------------------------------------------------------------
# 18. --dry-run writes to stdout, not to file
# ---------------------------------------------------------------------------


def test_dry_run_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(make_line("discovered", 124) + "\n", encoding="utf-8")
    state_out = tmp_path / "patch-state.json"

    argv = ["--ledger-path", str(ledger), "--state-path", str(state_out), "--dry-run"]
    rc = regen.main(argv)

    assert rc == 0
    assert not state_out.exists(), "--dry-run must not write state file"
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "124" in parsed


# ---------------------------------------------------------------------------
# 19. --check pass when state matches
# ---------------------------------------------------------------------------


def test_check_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(make_line("discovered", 125) + "\n", encoding="utf-8")

    # First write the state
    state_path = tmp_path / "patch-state.json"
    rc_write = regen.main(["--ledger-path", str(ledger), "--state-path", str(state_path)])
    assert rc_write == 0

    # Now --check should pass
    rc_check = regen.main([
        "--ledger-path", str(ledger), "--state-path", str(state_path), "--check",
    ])
    assert rc_check == 0


# ---------------------------------------------------------------------------
# 20. --check fail when state does not match
# ---------------------------------------------------------------------------


def test_check_fail_when_mismatch(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(make_line("discovered", 126) + "\n", encoding="utf-8")

    state_path = tmp_path / "patch-state.json"
    state_path.write_text('{"126": {"state": "cherry_picked"}}', encoding="utf-8")

    rc = regen.main(["--ledger-path", str(ledger), "--state-path", str(state_path), "--check"])
    assert rc == 1


def test_check_fail_when_state_file_missing(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(make_line("discovered", 127) + "\n", encoding="utf-8")
    state_path = tmp_path / "nonexistent.json"

    rc = regen.main(["--ledger-path", str(ledger), "--state-path", str(state_path), "--check"])
    assert rc == 1


# ---------------------------------------------------------------------------
# 21. Graduation override: published PR can transition to graduated_upstream
# ---------------------------------------------------------------------------


def test_graduation_override() -> None:
    lines = [
        make_line("discovered", 128),
        make_line("published", 128, details={
            "nuget_version": "v", "nuget_url": "u", "git_tag": "t",
        }),
        make_line("graduated_upstream", 128, details={
            "upstream_commit_sha": "s", "detection_method": "patch_id",
        }),
    ]
    state = replay(lines)
    assert state[128]["state"] == "graduated_upstream"
    assert state[128]["graduated_upstream"] is True


# ---------------------------------------------------------------------------
# 22. Partial state: PR with only review_1 shows review_1 state
# ---------------------------------------------------------------------------


def test_partial_state_review_1_only() -> None:
    lines = [
        make_line("discovered", 129),
        make_line("review_1", 129, details={"verdict": "safe"}),
    ]
    state = replay(lines)
    assert state[129]["state"] == "review_1"
    assert state[129]["review_1_verdict"] == "safe"
    assert state[129]["review_2_verdict"] is None


# ---------------------------------------------------------------------------
# 23. Multiple PRs in one ledger → correct per-PR records
# ---------------------------------------------------------------------------


def test_multiple_prs_independent() -> None:
    lines = [
        make_line("discovered", 130, ts="2026-04-01T10:00:00Z"),
        make_line("discovered", 131, ts="2026-04-01T11:00:00Z"),
        make_line("review_1", 130, details={"verdict": "safe"}, ts="2026-04-01T12:00:00Z"),
        make_line("rejected", 131, details={"unsafe_reasons": []}, ts="2026-04-01T13:00:00Z"),
    ]
    state = replay(lines)
    assert state[130]["state"] == "review_1"
    assert state[131]["state"] == "rejected"


# ---------------------------------------------------------------------------
# 24. first_seen set from first event timestamp
# ---------------------------------------------------------------------------


def test_first_seen_from_first_event() -> None:
    lines = [
        make_line("discovered", 132, ts="2026-04-01T08:00:00Z"),
        make_line("review_1", 132, details={"verdict": "safe"}, ts="2026-04-01T09:00:00Z"),
    ]
    state = replay(lines)
    assert state[132]["first_seen"] == "2026-04-01T08:00:00Z"


# ---------------------------------------------------------------------------
# 25. head_sha locked from discovered event
# ---------------------------------------------------------------------------


def test_head_sha_locked_from_discovered() -> None:
    discovered_sha = "sha_from_discovered"
    later_sha = "sha_from_later"
    lines = [
        make_line("discovered", 133, head_sha=discovered_sha),
        make_line("review_1", 133, head_sha=later_sha, details={"verdict": "safe"}),
    ]
    state = replay(lines)
    # head_sha should be locked to discovered SHA
    assert state[133]["head_sha"] == discovered_sha


# ---------------------------------------------------------------------------
# 26. Fixture covers full state machine (one PR per state)
# ---------------------------------------------------------------------------


def test_fixture_multistate_coverage() -> None:
    """The ledger-multistate.jsonl fixture must have at least one PR per major state."""
    state = replay_fixture("ledger-multistate.jsonl")
    states_present = {rec["state"] for rec in state.values()}
    required_states = {
        "graduated_upstream",  # PR 1001
        "rejected",             # PR 1002
        "escalated",            # PR 1003
        "build_failed",         # PR 1004
        "published",            # PR 1005
        "smoke_failed",         # PR 1006
        "perf_failed",          # PR 1007
        "reverted",             # PR 1008
        "discovered",           # PR 1009
    }
    missing = required_states - states_present
    assert not missing, f"Missing states in fixture: {missing}"


def test_fixture_unique_prs() -> None:
    """Each PR in the fixture must appear exactly once in the output."""
    state = replay_fixture("ledger-multistate.jsonl")
    assert len(state) == 9


# ---------------------------------------------------------------------------
# 27. Output JSON is valid and keyed by string pr_number
# ---------------------------------------------------------------------------


def test_output_keys_are_strings() -> None:
    lines = [make_line("discovered", 134, if_tier="S")]
    state = replay(lines)
    result_str = regen.build_state_json(state)
    result = json.loads(result_str)
    for key in result:
        assert isinstance(key, str), f"Key {key!r} must be a string"


# ---------------------------------------------------------------------------
# 28. Deterministic JSON output (sorted keys)
# ---------------------------------------------------------------------------


def test_deterministic_sorted_keys() -> None:
    lines = [make_line("discovered", 135, if_tier="A")]
    state = replay(lines)
    json_str = regen.build_state_json(state)
    # Parse and re-encode; since we use sort_keys=True, re-encoding must match
    parsed = json.loads(json_str)
    re_encoded = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    assert json_str == re_encoded


# ---------------------------------------------------------------------------
# 29. --state-path guardrail: tests use /tmp, never touch .if-fork/
# ---------------------------------------------------------------------------


def test_write_uses_specified_state_path(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(make_line("discovered", 136) + "\n", encoding="utf-8")
    state_path = tmp_path / "patch-state.json"

    rc = regen.main(["--ledger-path", str(ledger), "--state-path", str(state_path)])
    assert rc == 0
    assert state_path.exists()
    parsed = json.loads(state_path.read_text(encoding="utf-8"))
    assert "136" in parsed


# ---------------------------------------------------------------------------
# 30. Missing ledger exits nonzero
# ---------------------------------------------------------------------------


def test_missing_ledger_exits_nonzero(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        regen.main([
            "--ledger-path", str(tmp_path / "nonexistent.jsonl"),
            "--state-path", str(tmp_path / "out.json"),
        ])
    assert exc_info.value.code != 0
