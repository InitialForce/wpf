"""
Tests for .github/workflows/claude-on-failure.yml

Validates structural requirements of the CI failure-analysis workflow:
1. YAML parses cleanly
2. All 3 required jobs are present: gate, analyze, record
3. workflow_run trigger only acts on conclusion == 'failure' (via job-level if:)
4. References .if-fork/prompts/failure-analysis.md
5. Ledger event failure_analyzed is emitted
6. Concurrency group uses workflow_run event id (one invocation per failed run)
7. Required permissions: actions:read, contents:read, issues:write, id-token:write
8. gate job uses autonomy-check.yml with requested_action: claude-invoke
9. analyze job needs gate and checks gate output
10. record job needs both gate and analyze; always() guard present
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "claude-on-failure.yml"
FAILURE_ANALYSIS_PROMPT_PATH = REPO_ROOT / ".if-fork" / "prompts" / "failure-analysis.md"

REQUIRED_JOBS = {"gate", "analyze", "record"}

# Workflows that must be listed in the workflow_run trigger
EXPECTED_WORKFLOWS = {
    "Build",
    "Nightly Rebase",
    "Release",
}


def _normalize_on_key(doc: dict) -> dict:
    """
    PyYAML parses bare YAML key ``on`` as Python boolean ``True`` (YAML 1.1 spec).
    Normalize it back to the string ``"on"`` so tests can use consistent key names.
    """
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Parse and return the claude-on-failure.yml document."""
    assert WORKFLOW_PATH.exists(), f"Workflow file not found: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


# ---------------------------------------------------------------------------
# Gate 1: YAML parses cleanly
# ---------------------------------------------------------------------------
def test_yaml_parses() -> None:
    """claude-on-failure.yml must be valid YAML."""
    assert WORKFLOW_PATH.exists(), f"Missing: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert doc is not None, "YAML parsed to None (empty file?)"
    assert isinstance(doc, dict), "Top-level YAML document must be a mapping"


# ---------------------------------------------------------------------------
# Gate 2: All 3 required jobs are present
# ---------------------------------------------------------------------------
def test_all_three_jobs_present(workflow: dict) -> None:
    """Workflow must define the 3 required jobs: gate, analyze, record."""
    jobs = workflow.get("jobs", {})
    missing = REQUIRED_JOBS - set(jobs.keys())
    assert not missing, f"Missing required jobs: {missing}. Found: {set(jobs.keys())}"


# ---------------------------------------------------------------------------
# Gate 3: workflow_run filter only acts on conclusion == 'failure'
# ---------------------------------------------------------------------------
def test_failure_conclusion_filter_on_gate_job(workflow: dict) -> None:
    """
    The workflow must only act on failed runs. This must be enforced either via
    a job-level 'if:' condition on the gate job checking
    github.event.workflow_run.conclusion == 'failure'.
    """
    jobs = workflow.get("jobs", {})
    gate_job = jobs.get("gate", {})
    gate_if = str(gate_job.get("if", ""))
    assert "failure" in gate_if, (
        f"gate job must have an 'if:' condition filtering on conclusion == 'failure'; "
        f"got: {gate_if!r}"
    )
    assert "workflow_run.conclusion" in gate_if, (
        f"gate job 'if:' must reference github.event.workflow_run.conclusion; "
        f"got: {gate_if!r}"
    )


def test_workflow_run_trigger_present(workflow: dict) -> None:
    """Workflow must be triggered by workflow_run events."""
    on_block = workflow.get("on", {})
    assert "workflow_run" in on_block, "Missing 'workflow_run' trigger"


def test_workflow_run_types_completed(workflow: dict) -> None:
    """workflow_run trigger must listen for 'completed' type."""
    on_block = workflow.get("on", {})
    wr = on_block.get("workflow_run", {})
    types = wr.get("types", [])
    assert "completed" in types, (
        f"workflow_run trigger must include 'completed' in types; got: {types}"
    )


def test_workflow_run_lists_key_workflows(workflow: dict) -> None:
    """workflow_run trigger must list at least the key workflows."""
    on_block = workflow.get("on", {})
    wr = on_block.get("workflow_run", {})
    listed = set(wr.get("workflows", []))
    missing = EXPECTED_WORKFLOWS - listed
    assert not missing, (
        f"workflow_run trigger must list workflows {EXPECTED_WORKFLOWS}; "
        f"missing: {missing}. Found: {listed}"
    )


# ---------------------------------------------------------------------------
# Gate 4: References .if-fork/prompts/failure-analysis.md
# ---------------------------------------------------------------------------
def test_references_failure_analysis_prompt(workflow: dict) -> None:
    """The analyze job must reference .if-fork/prompts/failure-analysis.md."""
    jobs = workflow.get("jobs", {})
    analyze_job = jobs.get("analyze", {})
    steps = analyze_job.get("steps", [])

    # Collect all step content as a single blob to search
    step_text = str(steps)

    assert "failure-analysis.md" in step_text, (
        "analyze job must reference .if-fork/prompts/failure-analysis.md "
        f"in one of its steps; steps: {step_text}"
    )


# ---------------------------------------------------------------------------
# Gate 5: Ledger event failure_analyzed is emitted
# ---------------------------------------------------------------------------
def test_ledger_event_failure_analyzed_emitted(workflow: dict) -> None:
    """The record job must emit a 'failure_analyzed' ledger event."""
    jobs = workflow.get("jobs", {})
    record_job = jobs.get("record", {})
    steps = record_job.get("steps", [])

    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )

    assert "failure_analyzed" in combined_run, (
        "record job must emit ledger event 'failure_analyzed' via ledger-event.py; "
        f"run scripts: {combined_run!r}"
    )

    assert "ledger-event.py" in combined_run, (
        "record job must call tools/ledger-event.py to emit the ledger event; "
        f"run scripts: {combined_run!r}"
    )


