"""
Tests for .github/workflows/autonomy-check.yml

Validates the YAML structure and gating logic references without executing
the workflow. If actionlint is available it will be called as well; if not,
the structural assertions below serve as the floor.

Security checks (CRIT-1):
- Default-deny for unknown requested_action values
- All 6 known actions enumerated in the gate script
- Unknown action sets proceed=false and reason contains "unknown requested_action"
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "autonomy-check.yml"
CALLER_PATH = REPO_ROOT / ".github" / "workflows" / "test-autonomy-check.yml"


def _normalize_on_key(doc: dict) -> dict:
    """
    PyYAML parses the bare YAML key ``on`` as the Python boolean ``True``
    (YAML 1.1 spec). Normalize it back to the string ``"on"`` so tests can
    use consistent key names.
    """
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Parse and return the autonomy-check.yml document."""
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


@pytest.fixture(scope="module")
def caller_workflow() -> dict:
    """Parse and return the test-autonomy-check.yml document."""
    with CALLER_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


# ---------------------------------------------------------------------------
# Case 1: on.workflow_call exists
# ---------------------------------------------------------------------------
def test_workflow_call_trigger_exists(workflow: dict) -> None:
    """The workflow must be triggered via workflow_call."""
    assert "on" in workflow, "Missing 'on' key"
    on_block = workflow["on"]
    assert "workflow_call" in on_block, (
        "'workflow_call' trigger not found; this workflow must be reusable"
    )


# ---------------------------------------------------------------------------
# Case 2: expected inputs declared
# ---------------------------------------------------------------------------
def test_inputs_declared(workflow: dict) -> None:
    """workflow_call must declare the required inputs."""
    wc = workflow["on"]["workflow_call"]
    assert "inputs" in wc, "workflow_call.inputs block missing"
    inputs = wc["inputs"]

    assert "requested_action" in inputs, "'requested_action' input not declared"
    assert inputs["requested_action"]["type"] == "string", (
        "'requested_action' must be type string"
    )
    assert inputs["requested_action"].get("required") is True, (
        "'requested_action' must be required"
    )

    assert "bypass_for_human_dispatch" in inputs, (
        "'bypass_for_human_dispatch' input not declared"
    )
    assert inputs["bypass_for_human_dispatch"]["type"] == "boolean", (
        "'bypass_for_human_dispatch' must be type boolean"
    )
    assert inputs["bypass_for_human_dispatch"].get("default") is False, (
        "'bypass_for_human_dispatch' default must be false"
    )


# ---------------------------------------------------------------------------
# Case 3: expected outputs declared (proceed, reason)
# ---------------------------------------------------------------------------
def test_outputs_declared(workflow: dict) -> None:
    """workflow_call must declare 'proceed' and 'reason' outputs."""
    wc = workflow["on"]["workflow_call"]
    assert "outputs" in wc, "workflow_call.outputs block missing"
    outputs = wc["outputs"]

    assert "proceed" in outputs, "'proceed' output not declared"
    assert "reason" in outputs, "'reason' output not declared"

    # Values must reference the gate job outputs
    assert "gate" in outputs["proceed"]["value"], (
        "'proceed' output must reference jobs.gate.outputs.proceed"
    )
    assert "gate" in outputs["reason"]["value"], (
        "'reason' output must reference jobs.gate.outputs.reason"
    )


# ---------------------------------------------------------------------------
# Case 4: gating logic references the correct vars
# ---------------------------------------------------------------------------
def test_gating_logic_references_vars(workflow: dict) -> None:
    """The gate step script must reference IF_AUTONOMY_ENABLED and IF_AUTOMERGE_FROZEN."""
    jobs = workflow.get("jobs", {})
    gate_job = jobs.get("gate", {})
    steps = gate_job.get("steps", [])
    assert steps, "gate job must have at least one step"

    # Collect all run scripts across steps
    combined_script = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )

    assert "${{ vars.IF_AUTONOMY_ENABLED }}" in combined_script, (
        "Gate script must reference ${{ vars.IF_AUTONOMY_ENABLED }} literally"
    )
    assert "${{ vars.IF_AUTOMERGE_FROZEN }}" in combined_script, (
        "Gate script must reference ${{ vars.IF_AUTOMERGE_FROZEN }} literally"
    )


