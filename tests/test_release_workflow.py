"""
Tests for .github/workflows/release.yml

Validates structural requirements of the tag-triggered NuGet publish workflow:
- YAML parses cleanly
- All 8 required jobs are present (gate, verify-tag, build, smoke-on-pack,
  perf-on-pack, release-notes, publish, record)
- needs: chain is correct
- publish job has environment: wpf-nuget-publish (manual approval gate)
- Trigger: push tags if-10.0.* + workflow_dispatch with tag input
- compute-version.ps1 is referenced in the build job
- check-regression.py --current-sha is referenced in the perf job
- ledger-event.py is referenced in the record job
- NuGet target URL contains nuget.pkg.github.com/initialforce
- Concurrency group: release-${{ github.ref }}, cancel-in-progress: false
- Permissions: contents:write, packages:write, id-token:write
- Tag pattern if-10.0.*
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"

REQUIRED_JOBS = {
    "gate",
    "verify-tag",
    "build",
    "smoke-on-pack",
    "perf-on-pack",
    "release-notes",
    "publish",
    "record",
}


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


def _steps_combined_run(job: dict[str, Any]) -> str:
    """Return all 'run' scripts in a job's steps, joined by newline."""
    steps = job.get("steps", [])
    return "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )


def _steps_combined_uses(job: dict[str, Any]) -> str:
    """Return all 'uses' values in a job's steps, joined by newline."""
    steps = job.get("steps", [])
    return "\n".join(
        step.get("uses", "") for step in steps if isinstance(step, dict)
    )


