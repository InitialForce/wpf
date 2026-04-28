"""
Tests for .github/workflows/weekly-differential.yml

Validates structural requirements of the weekly upstream-vs-fork differential
workflow without executing it. Actionlint integration when available.

Structural assertions:
1.  YAML parses cleanly
2.  All 5 required jobs are present
3.  Cron schedule is '23 5 * * 1' (Monday 05:23 UTC)
4.  workflow_dispatch trigger is present
5.  needs chain: gate→build-*; compare needs both builds; report needs compare
6.  gate job uses the reusable autonomy-check.yml workflow
7.  build-upstream-clean and build-fork each need 'gate'
8.  compare needs both 'build-upstream-clean' and 'build-fork'
9.  report needs 'compare'
10. tools/diff-smoke-results.py is referenced in compare job
11. permissions: contents:read, issues:write (id-token:write removed — no OIDC)
12. concurrency group references 'weekly-diff'
13. smoke-upstream.xml artifact is uploaded by build-upstream-clean
14. smoke-fork.xml artifact is uploaded by build-fork
15. compare job handles exit codes 1 and 2 from diff-smoke-results.py
16. report job has always() condition so it runs regardless of compare outcome
17. upstream checkout targets upstream/release/10.0 (no fork patches)
18. fork checkout targets if/main
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "weekly-differential.yml"
DIFF_TOOL_PATH = REPO_ROOT / "tools" / "diff-smoke-results.py"

EXPECTED_JOBS = {"gate", "build-upstream-clean", "build-fork", "compare", "report"}
EXPECTED_CRON = "23 5 * * 1"


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
    """Parse and return the weekly-differential.yml document."""
    assert WORKFLOW_PATH.exists(), f"Workflow file not found: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


def _get_needs_list(job: dict) -> list[str]:
    """Normalise 'needs' to a list regardless of whether it's a str or list."""
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    return list(needs) if needs else []


# ---------------------------------------------------------------------------
# 1. YAML parses cleanly
# ---------------------------------------------------------------------------
def test_yaml_parses() -> None:
    """weekly-differential.yml must be valid YAML."""
    assert WORKFLOW_PATH.exists(), f"Missing workflow: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert doc is not None, "YAML parsed to None (empty file?)"
    assert isinstance(doc, dict), f"Expected top-level dict, got {type(doc)}"


# ---------------------------------------------------------------------------
# 2. All 5 required jobs are present
# ---------------------------------------------------------------------------
def test_all_five_jobs_present(workflow: dict) -> None:
    """Workflow must define exactly the 5 required jobs."""
    jobs = workflow.get("jobs", {})
    missing = EXPECTED_JOBS - set(jobs.keys())
    assert not missing, (
        f"Missing required jobs: {missing}. "
        f"Found jobs: {set(jobs.keys())}"
    )


# ---------------------------------------------------------------------------
# 3. Cron schedule is '23 5 * * 1'
# ---------------------------------------------------------------------------
def test_cron_schedule(workflow: dict) -> None:
    """Workflow must have cron schedule '23 5 * * 1' (Monday 05:23 UTC)."""
    on_block = workflow.get("on", {})
    schedules = on_block.get("schedule", [])
    crons = [s["cron"] for s in schedules if isinstance(s, dict) and "cron" in s]
    assert EXPECTED_CRON in crons, (
        f"Expected cron '{EXPECTED_CRON}', got: {crons}"
    )


# ---------------------------------------------------------------------------
# 4. workflow_dispatch trigger is present
# ---------------------------------------------------------------------------
def test_workflow_dispatch_trigger(workflow: dict) -> None:
    """Workflow must support manual workflow_dispatch trigger."""
    on_block = workflow.get("on", {})
    assert "workflow_dispatch" in on_block, (
        "Missing 'workflow_dispatch' trigger in 'on' block"
    )


