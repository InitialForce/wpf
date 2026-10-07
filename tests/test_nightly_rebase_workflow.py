"""
Tests for .github/workflows/nightly-rebase.yml

The nightly job merges upstream release/10.0 into a fresh if/staging and keeps a
single PR if/staging -> if/main open. These tests check the structure and the
de-duplication rules without executing the workflow. Actionlint runs when available.
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

EXPECTED_JOBS = {"gate", "sync"}
GATE_CONDITION = "needs.gate.outputs.proceed == 'true'"


def _normalize_on_key(doc: dict) -> dict:
    """PyYAML parses bare ``on`` as ``True``; map it back to ``"on"``."""
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


@pytest.fixture(scope="module")
def workflow() -> dict:
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


@pytest.fixture(scope="module")
def sync_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["sync"]["steps"]


def _step(steps: list[dict], name_part: str) -> dict:
    for s in steps:
        if name_part in s.get("name", ""):
            return s
    raise AssertionError(f"No step containing {name_part!r}: {[s.get('name') for s in steps]}")


def test_yaml_parses() -> None:
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, dict)


def test_jobs(workflow: dict) -> None:
    assert set(workflow["jobs"]) == EXPECTED_JOBS


def test_gate_job_uses_reusable_workflow(workflow: dict) -> None:
    assert "autonomy-check.yml" in workflow["jobs"]["gate"]["uses"]


def test_sync_is_gated(workflow: dict) -> None:
    sync = workflow["jobs"]["sync"]
    assert sync["needs"] == "gate" or "gate" in sync["needs"]
    assert GATE_CONDITION in sync["if"]


def test_schedule_trigger(workflow: dict) -> None:
    on_block = workflow["on"]
    assert "workflow_dispatch" in on_block
    assert "7 3 * * *" in [s["cron"] for s in on_block["schedule"]]
    assert on_block["repository_dispatch"]["types"] == ["nightly-rebase"]


def test_permissions_declared(workflow: dict) -> None:
    perms = workflow["permissions"]
    assert perms == {"contents": "write", "pull-requests": "write", "issues": "write"}


def test_shares_concurrency_with_stable_adoption(workflow: dict) -> None:
    assert workflow["concurrency"]["group"] == "release-branch-write"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_rebase_prompt_exists_and_size() -> None:
    size = len(REBASE_PROMPT_PATH.read_text(encoding="utf-8"))
    assert 800 <= size <= 5000, f"rebase.md is {size} chars"


def test_conflict_prompt_exists_and_size() -> None:
    size = len(CONFLICT_PROMPT_PATH.read_text(encoding="utf-8"))
    assert 800 <= size <= 5000, f"resolve-rebase-conflict.md is {size} chars"


# ---------------------------------------------------------------------------
# Branch model: merge into if/staging made from if/main, never rebase if/main
# ---------------------------------------------------------------------------
def test_checks_out_if_main(sync_steps: list[dict]) -> None:
    checkout = next(s for s in sync_steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "if/main"
    assert checkout["with"]["fetch-depth"] == 0


def test_merges_instead_of_rebasing(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Merge upstream")["run"]
    assert 'git checkout -B "$STAGING_BRANCH" "origin/$MAIN_BRANCH"' in run
    assert "git merge --no-ff --no-commit" in run
    raw = WORKFLOW_PATH.read_text()
    assert "git rebase" not in raw


def test_push_targets_only_staging(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Commit merge and push")["run"]
    assert "--force-with-lease" in run
    assert 'HEAD:refs/heads/$STAGING_BRANCH' in run
    raw = WORKFLOW_PATH.read_text()
    assert "push origin if/main" not in raw


def test_merge_limited_to_upstream_changed_files(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Commit merge and push")["run"]
    assert "comm -23" in run
    assert "upstream did not change" in run


# ---------------------------------------------------------------------------
# De-duplication: no new PR per night, no work when nothing changed
# ---------------------------------------------------------------------------
def test_no_op_when_upstream_already_in_main(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Decide whether a sync is needed")["run"]
    assert 'git merge-base --is-ancestor "$UPSTREAM_SHA" "origin/$MAIN_BRANCH"' in run
    assert "result=up-to-date" in run


def test_no_op_when_open_pr_already_has_upstream(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Decide whether a sync is needed")["run"]
    assert "result=already-proposed" in run
    assert '--head "$STAGING_BRANCH"' in run


def test_reuses_open_pr(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Open or update the sync PR")["run"]
    assert 'if [ -n "$OPEN_PR" ]' in run
    assert "gh pr create" in run
    # Label failure must not fail the step.
    assert "--add-label automated" in run
    assert "|| echo" in run


def test_no_dated_branches() -> None:
    assert "claude/rebase-" not in WORKFLOW_PATH.read_text()


# ---------------------------------------------------------------------------
# Conflicts and failure reporting
# ---------------------------------------------------------------------------
def test_denylisted_conflicts_skip_claude(sync_steps: list[dict]) -> None:
    deny = _step(sync_steps, "Refuse conflicts on denylisted files")
    assert "tools/check-denylist.py" in deny["run"]
    resolve = _step(sync_steps, "Resolve conflicts")
    assert resolve["if"] == "steps.denylist.outputs.allowed == 'true'"
    assert resolve.get("continue-on-error") is True
    assert "--max-turns 40" in resolve["with"]["claude_args"]


def test_verify_checks_conflict_markers(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Verify resolution")["run"]
    assert "<<<<<<<" in run
    assert "--diff-filter=U" in run


def test_blocked_sync_issue_is_deduplicated(sync_steps: list[dict]) -> None:
    step = _step(sync_steps, "Report blocked sync")
    run = step["run"]
    assert "gh issue list" in run
    assert "gh issue create" in run
    assert "gh issue comment" in run
    assert "already reports this upstream SHA" in run
    assert "--event rebase_failed" in run
    # The job must not fail: the issue is the signal.
    assert "exit 1" not in run


def test_blocked_issue_closed_on_success(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Close blocked-sync issue")["run"]
    assert "gh issue close" in run


def test_issue_labels_exist_only_automated() -> None:
    raw = WORKFLOW_PATH.read_text()
    assert "rebase-failure" not in raw
    assert '--label "automated,rebase"' not in raw


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not available")
def test_actionlint_nightly_rebase() -> None:
    result = subprocess.run(["actionlint", str(WORKFLOW_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"
