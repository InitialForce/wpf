"""
Tests for .github/workflows/build.yml

Validates structural requirements of the PR validation matrix workflow:
- YAML parses cleanly
- All 6 required jobs are present
- Matrix: windows-latest runner + x64/arm64 architectures
- Triggers: pull_request, push, workflow_dispatch
- Python lint job calls ruff, mypy, pytest
- Concurrency group is set
- Permissions block contains required entries
- arm64 conditional gate present
- needs relationships are correct
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build.yml"

REQUIRED_JOBS = {"lint-tools", "lint-yaml", "build-wpf", "smoke", "perf", "aggregate"}


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
    """Parse and return the build.yml document."""
    assert WORKFLOW_PATH.exists(), f"Workflow file not found: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


# ---------------------------------------------------------------------------
# 1. YAML parses without error
# ---------------------------------------------------------------------------
def test_yaml_parses() -> None:
    """build.yml must be valid YAML."""
    assert WORKFLOW_PATH.exists(), f"Missing: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert doc is not None, "YAML parsed to None (empty file?)"
    assert isinstance(doc, dict), "Top-level YAML document must be a mapping"


# ---------------------------------------------------------------------------
# 2. All 6 required jobs are present
# ---------------------------------------------------------------------------
def test_all_six_jobs_present(workflow: dict) -> None:
    """Workflow must define exactly the 6 required jobs."""
    jobs = workflow.get("jobs", {})
    missing = REQUIRED_JOBS - set(jobs.keys())
    assert not missing, f"Missing required jobs: {missing}"


# ---------------------------------------------------------------------------
# 3. Matrix: windows-latest runner + x64/arm64
# ---------------------------------------------------------------------------
def test_build_wpf_runs_on_windows(workflow: dict) -> None:
    """build-wpf job must target windows-latest."""
    jobs = workflow["jobs"]
    assert "build-wpf" in jobs, "build-wpf job not found"
    build_job = jobs["build-wpf"]
    assert build_job.get("runs-on") == "windows-latest", (
        f"build-wpf must run on windows-latest, got: {build_job.get('runs-on')}"
    )


def test_matrix_arch_includes_x64_and_arm64(workflow: dict) -> None:
    """build-wpf matrix must include both x64 and arm64."""
    jobs = workflow["jobs"]
    build_job = jobs["build-wpf"]
    strategy = build_job.get("strategy", {})
    matrix = strategy.get("matrix", {})
    arch_list = matrix.get("arch", [])

    assert "x64" in arch_list, f"Matrix must include x64; got: {arch_list}"
    assert "arm64" in arch_list, f"Matrix must include arm64; got: {arch_list}"


# ---------------------------------------------------------------------------
# 4. Triggers: pull_request, push, workflow_dispatch
# ---------------------------------------------------------------------------
def test_trigger_pull_request(workflow: dict) -> None:
    """Workflow must be triggered on pull_request events."""
    on_block = workflow.get("on", {})
    assert "pull_request" in on_block, (
        "Missing 'pull_request' trigger in 'on' block"
    )


def test_trigger_push(workflow: dict) -> None:
    """Workflow must be triggered on push events."""
    on_block = workflow.get("on", {})
    assert "push" in on_block, "Missing 'push' trigger in 'on' block"


def test_trigger_workflow_dispatch(workflow: dict) -> None:
    """Workflow must be triggered on workflow_dispatch."""
    on_block = workflow.get("on", {})
    assert "workflow_dispatch" in on_block, (
        "Missing 'workflow_dispatch' trigger in 'on' block"
    )


def test_pull_request_targets_if_branches(workflow: dict) -> None:
    """pull_request trigger must target if/main and/or if/staging."""
    on_block = workflow.get("on", {})
    pr_block = on_block.get("pull_request", {}) or {}
    branches = pr_block.get("branches", []) if isinstance(pr_block, dict) else []

    if_branches = [b for b in branches if b.startswith("if/")]
    assert if_branches, (
        f"pull_request trigger must target at least one if/* branch; got branches: {branches}"
    )


# ---------------------------------------------------------------------------
# 5. Python lint job calls ruff, mypy, pytest
# ---------------------------------------------------------------------------
def test_lint_tools_calls_ruff(workflow: dict) -> None:
    """lint-tools job must invoke ruff."""
    jobs = workflow["jobs"]
    lint_job = jobs.get("lint-tools", {})
    steps = lint_job.get("steps", [])
    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    assert "ruff" in combined_run, (
        "lint-tools job must call ruff; not found in any step's 'run' script"
    )


def test_lint_tools_calls_mypy(workflow: dict) -> None:
    """lint-tools job must invoke mypy."""
    jobs = workflow["jobs"]
    lint_job = jobs.get("lint-tools", {})
    steps = lint_job.get("steps", [])
    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    assert "mypy" in combined_run, (
        "lint-tools job must call mypy; not found in any step's 'run' script"
    )


def test_lint_tools_calls_pytest(workflow: dict) -> None:
    """lint-tools job must invoke pytest."""
    jobs = workflow["jobs"]
    lint_job = jobs.get("lint-tools", {})
    steps = lint_job.get("steps", [])
    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    assert "pytest" in combined_run, (
        "lint-tools job must call pytest; not found in any step's 'run' script"
    )


def test_lint_tools_runs_on_ubuntu(workflow: dict) -> None:
    """lint-tools job must run on ubuntu-latest."""
    jobs = workflow["jobs"]
    lint_job = jobs.get("lint-tools", {})
    assert lint_job.get("runs-on") == "ubuntu-latest", (
        f"lint-tools must run on ubuntu-latest, got: {lint_job.get('runs-on')}"
    )


# ---------------------------------------------------------------------------
# 6. Concurrency group is set
# ---------------------------------------------------------------------------
def test_concurrency_group_set(workflow: dict) -> None:
    """Workflow must define a concurrency group."""
    concurrency = workflow.get("concurrency")
    assert concurrency is not None, "Missing top-level 'concurrency' block"
    assert isinstance(concurrency, dict), "'concurrency' must be a mapping"

    group = concurrency.get("group", "")
    assert group, "concurrency.group must be non-empty"
    assert "github.ref" in group, (
        f"concurrency.group should reference github.ref for per-ref isolation; got: {group}"
    )


def test_concurrency_cancel_in_progress(workflow: dict) -> None:
    """concurrency.cancel-in-progress must be true."""
    concurrency = workflow.get("concurrency", {})
    assert concurrency.get("cancel-in-progress") is True, (
        "concurrency.cancel-in-progress must be true"
    )


# ---------------------------------------------------------------------------
# 7. Permissions block
# ---------------------------------------------------------------------------
def test_permissions_checks_write(workflow: dict) -> None:
    """Workflow must grant checks:write permission."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("checks") == "write", (
        f"permissions.checks must be 'write'; got: {permissions.get('checks')}"
    )