# ---------------------------------------------------------------------------
# Gate 6: Concurrency group uses workflow_run event id
# ---------------------------------------------------------------------------
def test_concurrency_group_uses_run_id(workflow: dict) -> None:
    """
    Concurrency group must include the workflow_run event id to ensure
    only one analysis per failed run.
    """
    concurrency = workflow.get("concurrency")
    assert concurrency is not None, "Missing top-level 'concurrency' block"
    assert isinstance(concurrency, dict), "'concurrency' must be a mapping"

    group = concurrency.get("group", "")
    assert group, "concurrency.group must be non-empty"
    assert "workflow_run.id" in group, (
        f"concurrency.group must use workflow_run.id to ensure one-per-run; got: {group!r}"
    )


def test_concurrency_cancel_in_progress_false(workflow: dict) -> None:
    """concurrency.cancel-in-progress must be false (never cancel an in-progress analysis)."""
    concurrency = workflow.get("concurrency", {})
    assert concurrency.get("cancel-in-progress") is False, (
        "concurrency.cancel-in-progress must be false for failure analysis workflow; "
        f"got: {concurrency.get('cancel-in-progress')}"
    )


# ---------------------------------------------------------------------------
# Gate 7: Permissions block
# ---------------------------------------------------------------------------
def test_permissions_actions_read(workflow: dict) -> None:
    """Workflow must grant actions:read permission."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("actions") == "read", (
        f"permissions.actions must be 'read'; got: {permissions.get('actions')}"
    )


def test_permissions_contents_read(workflow: dict) -> None:
    """Workflow must grant contents:read permission."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("contents") == "read", (
        f"permissions.contents must be 'read'; got: {permissions.get('contents')}"
    )


def test_permissions_issues_write(workflow: dict) -> None:
    """Workflow must grant issues:write permission."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("issues") == "write", (
        f"permissions.issues must be 'write'; got: {permissions.get('issues')}"
    )


def test_permissions_id_token_write(workflow: dict) -> None:
    """Workflow must grant id-token:write permission."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("id-token") == "write", (
        f"permissions.id-token must be 'write'; got: {permissions.get('id-token')}"
    )


# ---------------------------------------------------------------------------
# Gate 8: gate job uses autonomy-check with claude-invoke
# ---------------------------------------------------------------------------
def test_gate_job_calls_autonomy_check(workflow: dict) -> None:
    """gate job must use the reusable autonomy-check.yml workflow."""
    jobs = workflow.get("jobs", {})
    gate_job = jobs.get("gate", {})
    uses = gate_job.get("uses", "")
    assert "autonomy-check.yml" in uses, (
        f"gate job must use ./.github/workflows/autonomy-check.yml; got: {uses!r}"
    )


def test_gate_job_requests_claude_invoke(workflow: dict) -> None:
    """gate job must pass requested_action: claude-invoke."""
    jobs = workflow.get("jobs", {})
    gate_job = jobs.get("gate", {})
    with_block = gate_job.get("with", {})
    requested_action = with_block.get("requested_action", "")
    assert requested_action == "claude-invoke", (
        f"gate job must pass requested_action: claude-invoke; got: {requested_action!r}"
    )


# ---------------------------------------------------------------------------
# Gate 9: analyze job needs gate and checks gate output
# ---------------------------------------------------------------------------
def test_analyze_needs_gate(workflow: dict) -> None:
    """analyze job must declare needs: gate."""
    jobs = workflow.get("jobs", {})
    analyze_job = jobs.get("analyze", {})
    needs = analyze_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "gate" in needs, (
        f"analyze job must declare needs: [gate]; got: {needs}"
    )


def test_analyze_checks_gate_proceed_output(workflow: dict) -> None:
    """analyze job must check needs.gate.outputs.proceed == 'true'."""
    jobs = workflow.get("jobs", {})
    analyze_job = jobs.get("analyze", {})
    job_if = str(analyze_job.get("if", ""))
    assert "gate" in job_if and "proceed" in job_if, (
        f"analyze job 'if:' must check needs.gate.outputs.proceed; got: {job_if!r}"
    )


# ---------------------------------------------------------------------------
# Gate 10: record job needs gate and analyze; always() guard for resilience
# ---------------------------------------------------------------------------
def test_record_needs_gate_and_analyze(workflow: dict) -> None:
    """record job must declare needs: [gate, analyze]."""
    jobs = workflow.get("jobs", {})
    record_job = jobs.get("record", {})
    needs = record_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "gate" in needs, f"record job must need 'gate'; got: {needs}"
    assert "analyze" in needs, f"record job must need 'analyze'; got: {needs}"


def test_record_has_always_condition(workflow: dict) -> None:
    """record job must have an always() condition to run even if analyze partially fails."""
    jobs = workflow.get("jobs", {})
    record_job = jobs.get("record", {})
    job_if = str(record_job.get("if", ""))
    assert "always()" in job_if, (
        f"record job 'if:' must include always() for resilience; got: {job_if!r}"
    )


# ---------------------------------------------------------------------------
# Bonus: actionlint (skipped gracefully if not installed)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint not available; skipping lint gate",
)
def test_actionlint_claude_on_failure_yml() -> None:
    """actionlint must pass on claude-on-failure.yml when available."""
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint failed on claude-on-failure.yml:\n{result.stdout}\n{result.stderr}"
    )