# ---------------------------------------------------------------------------
# 5. needs chain correctness (comprehensive)
# ---------------------------------------------------------------------------
def test_needs_chain(workflow: dict) -> None:
    """Validate the complete needs chain across all 5 jobs."""
    jobs = workflow["jobs"]

    # gate has no needs (it IS the gate)
    gate_needs = _get_needs_list(jobs["gate"])
    assert not gate_needs, f"'gate' should have no needs, got: {gate_needs}"

    # build-upstream-clean needs gate
    buc_needs = _get_needs_list(jobs["build-upstream-clean"])
    assert "gate" in buc_needs, (
        f"'build-upstream-clean' must need 'gate', got: {buc_needs}"
    )

    # build-fork needs gate
    bf_needs = _get_needs_list(jobs["build-fork"])
    assert "gate" in bf_needs, (
        f"'build-fork' must need 'gate', got: {bf_needs}"
    )

    # compare needs both build jobs
    cmp_needs = _get_needs_list(jobs["compare"])
    assert "build-upstream-clean" in cmp_needs, (
        f"'compare' must need 'build-upstream-clean', got: {cmp_needs}"
    )
    assert "build-fork" in cmp_needs, (
        f"'compare' must need 'build-fork', got: {cmp_needs}"
    )

    # report needs compare
    rep_needs = _get_needs_list(jobs["report"])
    assert "compare" in rep_needs, (
        f"'report' must need 'compare', got: {rep_needs}"
    )


# ---------------------------------------------------------------------------
# 6. gate job uses the reusable autonomy-check.yml workflow
# ---------------------------------------------------------------------------
def test_gate_uses_autonomy_check(workflow: dict) -> None:
    """gate job must call the reusable autonomy-check.yml via 'uses:'."""
    jobs = workflow["jobs"]
    gate_job = jobs.get("gate", {})
    uses = gate_job.get("uses", "")
    assert "autonomy-check.yml" in uses, (
        f"gate job must use autonomy-check.yml; got 'uses': {uses!r}"
    )


# ---------------------------------------------------------------------------
# 7. build-upstream-clean and build-fork each need 'gate'
# (subset of test_needs_chain — explicit gate assertions)
# ---------------------------------------------------------------------------
def test_build_jobs_need_gate(workflow: dict) -> None:
    """Both build jobs must declare needs: gate."""
    jobs = workflow["jobs"]
    for job_name in ("build-upstream-clean", "build-fork"):
        needs = _get_needs_list(jobs[job_name])
        assert "gate" in needs, (
            f"'{job_name}' must need 'gate'; got needs: {needs}"
        )


# ---------------------------------------------------------------------------
# 8. compare needs both build jobs
# ---------------------------------------------------------------------------
def test_compare_needs_both_builds(workflow: dict) -> None:
    """compare job must need both build-upstream-clean and build-fork."""
    jobs = workflow["jobs"]
    cmp_needs = _get_needs_list(jobs["compare"])
    assert "build-upstream-clean" in cmp_needs and "build-fork" in cmp_needs, (
        f"compare must need both build jobs; got: {cmp_needs}"
    )


# ---------------------------------------------------------------------------
# 9. report needs compare
# ---------------------------------------------------------------------------
def test_report_needs_compare(workflow: dict) -> None:
    """report job must declare needs: compare."""
    jobs = workflow["jobs"]
    rep_needs = _get_needs_list(jobs["report"])
    assert "compare" in rep_needs, (
        f"'report' must need 'compare'; got: {rep_needs}"
    )


# ---------------------------------------------------------------------------
# 10. tools/diff-smoke-results.py is referenced in compare job
# ---------------------------------------------------------------------------
def test_diff_tool_referenced_in_compare(workflow: dict) -> None:
    """compare job must reference tools/diff-smoke-results.py in a run step."""
    jobs = workflow["jobs"]
    compare_job = jobs.get("compare", {})
    steps = compare_job.get("steps", [])
    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    assert "diff-smoke-results.py" in combined_run, (
        "compare job must call tools/diff-smoke-results.py in a run step; "
        "not found in any step's 'run' script"
    )