def test_permissions_contents_read(workflow: dict) -> None:
    """Workflow must grant contents:read permission."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("contents") == "read", (
        f"permissions.contents must be 'read'; got: {permissions.get('contents')}"
    )


# ---------------------------------------------------------------------------
# 8. arm64 conditional gate
# ---------------------------------------------------------------------------
def test_build_wpf_arm64_gate_present(workflow: dict) -> None:
    """build-wpf job must have an arm64 conditional guard."""
    jobs = workflow["jobs"]
    build_job = jobs.get("build-wpf", {})
    job_if = build_job.get("if", "")
    assert job_if, "build-wpf job must have an 'if' condition for arm64 gating"
    assert "arm64" in str(job_if), (
        f"build-wpf 'if' condition must reference arm64; got: {job_if}"
    )


# ---------------------------------------------------------------------------
# 9. needs relationships
# ---------------------------------------------------------------------------
def test_smoke_needs_build_wpf(workflow: dict) -> None:
    """smoke job must declare needs: build-wpf."""
    jobs = workflow["jobs"]
    smoke_job = jobs.get("smoke", {})
    needs = smoke_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "build-wpf" in needs, (
        f"smoke job must need 'build-wpf'; got needs: {needs}"
    )


def test_perf_needs_build_wpf(workflow: dict) -> None:
    """perf job must declare needs: build-wpf."""
    jobs = workflow["jobs"]
    perf_job = jobs.get("perf", {})
    needs = perf_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "build-wpf" in needs, (
        f"perf job must need 'build-wpf'; got needs: {needs}"
    )


def test_aggregate_needs_all_prior_jobs(workflow: dict) -> None:
    """aggregate job must depend on lint-tools, lint-yaml, smoke, and perf."""
    jobs = workflow["jobs"]
    agg_job = jobs.get("aggregate", {})
    needs = agg_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]

    for required_dep in ("lint-tools", "lint-yaml", "smoke", "perf"):
        assert required_dep in needs, (
            f"aggregate must need '{required_dep}'; got needs: {needs}"
        )


# ---------------------------------------------------------------------------
# 10. smoke artifact upload uses matrix arch in name
# ---------------------------------------------------------------------------
def test_smoke_artifact_name_includes_arch(workflow: dict) -> None:
    """smoke job must upload artifacts named smoke-<arch>.xml (matrix.arch in name)."""
    jobs = workflow["jobs"]
    smoke_job = jobs.get("smoke", {})
    steps = smoke_job.get("steps", [])

    upload_steps = [
        s for s in steps
        if isinstance(s, dict) and "upload-artifact" in str(s.get("uses", ""))
    ]
    assert upload_steps, "smoke job must have at least one actions/upload-artifact step"

    # At least one upload step should reference matrix.arch in its artifact name
    artifact_names = [
        s.get("with", {}).get("name", "") for s in upload_steps
    ]
    arch_in_names = any("matrix.arch" in name or "arch" in name for name in artifact_names)
    assert arch_in_names, (
        "smoke artifact name must include matrix arch for per-arch upload; "
        f"got names: {artifact_names}"
    )


# ---------------------------------------------------------------------------
# 11. lint-yaml job validates workflow YAML
# ---------------------------------------------------------------------------
def test_lint_yaml_job_exists(workflow: dict) -> None:
    """lint-yaml job must exist and run on ubuntu-latest."""
    jobs = workflow["jobs"]
    assert "lint-yaml" in jobs, "lint-yaml job not found"
    lint_yaml_job = jobs["lint-yaml"]
    assert lint_yaml_job.get("runs-on") == "ubuntu-latest", (
        f"lint-yaml must run on ubuntu-latest, got: {lint_yaml_job.get('runs-on')}"
    )


def test_lint_yaml_validates_workflow_files(workflow: dict) -> None:
    """lint-yaml job must reference actionlint or YAML validation in its steps."""
    jobs = workflow["jobs"]
    lint_yaml_job = jobs.get("lint-yaml", {})
    steps = lint_yaml_job.get("steps", [])
    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    assert "actionlint" in combined_run or "yaml" in combined_run.lower(), (
        "lint-yaml job must reference actionlint or YAML validation in its steps"
    )


# ---------------------------------------------------------------------------
# 12. actionlint (skipped gracefully if not installed)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint not available; skipping lint gate",
)
def test_actionlint_build_yml() -> None:
    """actionlint must pass on build.yml when available."""
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint failed on build.yml:\n{result.stdout}\n{result.stderr}"
    )
