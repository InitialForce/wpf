"""
Tests for .github/workflows/upstream-stable-adoption.yml

Validates the YAML structure, job topology, trigger configuration, and key
gating invariants without executing the workflow. If actionlint is available
it will also be called as a lint gate.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = (
    REPO_ROOT / ".github" / "workflows" / "upstream-stable-adoption.yml"
)

EXPECTED_JOBS = {"gate", "verify-tag", "promote-mirror", "trigger-rebase", "notify"}

# Jobs that must be skipped when the gate says proceed=false
GATED_JOBS = {"verify-tag", "promote-mirror", "trigger-rebase", "notify"}


def _normalize_on_key(doc: dict) -> dict:
    """
    PyYAML parses the bare YAML key ``on`` as the Python boolean ``True``
    (YAML 1.1 spec).  Normalize it back to the string ``"on"`` so tests can
    use consistent key names.
    """
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    """Parse and return the upstream-stable-adoption.yml document."""
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict[str, Any]:
    """Return the jobs mapping from the parsed workflow."""
    return workflow.get("jobs", {})


# ---------------------------------------------------------------------------
# Gate 1: YAML parses without error (covered by the fixture; explicit assertion)
# ---------------------------------------------------------------------------
def test_yaml_is_valid(workflow: dict) -> None:
    """The workflow YAML must parse to a non-empty dict."""
    assert isinstance(workflow, dict) and workflow, (
        "YAML did not parse to a non-empty dict"
    )


# ---------------------------------------------------------------------------
# Gate 2: All 5 jobs are present
# ---------------------------------------------------------------------------
def test_all_five_jobs_present(jobs: dict) -> None:
    """Exactly the five required jobs must be declared."""
    missing = EXPECTED_JOBS - set(jobs.keys())
    assert not missing, f"Missing jobs: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Gate 3: `needs:` chain is correct
# ---------------------------------------------------------------------------
def test_needs_chain(jobs: dict) -> None:
    """Each gated job must declare a needs: chain that includes 'gate'."""

    def _needs(job_def: dict) -> set[str]:
        raw = job_def.get("needs", [])
        if isinstance(raw, str):
            return {raw}
        return set(raw)

    # verify-tag needs gate
    assert "gate" in _needs(jobs["verify-tag"]), (
        "verify-tag must depend on gate"
    )
    # promote-mirror needs gate and verify-tag
    pm_needs = _needs(jobs["promote-mirror"])
    assert "gate" in pm_needs, "promote-mirror must depend on gate"
    assert "verify-tag" in pm_needs, "promote-mirror must depend on verify-tag"

    # trigger-rebase needs gate and promote-mirror
    tr_needs = _needs(jobs["trigger-rebase"])
    assert "gate" in tr_needs, "trigger-rebase must depend on gate"
    assert "promote-mirror" in tr_needs, "trigger-rebase must depend on promote-mirror"

    # notify needs gate, verify-tag, promote-mirror, trigger-rebase
    n_needs = _needs(jobs["notify"])
    assert "gate" in n_needs, "notify must depend on gate"
    assert "promote-mirror" in n_needs, "notify must depend on promote-mirror"


# ---------------------------------------------------------------------------
# Gate 4: repository_dispatch trigger with correct event type
# ---------------------------------------------------------------------------
def test_repository_dispatch_trigger(workflow: dict) -> None:
    """on.repository_dispatch must list 'upstream-stable-released' in types."""
    on_block = workflow.get("on", {})
    assert "repository_dispatch" in on_block, (
        "'repository_dispatch' trigger not found"
    )
    rd = on_block["repository_dispatch"]
    assert "types" in rd, "repository_dispatch must declare 'types'"
    types = rd["types"]
    assert "upstream-stable-released" in types, (
        "'upstream-stable-released' not in repository_dispatch.types"
    )


# ---------------------------------------------------------------------------
# Gate 5: workflow_dispatch trigger with 'tag' input
# ---------------------------------------------------------------------------
def test_workflow_dispatch_with_tag_input(workflow: dict) -> None:
    """on.workflow_dispatch must exist and declare a required 'tag' input."""
    on_block = workflow.get("on", {})
    assert "workflow_dispatch" in on_block, (
        "'workflow_dispatch' trigger not found"
    )
    wd = on_block["workflow_dispatch"] or {}
    assert "inputs" in wd, "workflow_dispatch must declare inputs"
    inputs = wd["inputs"]
    assert "tag" in inputs, "'tag' input not declared in workflow_dispatch.inputs"
    tag_input = inputs["tag"]
    assert tag_input.get("required") is True, "'tag' input must be required"


# ---------------------------------------------------------------------------
# Gate 6: gate job uses the autonomy-check reusable workflow
# ---------------------------------------------------------------------------
def test_gate_job_uses_autonomy_check(jobs: dict) -> None:
    """The 'gate' job must call the autonomy-check reusable workflow."""
    gate_job = jobs.get("gate", {})
    assert "uses" in gate_job, "'gate' job must use a reusable workflow via 'uses'"
    assert "autonomy-check.yml" in gate_job["uses"], (
        "'gate' job must reference autonomy-check.yml"
    )


# ---------------------------------------------------------------------------
# Gate 7: gated jobs have an 'if' condition referencing gate.outputs.proceed
# ---------------------------------------------------------------------------
def test_gated_jobs_have_proceed_condition(jobs: dict) -> None:
    """Every gated job must have an if: condition checking gate.outputs.proceed."""
    for job_name in GATED_JOBS:
        job = jobs.get(job_name, {})
        if_condition = job.get("if", "")
        assert "gate.outputs.proceed" in str(if_condition), (
            f"Job '{job_name}' must check gate.outputs.proceed in its 'if' condition; "
            f"got: {if_condition!r}"
        )


# ---------------------------------------------------------------------------
# Gate 8: promote-mirror step issues a gh issue create on non-FF failure
# ---------------------------------------------------------------------------
def test_promote_mirror_opens_issue_on_non_ff(jobs: dict) -> None:
    """promote-mirror must call 'gh issue create' in its promotion step."""
    pm_job = jobs.get("promote-mirror", {})
    steps = pm_job.get("steps", [])
    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    assert "gh issue create" in combined_run, (
        "promote-mirror must open a gh issue on non-fast-forward failure"
    )
    assert "--ff-only" in combined_run or "ff-only" in combined_run, (
        "promote-mirror must use fast-forward-only merge (--ff-only)"
    )


# ---------------------------------------------------------------------------
# Gate 9: trigger-rebase dispatches 'nightly-rebase' event
# ---------------------------------------------------------------------------
def test_trigger_rebase_dispatches_nightly_rebase(jobs: dict) -> None:
    """trigger-rebase must dispatch a 'nightly-rebase' repository_dispatch event."""
    tr_job = jobs.get("trigger-rebase", {})
    steps = tr_job.get("steps", [])
    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    assert "nightly-rebase" in combined_run, (
        "trigger-rebase must dispatch event_type=nightly-rebase"
    )


# ---------------------------------------------------------------------------
# Gate 10: notify step opens an issue with 'operator-followup' label
# ---------------------------------------------------------------------------
def test_notify_opens_operator_followup_issue(jobs: dict) -> None:
    """The notify job must open a gh issue with the 'operator-followup' label."""
    notify_job = jobs.get("notify", {})
    steps = notify_job.get("steps", [])
    combined_run = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    assert "operator-followup" in combined_run, (
        "notify job must create an issue with 'operator-followup' label"
    )
    assert "gh issue create" in combined_run, (
        "notify job must call 'gh issue create'"
    )


# ---------------------------------------------------------------------------
# Gate 11: concurrency group matches the shared release-branch-write key
# ---------------------------------------------------------------------------
def test_concurrency_group(workflow: dict) -> None:
    """The workflow must share the 'release-branch-write' concurrency group."""
    concurrency = workflow.get("concurrency", {})
    assert isinstance(concurrency, dict), "'concurrency' must be a mapping"
    group = concurrency.get("group", "")
    assert "release-branch-write" in group, (
        f"concurrency.group must contain 'release-branch-write'; got {group!r}"
    )
    assert concurrency.get("cancel-in-progress") is False, (
        "cancel-in-progress must be false to prevent concurrent branch writes"
    )


# ---------------------------------------------------------------------------
# Gate 12: permissions include contents:write and pull-requests:write
# ---------------------------------------------------------------------------
def test_permissions(workflow: dict) -> None:
    """Workflow-level permissions must include contents:write and pull-requests:write."""
    perms = workflow.get("permissions", {})
    assert isinstance(perms, dict), "'permissions' must be a mapping"
    assert perms.get("contents") == "write", "permissions.contents must be 'write'"
    assert perms.get("pull-requests") == "write", (
        "permissions.pull-requests must be 'write'"
    )


# ---------------------------------------------------------------------------
# actionlint (skipped gracefully if not installed)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint not available; skipping lint gate",
)
def test_actionlint() -> None:
    """actionlint must exit 0 on the workflow file."""
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint failed:\n{result.stdout}\n{result.stderr}"
    )
