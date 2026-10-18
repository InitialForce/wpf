"""
Tests for .github/workflows/weekly-differential.yml

The workflow builds an upstream tree (if/main with src/ from its upstream base)
and the fork tree with the same tooling, runs the smoke harness on both, and
keeps one issue up to date with the comparison. These tests pin that structure.
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

EXPECTED_JOBS = {"gate", "build-smoke", "compare"}


@pytest.fixture(scope="module")
def workflow() -> dict:
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def _steps(job: dict) -> dict[str, dict]:
    return {s.get("name", ""): s for s in job["steps"]}


def test_jobs(workflow: dict) -> None:
    assert set(workflow["jobs"]) == EXPECTED_JOBS


def test_schedule(workflow: dict) -> None:
    assert workflow["on"]["schedule"][0]["cron"] == "23 5 * * 1"
    assert "workflow_dispatch" in workflow["on"]


def test_gate(workflow: dict) -> None:
    assert "autonomy-check.yml" in workflow["jobs"]["gate"]["uses"]
    for name in ("build-smoke", "compare"):
        job = workflow["jobs"][name]
        assert "gate" in job["needs"]
        assert "needs.gate.outputs.proceed == 'true'" in job["if"]


def test_matrix_builds_both_variants(workflow: dict) -> None:
    job = workflow["jobs"]["build-smoke"]
    assert job["strategy"]["matrix"]["variant"] == ["upstream", "fork"]
    assert job["strategy"]["fail-fast"] is False


def test_both_variants_use_if_main_tooling(workflow: dict) -> None:
    job = workflow["jobs"]["build-smoke"]
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "if/main"
    swap = _steps(job)["Replace src/ with the upstream base (upstream variant only)"]
    assert swap["if"] == "matrix.variant == 'upstream'"
    assert "git merge-base HEAD upstream/release/10.0" in swap["run"]
    assert 'git checkout "$BASE" -- src' in swap["run"]


def test_build_matches_build_yml_invocation(workflow: dict) -> None:
    """The weekly runs failed on '/p:OfficialBuildId=0'; use build.cmd like build.yml."""
    runs = [s.get("run", "") for s in workflow["jobs"]["build-smoke"]["steps"]]
    assert not any("OfficialBuildId" in r for r in runs)
    run = _steps(workflow["jobs"]["build-smoke"])["Build WPF (Release x64)"]["run"]
    assert "build.cmd -c Release -plat x64" in run


def test_smoke_results_uploaded_per_variant(workflow: dict) -> None:
    up = _steps(workflow["jobs"]["build-smoke"])["Upload smoke results"]
    assert up["with"]["name"] == "smoke-${{ matrix.variant }}"
    run = _steps(workflow["jobs"]["build-smoke"])["Run smoke harness"]["run"]
    assert "--result:" in run
    assert "smoke harness produced no result file" in run


def test_compare_uses_diff_tool(workflow: dict) -> None:
    assert DIFF_TOOL_PATH.exists()
    steps = _steps(workflow["jobs"]["compare"])
    run = steps["Run diff-smoke-results.py"]["run"]
    assert "tools/diff-smoke-results.py" in run
    assert "smoke-results/smoke-upstream.xml" in run
    assert "smoke-results/smoke-fork.xml" in run


def test_single_issue_is_updated_not_duplicated(workflow: dict) -> None:
    run = _steps(workflow["jobs"]["compare"])["Update the differential issue"]["run"]
    assert "<!-- weekly-differential -->" in run
    assert "gh issue close" in run
    assert "gh issue comment" in run
    assert "gh issue create" in run
    assert run.count("gh issue create") == 1
    assert "--label automated" in run


def test_permissions(workflow: dict) -> None:
    assert workflow["permissions"] == {"contents": "read", "issues": "write"}


def test_concurrency(workflow: dict) -> None:
    assert "weekly-diff" in workflow["concurrency"]["group"]


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not available")
def test_actionlint() -> None:
    result = subprocess.run(["actionlint", str(WORKFLOW_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"