def test_diff_tool_exists() -> None:
    """tools/diff-smoke-results.py must exist in the repository."""
    assert DIFF_TOOL_PATH.exists(), (
        f"tools/diff-smoke-results.py not found at {DIFF_TOOL_PATH}"
    )


# ---------------------------------------------------------------------------
# 11. Permissions: contents:read, issues:write (id-token:write removed)
# ---------------------------------------------------------------------------
def test_permissions_contents_read(workflow: dict) -> None:
    """Workflow must grant contents:read."""
    perms = workflow.get("permissions", {})
    assert perms.get("contents") == "read", (
        f"permissions.contents must be 'read'; got: {perms.get('contents')}"
    )


def test_permissions_issues_write(workflow: dict) -> None:
    """Workflow must grant issues:write."""
    perms = workflow.get("permissions", {})
    assert perms.get("issues") == "write", (
        f"permissions.issues must be 'write'; got: {perms.get('issues')}"
    )


def test_permissions_no_id_token_write(workflow: dict) -> None:
    """Workflow must NOT grant id-token:write — no OIDC usage in weekly-differential."""
    perms = workflow.get("permissions", {})
    assert perms.get("id-token") != "write", (
        "permissions.id-token must NOT be 'write' in weekly-differential — no OIDC usage; "
        f"got: {perms.get('id-token')}"
    )


# ---------------------------------------------------------------------------
# 12. Concurrency group references 'weekly-diff'
# ---------------------------------------------------------------------------
def test_concurrency_group(workflow: dict) -> None:
    """concurrency.group must reference 'weekly-diff'."""
    concurrency = workflow.get("concurrency", {})
    assert concurrency, "Missing top-level 'concurrency' block"
    group = str(concurrency.get("group", ""))
    assert "weekly-diff" in group, (
        f"concurrency.group must reference 'weekly-diff'; got: {group!r}"
    )


# ---------------------------------------------------------------------------
# 13. smoke-upstream.xml artifact uploaded by build-upstream-clean
# ---------------------------------------------------------------------------
def test_upstream_artifact_uploaded(workflow: dict) -> None:
    """build-upstream-clean must upload an artifact named 'smoke-upstream'."""
    jobs = workflow["jobs"]
    buc_job = jobs.get("build-upstream-clean", {})
    steps = buc_job.get("steps", [])
    upload_steps = [
        s for s in steps
        if isinstance(s, dict) and "upload-artifact" in str(s.get("uses", ""))
    ]
    assert upload_steps, "build-upstream-clean must have at least one upload-artifact step"
    artifact_names = [s.get("with", {}).get("name", "") for s in upload_steps]
    assert any("smoke-upstream" in name for name in artifact_names), (
        f"Expected 'smoke-upstream' artifact; got names: {artifact_names}"
    )


# ---------------------------------------------------------------------------
# 14. smoke-fork.xml artifact uploaded by build-fork
# ---------------------------------------------------------------------------
def test_fork_artifact_uploaded(workflow: dict) -> None:
    """build-fork must upload an artifact named 'smoke-fork'."""
    jobs = workflow["jobs"]
    bf_job = jobs.get("build-fork", {})
    steps = bf_job.get("steps", [])
    upload_steps = [
        s for s in steps
        if isinstance(s, dict) and "upload-artifact" in str(s.get("uses", ""))
    ]
    assert upload_steps, "build-fork must have at least one upload-artifact step"
    artifact_names = [s.get("with", {}).get("name", "") for s in upload_steps]
    assert any("smoke-fork" in name for name in artifact_names), (
        f"Expected 'smoke-fork' artifact; got names: {artifact_names}"
    )


