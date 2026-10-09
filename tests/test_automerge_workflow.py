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
    step = _steps(workflow)["Find the PR for this build"]
    run = step["run"]
    for needle in (
        '.state != "open"',
        '.base.ref != "if/main"',
        ".head.repo.full_name != $repo",
        ".user.login != $bot",
        'index("automated")',
        ".head.sha != $sha",
    ):
        assert needle in run, f"missing eligibility check {needle!r}"
    assert step["env"]["BOT_LOGIN"] == "${{ steps.app-token.outputs.app-slug }}[bot]"


def test_verify_uses_trusted_if_main(workflow: dict) -> None:
    steps = _steps(workflow)
    trusted = steps["Check out if/main (trusted tools, policy and bot key)"]
    assert trusted["with"] == {"ref": "if/main", "path": "trusted", "persist-credentials": False}
    verify = steps["Verify commits, denylist and base"]
    assert 'bash "$TRUSTED/tools/automerge-verify.sh"' in verify["run"]
    assert verify["env"]["TRUSTED"] == "${{ github.workspace }}/trusted"
    assert verify["env"]["BOT_FPR"] == "${{ secrets.GH_BOT_GPG_FINGERPRINT }}"


def test_protected_paths_and_denylist_in_verify_script() -> None:
    text = (REPO_ROOT / "tools" / "automerge-verify.sh").read_text()
    for path in (r"\.github/", r"\.if-fork/", "tools/", "docs/", "test/", "tests/"):
        assert path in text, f"protected path {path!r} not refused"
    assert "grep -vE '^(src|eng)/'" in text
    assert '"$TRUSTED/tools/check-denylist.py" --config "$TRUSTED/.if-fork/config.yaml"' in text
    assert '"$TRUSTED/tools/check-approval.py"' in text
    assert '"$TRUSTED/docs/bot-gpg-pubkey.asc"' in text


def test_merge_requires_verified_up_to_date_head_and_gate(workflow: dict) -> None:
    steps = _steps(workflow)
    assert "env.CONCLUSION == 'success'" in steps["Verify commits, denylist and base"]["if"]
    step = steps["Merge PR (build passed)"]
    assert "steps.verify.outputs.result == 'ok'" in step["if"]
    assert "needs.gate.outputs.proceed == 'true'" in step["if"]
    assert "--merge" in step["run"]
    assert "--match-head-commit" in step["run"]
    assert "--squash" not in step["run"]
    assert "--auto" not in step["run"]


def test_behind_pr_is_rebuilt_not_updated_by_github(workflow: dict) -> None:
    step = _steps(workflow)["Rebuild the branch on the new if/main"]
    assert "steps.verify.outputs.result == 'behind'" in step["if"]
    assert '"nightly-rebase"' in step["run"] and "force: true" in step["run"]
    assert '"pr-ingestion-requested"' in step["run"]
    assert "update-branch" not in WORKFLOW_PATH.read_text().replace("update-branch, whose", "")


def test_concurrency_bound_to_if_main(workflow: dict) -> None:
    assert workflow["concurrency"] == {"group": "automerge-if-main", "cancel-in-progress": False}


def test_failed_build_comment_is_deduplicated(workflow: dict) -> None:
    step = _steps(workflow)["Comment once on a failed build"]
    assert "env.CONCLUSION == 'failure'" in step["if"]
    assert "automerge-build-failed:$HEAD_SHA" in step["run"]
    assert "grep -qF" in step["run"]


def test_merge_job_uses_app_token_without_environment(workflow: dict) -> None:
    job = workflow["jobs"]["merge"]
    assert job["needs"] == "gate"
    assert "environment" not in job
    assert any("create-github-app-token" in s.get("uses", "") for s in job["steps"])


def test_no_expressions_inside_run_blocks(workflow: dict) -> None:
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), f"{name}: {step.get('name')}"


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not available")
def test_actionlint_automerge() -> None:
    result = subprocess.run(["actionlint", str(WORKFLOW_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"
