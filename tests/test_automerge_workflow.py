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


RUN_SCRIPT = REPO_ROOT / "tools" / "automerge-run.sh"


def test_triggers_build_completion_schedule_and_dispatch(workflow: dict) -> None:
    on = workflow["on"]
    assert on["workflow_run"]["workflows"] == ["Build"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert on["schedule"] == [{"cron": "*/30 * * * *"}]
    assert "workflow_dispatch" in on


def test_build_workflow_name_matches() -> None:
    build = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "build.yml").read_text())
    assert build["name"] == "Build"


def test_gate_requests_auto_merge(workflow: dict) -> None:
    gate = workflow["jobs"]["gate"]
    assert "autonomy-check.yml" in gate["uses"]
    assert gate["with"]["requested_action"] == "auto-merge"
    assert "bypass_for_human_dispatch" not in gate["with"]


def test_build_events_limited_to_pipeline_branches(workflow: dict) -> None:
    cond = workflow["jobs"]["gate"]["if"]
    assert "github.event_name != 'workflow_run'" in cond
    assert "workflow_run.event == 'pull_request'" in cond
    assert "'if/staging'" in cond
    assert "'claude/cherry-pick-pr-'" in cond


def test_every_run_processes_all_prs(workflow: dict) -> None:
    step = _steps(workflow)["Verify and merge every eligible PR"]
    assert 'bash "$TRUSTED/tools/automerge-run.sh"' in step["run"]
    assert step["env"]["PROCEED"] == "${{ needs.gate.outputs.proceed }}"
    assert step["env"]["BOT_FPR"] == "${{ secrets.GH_BOT_GPG_FINGERPRINT }}"
    assert step["env"]["TRUSTED"] == "${{ github.workspace }}/trusted"
    assert step["env"]["BOT_LOGIN"] == "${{ steps.app-token.outputs.app-slug }}[bot]"
    assert "if" not in step


def test_eligibility_checks_in_run_script() -> None:
    text = RUN_SCRIPT.read_text()
    for needle in (
        '.state != "open"',
        '.base.ref != "if/main"',
        ".head.repo.full_name != $repo",
        ".user.login != $bot",
        'index("automated")',
        ".head.sha != $sha",
        "actions/workflows/build.yml/runs?event=pull_request&head_sha=",
        '"$TRUSTED/tools/automerge-verify.sh"',
    ):
        assert needle in text, f"missing check {needle!r}"


def test_verify_uses_trusted_if_main(workflow: dict) -> None:
    steps = _steps(workflow)
    trusted = steps["Check out if/main (trusted tools, policy and bot key)"]
    assert trusted["with"] == {"ref": "if/main", "path": "trusted", "persist-credentials": False}


def test_protected_paths_and_denylist_in_verify_script() -> None:
    text = (REPO_ROOT / "tools" / "automerge-verify.sh").read_text()
    for path in (r"\.github/", r"\.if-fork/", "tools/", "docs/", "test/", "tests/"):
        assert path in text, f"protected path {path!r} not refused"
    assert "grep -vE '^(src|eng)/'" in text
    assert '"$TRUSTED/tools/check-denylist.py" --config "$TRUSTED/.if-fork/config.yaml"' in text
    assert '"$TRUSTED/tools/check-approval.py"' in text
    assert '"$TRUSTED/docs/bot-gpg-pubkey.asc"' in text


def test_merge_is_a_pinned_merge_commit() -> None:
    text = RUN_SCRIPT.read_text()
    assert '--merge --match-head-commit "$sha"' in text
    assert "--squash" not in text and "--auto" not in text
    assert "update-branch" not in text


def test_behind_pr_is_rebuilt_not_updated_by_github() -> None:
    text = RUN_SCRIPT.read_text()
    assert '"nightly-rebase"' in text and "force: true" in text
    assert '"pr-ingestion-requested"' in text
    assert "update-branch" not in WORKFLOW_PATH.read_text().replace("update-branch, whose", "")


def test_concurrency_bound_to_if_main(workflow: dict) -> None:
    assert workflow["concurrency"] == {"group": "automerge-if-main", "cancel-in-progress": False}


def test_failed_build_comment_is_deduplicated(workflow: dict) -> None:
    steps = _steps(workflow)
    assert "env.CONCLUSION == 'failure'" in steps["Find the PR whose build failed"]["if"]
    step = steps["Comment once on a failed build"]
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