# ---------------------------------------------------------------------------
# 15. compare job handles exit codes 1 and 2 from diff-smoke-results.py
# ---------------------------------------------------------------------------
def test_compare_handles_regression_exit(workflow: dict) -> None:
    """compare must have a step that handles diff exit code 2 (regression)."""
    jobs = workflow["jobs"]
    compare_job = jobs.get("compare", {})
    steps = compare_job.get("steps", [])

    # Look for a step conditional on exit code 2
    regression_steps = [
        s for s in steps
        if isinstance(s, dict)
        and (
            "'2'" in str(s.get("if", ""))
            or '"2"' in str(s.get("if", ""))
            or "== '2'" in str(s.get("if", ""))
            or "regression" in str(s.get("name", "")).lower()
        )
    ]
    assert regression_steps, (
        "compare job must have a step handling exit code 2 (regression). "
        f"Steps: {[s.get('name') for s in steps]}"
    )


def test_compare_handles_drift_exit(workflow: dict) -> None:
    """compare must have a step that handles diff exit code 1 (drift)."""
    jobs = workflow["jobs"]
    compare_job = jobs.get("compare", {})
    steps = compare_job.get("steps", [])

    drift_steps = [
        s for s in steps
        if isinstance(s, dict)
        and (
            "'1'" in str(s.get("if", ""))
            or '"1"' in str(s.get("if", ""))
            or "== '1'" in str(s.get("if", ""))
            or "drift" in str(s.get("name", "")).lower()
        )
    ]
    assert drift_steps, (
        "compare job must have a step handling exit code 1 (drift). "
        f"Steps: {[s.get('name') for s in steps]}"
    )


# ---------------------------------------------------------------------------
# 16. report job has always() / unconditional-ish 'if' so it runs regardless
# ---------------------------------------------------------------------------
def test_report_runs_always(workflow: dict) -> None:
    """report job must have an 'if' condition with always() to run even on compare failure."""
    jobs = workflow["jobs"]
    report_job = jobs.get("report", {})
    condition = str(report_job.get("if", ""))
    assert "always()" in condition, (
        f"report job 'if' must contain always(); got: {condition!r}"
    )


# ---------------------------------------------------------------------------
# 17. upstream checkout targets upstream/release/10.0
# ---------------------------------------------------------------------------
def test_upstream_build_checkouts_upstream_clean(workflow: dict) -> None:
    """build-upstream-clean must checkout upstream/release/10.0 (no fork patches)."""
    jobs = workflow["jobs"]
    buc_job = jobs.get("build-upstream-clean", {})
    steps = buc_job.get("steps", [])

    combined = "\n".join(
        str(step.get("run", "")) + str(step.get("with", {}))
        for step in steps
        if isinstance(step, dict)
    )
    assert "upstream/release/10.0" in combined or "release/10.0" in combined, (
        "build-upstream-clean must reference upstream/release/10.0 in its steps. "
        f"Combined step content (truncated): {combined[:400]}"
    )


# ---------------------------------------------------------------------------
# 18. fork checkout targets if/main
# ---------------------------------------------------------------------------
def test_fork_build_checkouts_if_main(workflow: dict) -> None:
    """build-fork must checkout if/main."""
    jobs = workflow["jobs"]
    bf_job = jobs.get("build-fork", {})
    steps = bf_job.get("steps", [])

    combined = "\n".join(
        str(step.get("run", "")) + str(step.get("with", {}))
        for step in steps
        if isinstance(step, dict)
    )
    assert "if/main" in combined, (
        "build-fork must checkout 'if/main'. "
        f"Combined step content (truncated): {combined[:400]}"
    )


# ---------------------------------------------------------------------------
# Bonus: actionlint (skipped gracefully if not installed)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint not available; skipping",
)
def test_actionlint_weekly_differential() -> None:
    """actionlint must pass on weekly-differential.yml when available."""
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint failed on weekly-differential.yml:\n{result.stdout}\n{result.stderr}"
    )