# ---------------------------------------------------------------------------
# Case 5: caller workflow has the two expected jobs
# ---------------------------------------------------------------------------
def test_caller_workflow_structure(caller_workflow: dict) -> None:
    """test-autonomy-check.yml must have a 'gate' job and a 'report' job."""
    jobs = caller_workflow.get("jobs", {})
    assert "gate" in jobs, "Caller workflow must have a 'gate' job"
    assert "report" in jobs, "Caller workflow must have a 'report' job"

    gate_job = jobs["gate"]
    assert "uses" in gate_job, "gate job must use the reusable workflow via 'uses'"
    assert "autonomy-check.yml" in gate_job["uses"], (
        "gate job must reference autonomy-check.yml"
    )


# ---------------------------------------------------------------------------
# Case 6: default-deny for unknown requested_action (CRIT-1)
# ---------------------------------------------------------------------------

KNOWN_ACTIONS = [
    "claude-invoke",
    "auto-merge",
    "discovery-scan",
    "release-publish",
    "cherry-pick",
    "upstream-stable-adoption",
]


def _gate_combined_script(workflow: dict) -> str:
    """Return all run scripts from the gate job's steps, joined."""
    jobs = workflow.get("jobs", {})
    gate_job = jobs.get("gate", {})
    steps = gate_job.get("steps", [])
    return "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )


def test_default_deny_logic_present(workflow: dict) -> None:
    """Gate script must block unknown requested_action values (default-deny)."""
    script = _gate_combined_script(workflow)
    assert "unknown requested_action" in script, (
        "Gate script must emit 'unknown requested_action: <X>' reason for unknown actions (CRIT-1)"
    )


def test_default_deny_sets_proceed_false(workflow: dict) -> None:
    """Gate script must set proceed=false for unknown actions."""
    script = _gate_combined_script(workflow)
    # Must have both the default-deny block and proceed=false
    assert "proceed=false" in script, (
        "Gate script must set proceed=false somewhere (covers unknown action path)"
    )


def test_all_known_actions_enumerated(workflow: dict) -> None:
    """All 6 known actions must appear in the gate script's allow-list."""
    script = _gate_combined_script(workflow)
    for action in KNOWN_ACTIONS:
        assert action in script, (
            f"Known action '{action}' is not listed in the gate script's allow-list"
        )


def test_default_deny_is_before_kill_switch(workflow: dict) -> None:
    """Default-deny (Gate 0) must appear before the global kill switch (Gate 1) in the script."""
    script = _gate_combined_script(workflow)
    # Locate the default-deny block via its distinctive reason string
    idx_deny = script.find("unknown requested_action")
    # Locate the kill-switch gate logic via its conditional check (not the variable assignment)
    idx_kill = script.find("autonomy disabled by IF_AUTONOMY_ENABLED")
    assert idx_deny >= 0, "Default-deny block not found in gate script"
    assert idx_kill >= 0, (
        "IF_AUTONOMY_ENABLED kill-switch reason string not found in gate script; "
        "expected 'autonomy disabled by IF_AUTONOMY_ENABLED'"
    )
    assert idx_deny < idx_kill, (
        "Default-deny (Gate 0) must appear before the IF_AUTONOMY_ENABLED kill-switch (Gate 1)"
    )


# ---------------------------------------------------------------------------
# Case 7: actionlint (skipped gracefully if not installed)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint not available; skipping lint gate",
)
def test_actionlint_autonomy_check() -> None:
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint failed on autonomy-check.yml:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint not available; skipping lint gate",
)
def test_actionlint_test_caller() -> None:
    result = subprocess.run(
        ["actionlint", str(CALLER_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint failed on test-autonomy-check.yml:\n{result.stdout}\n{result.stderr}"
    )
