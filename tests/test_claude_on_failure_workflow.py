"""
Tests for .github/workflows/claude-on-failure.yml

The workflow keeps one open issue per failing workflow: a new failure gets a
Claude analysis and one issue, repeats get a comment, the next success closes it.
These tests pin that structure and the loop guards without running anything.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_PATH = WORKFLOWS_DIR / "claude-on-failure.yml"
FAILURE_ANALYSIS_PROMPT_PATH = REPO_ROOT / ".if-fork" / "prompts" / "failure-analysis.md"

REQUIRED_JOBS = {"triage", "gate", "analyze", "record"}


def _load(path: Path) -> dict:
    with path.open() as fh:
        doc = yaml.safe_load(fh)
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW_PATH)


def _steps(job: dict) -> dict[str, dict]:
    return {s.get("name", ""): s for s in job.get("steps", [])}


def test_jobs(workflow: dict) -> None:
    assert set(workflow["jobs"]) == REQUIRED_JOBS


def test_watched_workflows_exist(workflow: dict) -> None:
    """Every watched name must match a real workflow, or the trigger silently never fires."""
    names = {_load(p).get("name") for p in WORKFLOWS_DIR.glob("*.yml")}
    watched = workflow["on"]["workflow_run"]["workflows"]
    missing = [w for w in watched if w not in names]
    assert not missing, f"watched workflows with no matching name: {missing}"
    assert workflow["on"]["workflow_run"]["types"] == ["completed"]


def test_does_not_watch_itself(workflow: dict) -> None:
    assert workflow["name"] not in workflow["on"]["workflow_run"]["workflows"]


def test_triage_skips_pull_requests_and_other_conclusions(workflow: dict) -> None:
    cond = workflow["jobs"]["triage"]["if"]
    assert "workflow_run.event != 'pull_request'" in cond
    assert "conclusion == 'failure'" in cond
    assert "conclusion == 'success'" in cond


def test_triage_finds_issue_by_marker(workflow: dict) -> None:
    run = _steps(workflow["jobs"]["triage"])["Find open issue for this workflow"]["run"]
    assert "<!-- ci-failure:${FAILED_WORKFLOW} -->" in run
    for action in ("close", "comment", "new", "none"):
        assert action in run


def test_repeat_failure_comments_and_success_closes(workflow: dict) -> None:
    steps = _steps(workflow["jobs"]["triage"])
    close = steps["Close issue (workflow is green again)"]
    assert close["if"] == "steps.find.outputs.action == 'close'"
    assert "gh issue close" in close["run"]
    comment = steps["Comment on existing issue (repeat failure)"]
    assert comment["if"] == "steps.find.outputs.action == 'comment'"
    assert "gh issue comment" in comment["run"]


def test_claude_only_for_new_failures(workflow: dict) -> None:
    gate = workflow["jobs"]["gate"]
    assert "autonomy-check.yml" in gate["uses"]
    assert gate["with"]["requested_action"] == "claude-invoke"
    assert gate["if"] == "needs.triage.outputs.action == 'new'"
    analyze = workflow["jobs"]["analyze"]
    assert "gate" in analyze["needs"]
    assert "needs.gate.outputs.proceed == 'true'" in analyze["if"]


def test_analysis_budget_and_tools(workflow: dict) -> None:
    step = _steps(workflow["jobs"]["analyze"])["Run failure analysis (Claude Sonnet)"]
    args = step["with"]["claude_args"]
    assert "--max-turns 25" in args
    assert "Edit" in args.split("--disallowedTools", 1)[1]
    assert "mcp__github__issue_write" in args.split("--disallowedTools", 1)[1]
    assert step.get("continue-on-error") is True
    assert "failure-analysis.md" in str(workflow["jobs"]["analyze"])


def test_record_opens_one_issue_even_without_analysis(workflow: dict) -> None:
    record = workflow["jobs"]["record"]
    assert {"triage", "gate", "analyze"} <= set(record["needs"])
    assert "always()" in record["if"]
    assert "needs.triage.outputs.action == 'new'" in record["if"]
    run = _steps(record)["Open failure issue"]["run"]
    assert "<!-- ci-failure:${FAILED_WORKFLOW} -->" in run
    assert "--label automated" in run
    download = _steps(record)["Download analysis artifact"]
    assert download.get("continue-on-error") is True


def test_ledger_event_only_after_analysis(workflow: dict) -> None:
    step = _steps(workflow["jobs"]["record"])["Emit failure_analyzed ledger event"]
    assert step["if"] == "needs.analyze.result == 'success'"
    assert "--event failure_analyzed" in step["run"]
    assert "tools/ledger-event.py" in step["run"]


def test_concurrency_per_workflow(workflow: dict) -> None:
    conc = workflow["concurrency"]
    assert "workflow_run.workflow_id" in conc["group"]
    assert conc["cancel-in-progress"] is False


def test_permissions(workflow: dict) -> None:
    assert workflow["permissions"] == {
        "actions": "read",
        "contents": "read",
        "issues": "write",
        "id-token": "write",
    }


def test_prompt_exists() -> None:
    text = FAILURE_ANALYSIS_PROMPT_PATH.read_text()
    assert "ANALYSIS_PATH" in text
    assert "gh issue create" not in text


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not available")
def test_actionlint_claude_on_failure_yml() -> None:
    result = subprocess.run(["actionlint", str(WORKFLOW_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"