def _job_needs_list(job: dict[str, Any]) -> list[str]:
    """Return the 'needs' list of a job, normalizing str → [str]."""
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    if isinstance(needs, list):
        return needs
    return []


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Parse and return the release.yml document."""
    assert WORKFLOW_PATH.exists(), f"Workflow file not found: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


# ---------------------------------------------------------------------------
# 1. YAML parses without error
# ---------------------------------------------------------------------------
def test_yaml_parses() -> None:
    """release.yml must be valid YAML."""
    assert WORKFLOW_PATH.exists(), f"Missing: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert doc is not None, "YAML parsed to None (empty file?)"
    assert isinstance(doc, dict), "Top-level YAML document must be a mapping"


# ---------------------------------------------------------------------------
# 2. All 8 required jobs are present
# ---------------------------------------------------------------------------
def test_all_eight_jobs_present(workflow: dict) -> None:
    """Workflow must define all 8 required jobs."""
    jobs = workflow.get("jobs", {})
    missing = REQUIRED_JOBS - set(jobs.keys())
    assert not missing, f"Missing required jobs: {missing}"


# ---------------------------------------------------------------------------
# 3. needs: chain is correct
# ---------------------------------------------------------------------------
def test_verify_tag_needs_gate(workflow: dict) -> None:
    """verify-tag must need gate."""
    jobs = workflow["jobs"]
    needs = _job_needs_list(jobs.get("verify-tag", {}))
    assert "gate" in needs, f"verify-tag must need 'gate'; got: {needs}"


def test_build_needs_verify_tag(workflow: dict) -> None:
    """build must need verify-tag."""
    jobs = workflow["jobs"]
    needs = _job_needs_list(jobs.get("build", {}))
    assert "verify-tag" in needs, f"build must need 'verify-tag'; got: {needs}"


def test_smoke_on_pack_needs_build(workflow: dict) -> None:
    """smoke-on-pack must need build."""
    jobs = workflow["jobs"]
    needs = _job_needs_list(jobs.get("smoke-on-pack", {}))
    assert "build" in needs, f"smoke-on-pack must need 'build'; got: {needs}"


def test_perf_on_pack_needs_build(workflow: dict) -> None:
    """perf-on-pack must need build."""
    jobs = workflow["jobs"]
    needs = _job_needs_list(jobs.get("perf-on-pack", {}))
    assert "build" in needs, f"perf-on-pack must need 'build'; got: {needs}"


def test_release_notes_needs_build(workflow: dict) -> None:
    """release-notes must need build."""
    jobs = workflow["jobs"]
    needs = _job_needs_list(jobs.get("release-notes", {}))
    assert "build" in needs, f"release-notes must need 'build'; got: {needs}"


def test_publish_needs_smoke_perf_notes(workflow: dict) -> None:
    """publish must need smoke-on-pack, perf-on-pack, and release-notes."""
    jobs = workflow["jobs"]
    needs = _job_needs_list(jobs.get("publish", {}))
    for required in ("smoke-on-pack", "perf-on-pack", "release-notes"):
        assert required in needs, (
            f"publish must need '{required}'; got needs: {needs}"
        )


def test_record_needs_publish(workflow: dict) -> None:
    """record must need publish."""
    jobs = workflow["jobs"]
    needs = _job_needs_list(jobs.get("record", {}))
    assert "publish" in needs, f"record must need 'publish'; got: {needs}"


# ---------------------------------------------------------------------------
# 4. publish job has environment: wpf-nuget-publish
# ---------------------------------------------------------------------------
def test_publish_job_has_environment_gate(workflow: dict) -> None:
    """publish job must declare environment: wpf-nuget-publish."""
    jobs = workflow["jobs"]
    publish_job = jobs.get("publish", {})
    environment = publish_job.get("environment")
    assert environment == "wpf-nuget-publish", (
        f"publish job must have 'environment: wpf-nuget-publish'; got: {environment}"
    )


# ---------------------------------------------------------------------------
# 5. Trigger: push tags if-10.0.*
# ---------------------------------------------------------------------------
def test_trigger_push_tags_if_10_0(workflow: dict) -> None:
    """Workflow must trigger on push of tags matching if-10.0.*"""
    on_block = workflow.get("on", {})
    push_block = on_block.get("push", {}) or {}
    tags = push_block.get("tags", []) if isinstance(push_block, dict) else []
    assert any("if-10.0." in str(t) for t in tags), (
        f"push trigger must include if-10.0.* tag pattern; got tags: {tags}"
    )


# ---------------------------------------------------------------------------
# 6. Trigger: workflow_dispatch with tag input
# ---------------------------------------------------------------------------
def test_trigger_workflow_dispatch_with_tag_input(workflow: dict) -> None:
    """Workflow must have workflow_dispatch trigger with a 'tag' input."""
    on_block = workflow.get("on", {})
    wd_block = on_block.get("workflow_dispatch", {}) or {}
    assert wd_block is not None, "Missing 'workflow_dispatch' trigger"
    if isinstance(wd_block, dict):
        inputs = wd_block.get("inputs", {}) or {}
        assert "tag" in inputs, (
            f"workflow_dispatch must have a 'tag' input; got inputs: {list(inputs.keys())}"
        )


# ---------------------------------------------------------------------------
# 7. compute-version.ps1 referenced in build job
# ---------------------------------------------------------------------------
def test_build_references_compute_version(workflow: dict) -> None:
    """build job must reference tools/compute-version.ps1."""
    jobs = workflow["jobs"]
    build_job = jobs.get("build", {})
    combined = _steps_combined_run(build_job)
    assert "compute-version.ps1" in combined, (
        "build job must invoke tools/compute-version.ps1 to derive NuGet version; "
        "not found in any step's 'run' script"
    )


# ---------------------------------------------------------------------------
# 8. check-regression.py --current-sha referenced in perf-on-pack job
# ---------------------------------------------------------------------------
def test_perf_references_check_regression_with_current_sha(workflow: dict) -> None:
    """perf-on-pack job must reference check-regression.py with --current-sha."""
    jobs = workflow["jobs"]
    perf_job = jobs.get("perf-on-pack", {})
    combined = _steps_combined_run(perf_job)
    assert "check-regression.py" in combined, (
        "perf-on-pack job must invoke tools/check-regression.py; "
        "not found in any step's 'run' script"
    )
    assert "--current-sha" in combined, (
        "perf-on-pack must pass --current-sha to check-regression.py (R4-3 fix); "
        "not found in check-regression.py invocation"
    )


# ---------------------------------------------------------------------------
# 9. ledger-event.py referenced in record job
# ---------------------------------------------------------------------------
def test_record_references_ledger_event(workflow: dict) -> None:
    """record job must reference tools/ledger-event.py."""
    jobs = workflow["jobs"]
    record_job = jobs.get("record", {})
    combined = _steps_combined_run(record_job)
    assert "ledger-event.py" in combined, (
        "record job must invoke tools/ledger-event.py; "
        "not found in any step's 'run' script"
    )


def test_record_emits_published_event(workflow: dict) -> None:
    """record job must emit the 'published' event type."""
    jobs = workflow["jobs"]
    record_job = jobs.get("record", {})
    combined = _steps_combined_run(record_job)
    assert "published" in combined, (
        "record job must emit '--event published' via ledger-event.py"
    )


# ---------------------------------------------------------------------------
# 10. NuGet target URL contains nuget.pkg.github.com/initialforce
# ---------------------------------------------------------------------------
def test_publish_targets_github_packages_initialforce(workflow: dict) -> None:
    """publish job must push to nuget.pkg.github.com/initialforce."""
    jobs = workflow["jobs"]
    publish_job = jobs.get("publish", {})
    combined = _steps_combined_run(publish_job)
    assert "nuget.pkg.github.com/initialforce" in combined.lower(), (
        "publish job must push to https://nuget.pkg.github.com/initialforce/index.json; "
        "not found in step scripts"
    )


# ---------------------------------------------------------------------------
# 11. Concurrency group: release-${{ github.ref }}, cancel-in-progress: false
# ---------------------------------------------------------------------------
def test_concurrency_group_release_ref(workflow: dict) -> None:
    """Concurrency group must use release-${{ github.ref }}."""
    concurrency = workflow.get("concurrency", {})
    assert concurrency is not None, "Missing top-level 'concurrency' block"
    group = concurrency.get("group", "")
    assert "release-" in group, (
        f"concurrency.group must start with 'release-'; got: {group}"
    )
    assert "github.ref" in group, (
        f"concurrency.group must reference github.ref; got: {group}"
    )


def test_concurrency_cancel_in_progress_false(workflow: dict) -> None:
    """cancel-in-progress must be false (never cancel an in-flight release)."""
    concurrency = workflow.get("concurrency", {})
    cip = concurrency.get("cancel-in-progress")
    assert cip is False, (
        f"concurrency.cancel-in-progress must be false for release workflow; got: {cip}"
    )


# ---------------------------------------------------------------------------
# 12. Permissions: contents:write, packages:write, id-token:write
# ---------------------------------------------------------------------------
def test_permissions_contents_write(workflow: dict) -> None:
    """Workflow must grant contents:write (needed to create GitHub Release)."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("contents") == "write", (
        f"permissions.contents must be 'write'; got: {permissions.get('contents')}"
    )


