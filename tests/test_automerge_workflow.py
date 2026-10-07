"""
Tests for .github/workflows/automerge.yml

The workflow merges the pipeline's own PRs (nightly sync, cherry-picks) once
Build passes on the exact head commit. These tests pin the safety conditions.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "automerge.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _steps(workflow: dict) -> dict[str, dict]:
    return {s["name"]: s for s in workflow["jobs"]["merge"]["steps"]}


def test_triggered_by_build_completion(workflow: dict) -> None:
    wr = workflow["on"]["workflow_run"]
    assert wr["workflows"] == ["Build"]
    assert wr["types"] == ["completed"]


def test_build_workflow_name_matches() -> None:
    build = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "build.yml").read_text())
    assert build["name"] == "Build"


def test_gate_requests_auto_merge(workflow: dict) -> None:
    gate = workflow["jobs"]["gate"]
    assert "autonomy-check.yml" in gate["uses"]
    assert gate["with"]["requested_action"] == "auto-merge"
    assert "bypass_for_human_dispatch" not in gate["with"]


def test_only_pipeline_branches_and_pull_request_events(workflow: dict) -> None:
    cond = workflow["jobs"]["gate"]["if"]
    assert "workflow_run.event == 'pull_request'" in cond
    assert "'if/staging'" in cond
    assert "'claude/cherry-pick-pr-'" in cond


def test_eligibility_checks(workflow: dict) -> None:
    run = _steps(workflow)["Find the PR for this build"]["run"]
    for needle in (
        '.state != "open"',
        '.base.ref != "if/main"',
        ".head.repo.full_name != $repo",
        '.user.type != "Bot"',
        'index("automated")',
        ".head.sha != $sha",
    ):
        assert needle in run, f"missing eligibility check {needle!r}"


def test_protected_paths_need_a_human(workflow: dict) -> None:
    run = _steps(workflow)["Find the PR for this build"]["run"]
    assert "/files" in run
    for path in (r"\.github/workflows/", r"\.if-fork/config\.yaml$", r"\.if-fork/prompts/"):
        assert path in run, f"protected path {path!r} not refused"


def test_merge_requires_success_and_gate(workflow: dict) -> None:
    step = _steps(workflow)["Merge PR (build passed)"]
    assert "conclusion == 'success'" in step["if"]
    assert "needs.gate.outputs.proceed == 'true'" in step["if"]
    assert "--merge" in step["run"]
    assert "--match-head-commit" in step["run"]
    assert "--squash" not in step["run"]
    assert "--auto" not in step["run"]


def test_failed_build_comment_is_deduplicated(workflow: dict) -> None:
    step = _steps(workflow)["Comment once on a failed build"]
    assert "conclusion == 'failure'" in step["if"]
    assert "automerge-build-failed:$HEAD_SHA" in step["run"]
    assert "grep -qF" in step["run"]


def test_merge_job_uses_app_token(workflow: dict) -> None:
    job = workflow["jobs"]["merge"]
    assert job["needs"] == "gate"
    assert job["environment"] == "branch-promotion"
    assert any("github-app-token" in s.get("uses", "") for s in job["steps"])


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not available")
def test_actionlint_automerge() -> None:
    result = subprocess.run(["actionlint", str(WORKFLOW_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"
