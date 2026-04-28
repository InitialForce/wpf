"""
Tests for .github/workflows/nightly-rebase.yml

Validates YAML structure, job dependencies, gate conditions, and prompt files
without executing the workflow. Actionlint integration when available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "nightly-rebase.yml"
REBASE_PROMPT_PATH = REPO_ROOT / ".if-fork" / "prompts" / "rebase.md"
CONFLICT_PROMPT_PATH = REPO_ROOT / ".if-fork" / "prompts" / "resolve-rebase-conflict.md"

EXPECTED_JOBS = {"gate", "fetch-upstream", "attempt-rebase", "verify", "promote"}
GATE_CONDITION = "needs.gate.outputs.proceed == 'true'"


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
    """Parse and return the nightly-rebase.yml document."""
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


# ---------------------------------------------------------------------------
# Gate 1: YAML parses
# ---------------------------------------------------------------------------
def test_yaml_parses() -> None:
    """The workflow YAML must parse without errors."""
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert doc is not None, "YAML parsed to None — file is empty or invalid"
    assert isinstance(doc, dict), f"Expected dict, got {type(doc)}"


# ---------------------------------------------------------------------------
# Gate 2: All 5 jobs exist
# ---------------------------------------------------------------------------
def test_all_five_jobs_exist(workflow: dict) -> None:
    """The workflow must define exactly the 5 required jobs."""
    jobs = workflow.get("jobs", {})
    missing = EXPECTED_JOBS - set(jobs.keys())
    assert not missing, f"Missing jobs: {missing}. Found: {set(jobs.keys())}"


# ---------------------------------------------------------------------------
# Gate 3: Correct needs chain
# ---------------------------------------------------------------------------
def test_needs_chain(workflow: dict) -> None:
    """Each job must declare the correct needs: dependencies."""
    jobs = workflow["jobs"]

    # gate has no needs (it IS the gate)
    gate_needs = jobs["gate"].get("needs", [])
    assert not gate_needs, f"'gate' should have no needs, got: {gate_needs}"

    # fetch-upstream needs gate
    fetch_needs = jobs["fetch-upstream"].get("needs", [])
    if isinstance(fetch_needs, str):
        fetch_needs = [fetch_needs]
    assert "gate" in fetch_needs, (
        f"'fetch-upstream' must need 'gate', got needs: {fetch_needs}"
    )

    # attempt-rebase needs gate and fetch-upstream
    rebase_needs = jobs["attempt-rebase"].get("needs", [])
    if isinstance(rebase_needs, str):
        rebase_needs = [rebase_needs]
    assert "gate" in rebase_needs, (
        f"'attempt-rebase' must need 'gate', got needs: {rebase_needs}"
    )
    assert "fetch-upstream" in rebase_needs, (
        f"'attempt-rebase' must need 'fetch-upstream', got needs: {rebase_needs}"
    )

    # verify needs gate and attempt-rebase
    verify_needs = jobs["verify"].get("needs", [])
    if isinstance(verify_needs, str):
        verify_needs = [verify_needs]
    assert "gate" in verify_needs, (
        f"'verify' must need 'gate', got needs: {verify_needs}"
    )
    assert "attempt-rebase" in verify_needs, (
        f"'verify' must need 'attempt-rebase', got needs: {verify_needs}"
    )

    # promote needs gate, attempt-rebase, and verify
    promote_needs = jobs["promote"].get("needs", [])
    if isinstance(promote_needs, str):
        promote_needs = [promote_needs]
    assert "gate" in promote_needs, (
        f"'promote' must need 'gate', got needs: {promote_needs}"
    )
    assert "verify" in promote_needs, (
        f"'promote' must need 'verify', got needs: {promote_needs}"
    )


# ---------------------------------------------------------------------------
# Gate 4: Gate condition on all non-gate jobs
# ---------------------------------------------------------------------------
def test_gate_condition_on_all_claude_jobs(workflow: dict) -> None:
    """All jobs except 'gate' must have an 'if' condition referencing needs.gate.outputs.proceed."""
    jobs = workflow["jobs"]
    jobs_requiring_gate = EXPECTED_JOBS - {"gate"}

    for job_name in jobs_requiring_gate:
        job = jobs[job_name]
        condition = job.get("if", "")
        assert GATE_CONDITION in str(condition), (
            f"Job '{job_name}' missing gate condition '{GATE_CONDITION}'. "
            f"Got 'if': {condition!r}"
        )


# ---------------------------------------------------------------------------
# Gate 5: Schedule trigger is correct
# ---------------------------------------------------------------------------
def test_schedule_trigger(workflow: dict) -> None:
    """Workflow must have cron schedule '7 3 * * *' and workflow_dispatch."""
    on_block = workflow.get("on", {})
    assert "schedule" in on_block, "Missing 'schedule' trigger"
    assert "workflow_dispatch" in on_block, "Missing 'workflow_dispatch' trigger"

    schedules = on_block["schedule"]
    crons = [s["cron"] for s in schedules if "cron" in s]
    assert "7 3 * * *" in crons, (
        f"Expected cron '7 3 * * *', got: {crons}"
    )


# ---------------------------------------------------------------------------
# Gate 6: Permissions declared
# ---------------------------------------------------------------------------
def test_permissions_declared(workflow: dict) -> None:
    """Workflow must declare contents:write, pull-requests:write, issues:write."""
    perms = workflow.get("permissions", {})
    assert perms.get("contents") == "write", (
        f"Expected permissions.contents=write, got: {perms.get('contents')}"
    )
    assert perms.get("pull-requests") == "write", (
        f"Expected permissions.pull-requests=write, got: {perms.get('pull-requests')}"
    )
    assert perms.get("issues") == "write", (
        f"Expected permissions.issues=write, got: {perms.get('issues')}"
    )


# ---------------------------------------------------------------------------
# Gate 7: Rebase prompt file exists and is within size bounds
# ---------------------------------------------------------------------------
def test_rebase_prompt_exists_and_size() -> None:
    """rebase.md must exist and be between 800 and 5000 characters."""
    assert REBASE_PROMPT_PATH.exists(), (
        f"rebase.md not found at {REBASE_PROMPT_PATH}"
    )
    content = REBASE_PROMPT_PATH.read_text(encoding="utf-8")
    size = len(content)
    assert 800 <= size <= 5000, (
        f"rebase.md is {size} chars; expected 800–5000"
    )


# ---------------------------------------------------------------------------
# Gate 8: Conflict-resolution prompt file exists and is within size bounds
# ---------------------------------------------------------------------------
def test_conflict_prompt_exists_and_size() -> None:
    """resolve-rebase-conflict.md must exist and be between 800 and 5000 characters."""
    assert CONFLICT_PROMPT_PATH.exists(), (
        f"resolve-rebase-conflict.md not found at {CONFLICT_PROMPT_PATH}"
    )
    content = CONFLICT_PROMPT_PATH.read_text(encoding="utf-8")
    size = len(content)
    assert 800 <= size <= 5000, (
        f"resolve-rebase-conflict.md is {size} chars; expected 800–5000"
    )


# ---------------------------------------------------------------------------
# Gate 9: Failure handling — attempt-rebase job references issue creation
# ---------------------------------------------------------------------------
def test_failure_handling_in_attempt_rebase(workflow: dict) -> None:
    """attempt-rebase job must have a step that opens an issue on rebase failure."""
    jobs = workflow["jobs"]
    rebase_job = jobs.get("attempt-rebase", {})
    steps = rebase_job.get("steps", [])

    # Look for a step that creates a rebase-failure issue
    issue_steps = [
        s for s in steps
        if isinstance(s, dict)
        and (
            "issue create" in s.get("run", "")
            or "rebase-failure" in s.get("run", "")
            or "Nightly rebase" in s.get("run", "")
        )
    ]
    assert issue_steps, (
        "attempt-rebase job must have a step that opens a GitHub issue on failure. "
        f"Steps found: {[s.get('name') for s in steps]}"
    )


# ---------------------------------------------------------------------------
# Gate 10: attempt-rebase outputs used by verify and promote
# ---------------------------------------------------------------------------
def test_attempt_rebase_outputs_consumed(workflow: dict) -> None:
    """verify and promote jobs must reference attempt-rebase outputs."""
    jobs = workflow["jobs"]

    for consumer_job in ("verify", "promote"):
        job = jobs.get(consumer_job, {})
        job_str = str(job)
        assert "attempt-rebase" in job_str, (
            f"Job '{consumer_job}' must reference attempt-rebase outputs. "
            f"Job content: {job_str[:200]}"
        )


# ---------------------------------------------------------------------------
# Gate 11: fetch-upstream exposes upstream_sha output
# ---------------------------------------------------------------------------
def test_fetch_upstream_sha_output(workflow: dict) -> None:
    """fetch-upstream job must declare an upstream_sha output."""
    jobs = workflow["jobs"]
    fetch_job = jobs.get("fetch-upstream", {})
    outputs = fetch_job.get("outputs", {})
    assert "upstream_sha" in outputs, (
        f"fetch-upstream must declare upstream_sha output. Got: {list(outputs.keys())}"
    )


# ---------------------------------------------------------------------------
# Gate 12: gate job uses the reusable autonomy-check workflow
# ---------------------------------------------------------------------------
def test_gate_job_uses_reusable_workflow(workflow: dict) -> None:
    """gate job must call the reusable autonomy-check.yml workflow."""
    jobs = workflow["jobs"]
    gate_job = jobs.get("gate", {})
    uses = gate_job.get("uses", "")
    assert "autonomy-check.yml" in uses, (
        f"gate job must use autonomy-check.yml via 'uses:'. Got: {uses!r}"
    )


# ---------------------------------------------------------------------------
# Bonus: actionlint (skipped gracefully if not installed)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint not available; skipping lint gate",
)
def test_actionlint_nightly_rebase() -> None:
    """Run actionlint on the workflow file."""
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint failed on nightly-rebase.yml:\n{result.stdout}\n{result.stderr}"
    )