def test_permissions_packages_write(workflow: dict) -> None:
    """Workflow must grant packages:write (needed to publish to GitHub Packages)."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("packages") == "write", (
        f"permissions.packages must be 'write'; got: {permissions.get('packages')}"
    )


def test_permissions_id_token_write(workflow: dict) -> None:
    """Workflow must grant id-token:write (needed for OIDC-based operations)."""
    permissions = workflow.get("permissions", {})
    assert permissions.get("id-token") == "write", (
        f"permissions.id-token must be 'write'; got: {permissions.get('id-token')}"
    )


# ---------------------------------------------------------------------------
# 13. build job packs both InitialForce.WPF packages
# ---------------------------------------------------------------------------
def test_build_packs_initialforce_wpf(workflow: dict) -> None:
    """build job must pack packaging/InitialForce.WPF/InitialForce.WPF.csproj."""
    jobs = workflow["jobs"]
    build_job = jobs.get("build", {})
    combined = _steps_combined_run(build_job)
    assert "InitialForce.WPF.csproj" in combined, (
        "build job must run dotnet pack on packaging/InitialForce.WPF/InitialForce.WPF.csproj"
    )


def test_build_packs_initialforce_wpf_runtimeoverride(workflow: dict) -> None:
    """build job must pack the RuntimeOverride csproj."""
    jobs = workflow["jobs"]
    build_job = jobs.get("build", {})
    combined = _steps_combined_run(build_job)
    assert "InitialForce.WPF.RuntimeOverride.csproj" in combined, (
        "build job must run dotnet pack on "
        "packaging/InitialForce.WPF.RuntimeOverride/InitialForce.WPF.RuntimeOverride.csproj"
    )


# ---------------------------------------------------------------------------
# 14. release-notes job invokes the release-notes.md prompt
# ---------------------------------------------------------------------------
def test_release_notes_uses_release_notes_prompt(workflow: dict) -> None:
    """release-notes job must reference .if-fork/prompts/release-notes.md."""
    jobs = workflow["jobs"]
    rn_job = jobs.get("release-notes", {})
    # The prompt is passed as a claude_args option; check all step content
    steps = rn_job.get("steps", [])
    all_content = ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        all_content += step.get("run", "") + "\n"
        with_block = step.get("with", {}) or {}
        all_content += str(with_block.get("claude_args", "")) + "\n"
    assert "release-notes.md" in all_content, (
        "release-notes job must pass .if-fork/prompts/release-notes.md as the prompt file"
    )


# ---------------------------------------------------------------------------
# 15. actionlint (skipped gracefully if not installed)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint not available; skipping lint gate",
)
def test_actionlint_release_yml() -> None:
    """actionlint must pass on release.yml when available."""
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint failed on release.yml:\n{result.stdout}\n{result.stderr}"
    )
