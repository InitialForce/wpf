"""
Tests for .github/workflows/nightly-rebase.yml

The nightly job merges upstream release/10.0 into a fresh if/staging and keeps a
single PR if/staging -> if/main open. These tests check the structure and the
de-duplication rules without executing the workflow. Actionlint runs when available.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "nightly-rebase.yml"
REBASE_PROMPT_PATH = REPO_ROOT / ".if-fork" / "prompts" / "rebase.md"
CONFLICT_PROMPT_PATH = REPO_ROOT / ".if-fork" / "prompts" / "resolve-rebase-conflict.md"

EXPECTED_JOBS = {"gate", "sync", "resolve-conflicts", "finalize", "report-blocked"}
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


@pytest.fixture(scope="module")
def finalize_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["finalize"]["steps"]


@pytest.fixture(scope="module")
def report_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["report-blocked"]["steps"]


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




MERGE_SCRIPT = REPO_ROOT / "tools" / "nightly-merge.sh"


# ---------------------------------------------------------------------------
# Branch model: merge into if/staging made from if/main, never rebase if/main
# ---------------------------------------------------------------------------
def test_checks_out_if_main(sync_steps: list[dict]) -> None:
    checkouts = [s for s in sync_steps if s.get("uses", "").startswith("actions/checkout")]
    assert all(s["with"]["ref"] == "if/main" for s in checkouts)
    work = next(s for s in checkouts if s["with"]["path"] == "work")
    assert work["with"]["fetch-depth"] == 0


def test_merges_instead_of_rebasing(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Try the merge")["run"]
    assert 'bash "$TRUSTED/tools/nightly-merge.sh"' in run
    script = MERGE_SCRIPT.read_text()
    assert 'git checkout -q -B if/staging "$BASE_SHA"' in script
    assert "git merge --no-ff --no-commit" in script
    assert "git rebase" not in WORKFLOW_PATH.read_text()


def test_push_targets_only_staging(finalize_steps: list[dict]) -> None:
    run = _step(finalize_steps, "Commit merge and push")["run"]
    assert "--force-with-lease" in run
    assert "HEAD:refs/heads/$STAGING_BRANCH" in run
    assert "push origin if/main" not in WORKFLOW_PATH.read_text()


def test_merge_limited_to_upstream_changed_files(finalize_steps: list[dict]) -> None:
    run = _step(finalize_steps, "Recreate the merge")["run"]
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


def test_blocked_sha_is_not_retried_without_force(sync_steps: list[dict]) -> None:
    step = _step(sync_steps, "Decide whether a sync is needed")
    run = step["run"]
    assert "result=blocked" in run
    assert '"rebase_failed"' in run and "patch-ledger.jsonl" in run
    assert "contains(env.UPSTREAM_SHA)" in run
    assert 'if [ "$FORCE" != "true" ]' in run
    force = step["env"]["FORCE"]
    assert "inputs.force_rebase" in force and "client_payload.force" in force


def test_reuses_open_pr(finalize_steps: list[dict]) -> None:
    run = _step(finalize_steps, "Open or update the sync PR")["run"]
    assert 'if [ -n "$OPEN_PR" ]' in run
    assert "gh pr create" in run
    # Label failure must not fail the step.
    assert "--add-label automated" in run
    assert "|| echo" in run


def test_no_dated_branches() -> None:
    assert "claude/rebase-" not in WORKFLOW_PATH.read_text()


# ---------------------------------------------------------------------------
# Trust split: agent without credentials, trusted checker and push
# ---------------------------------------------------------------------------
def test_agent_job_has_no_credentials(workflow: dict) -> None:
    job = workflow["jobs"]["resolve-conflicts"]
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job
    text = yaml.safe_dump(job)
    assert "GH_APP" not in text and "GPG" not in text
    assert "import-bot-gpg" not in text and "github-app-token" not in text
    checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkouts and all(s["with"]["persist-credentials"] is False for s in checkouts)
    claude = _step(job["steps"], "Resolve conflicts")
    assert claude["with"]["github_token"] == "${{ github.token }}"
    assert claude.get("continue-on-error") is True
    args = claude["with"]["claude_args"]
    assert "--max-turns 40" in args
    # The rules come from tools/prepare-resolver.sh: Edit only on the conflicts.
    assert '--allowedTools "${{ steps.prepare.outputs.allowed }}"' in args
    assert claude["if"] == "steps.prepare.outputs.run == 'true'"
    deny = re.search(r'--disallowedTools "([^"]+)"', args)
    assert deny and "Bash" in deny.group(1).split(","), args
    assert "upload-artifact" in text


def test_denylisted_conflicts_skip_claude(workflow: dict, sync_steps: list[dict]) -> None:
    deny = _step(sync_steps, "Refuse conflicts on denylisted files")
    assert '"$TRUSTED/tools/check-denylist.py"' in deny["run"]
    assert '--config "$TRUSTED/.if-fork/config.yaml"' in deny["run"]
    assert "--changed-files" in deny["run"]
    assert "needs.sync.outputs.conflicts_allowed == 'true'" in (
        workflow["jobs"]["resolve-conflicts"]["if"]
    )


@pytest.mark.parametrize("job_name", ["sync", "finalize"])
def test_checker_runs_from_trusted_checkout(workflow: dict, job_name: str) -> None:
    job = workflow["jobs"][job_name]
    trusted = [s for s in job["steps"] if (s.get("with") or {}).get("path") == "trusted"]
    assert trusted and trusted[0]["with"]["ref"] == "if/main"
    for step in job["steps"]:
        run = step.get("run", "")
        assert "python3 tools/" not in run and "bash tools/" not in run, step.get("name")


def test_finalize_takes_only_conflicted_paths(finalize_steps: list[dict]) -> None:
    run = _step(finalize_steps, "Recreate the merge")["run"]
    assert 'bash "$TRUSTED/tools/nightly-merge.sh"' in run
    assert 'bash "$TRUSTED/tools/conflict-handoff.sh" apply' in run
    assert "escalation.md" in run
    assert run.index("conflict-handoff.sh") < run.index("comm -23")


def test_blocked_sync_issue_is_deduplicated(report_steps: list[dict]) -> None:
    run = _step(report_steps, "blocked-sync issue")["run"]
    assert "gh issue list" in run
    assert "gh issue create" in run
    assert "gh issue comment" in run
    assert "already reports this upstream SHA" in run
    assert "--event rebase_failed" in run
    # The job must not fail: the issue is the signal.
    assert "exit 1" not in run


def test_blocked_issue_closed_on_success(finalize_steps: list[dict]) -> None:
    step = _step(finalize_steps, "Close blocked-sync issue")
    assert "nightly-close-blocked.sh" in step["run"]
    script = (REPO_ROOT / "tools" / "nightly-close-blocked.sh").read_text()
    assert "gh issue close" in script


def test_issue_labels_exist_only_automated() -> None:
    raw = WORKFLOW_PATH.read_text()
    assert "rebase-failure" not in raw
    assert '--label "automated,rebase"' not in raw


def test_no_expressions_inside_run_blocks(workflow: dict) -> None:
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), f"{name}: {step.get('name')}"


def test_run_blocks_are_valid_bash(workflow: dict) -> None:
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if "run" in step:
                r = subprocess.run(
                    ["bash", "-n"], input=step["run"], text=True, capture_output=True
                )
                assert r.returncode == 0, f"{name}: {step.get('name')}: {r.stderr}"


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not available")
def test_actionlint_nightly_rebase() -> None:
    result = subprocess.run(["actionlint", str(WORKFLOW_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, f"actionlint failed:\n{result.stdout}\n{result.stderr}"


def test_if_main_commit_is_pinned_across_jobs() -> None:
    doc = yaml.safe_load(WORKFLOW_PATH.read_text())
    jobs = doc["jobs"]
    assert jobs["sync"]["outputs"]["main_sha"] == "${{ steps.plan.outputs.main_sha }}"
    plan = next(s for s in jobs["sync"]["steps"] if s.get("id") == "plan")
    assert 'echo "main_sha=$(git rev-parse "origin/$MAIN_BRANCH")"' in plan["run"]
    merge = next(s for s in jobs["sync"]["steps"] if s.get("id") == "merge")
    assert merge["env"]["BASE_SHA"] == "${{ steps.plan.outputs.main_sha }}"
    for name in ("resolve-conflicts", "finalize"):
        job = jobs[name]
        assert job["env"]["BASE_SHA"] == "${{ needs.sync.outputs.main_sha }}"
        assert '"$BASE_SHA" =~ ^[0-9a-f]{40}$' in job["steps"][0]["run"]
        refs = [
            s["with"]["ref"]
            for s in job["steps"]
            if "actions/checkout" in s.get("uses", "") and s["with"].get("path") != "trusted"
        ]
        assert refs == ["${{ env.BASE_SHA }}"], name
    check = next(s for s in jobs["finalize"]["steps"] if s.get("id") == "check")["run"]
    assert "origin/$MAIN_BRANCH" not in check
    assert 'git merge-base "$BASE_SHA" "$UPSTREAM_SHA"' in check


def test_agent_resolution_is_marked_for_a_person(finalize_steps: list[dict]) -> None:
    push = _step(finalize_steps, "Commit merge and push")
    assert '--trailer "Conflict-Resolved-By: claude"' in push["run"]
    assert push["env"]["HAS_CONFLICTS"] == "${{ needs.sync.outputs.has_conflicts }}"
    pr = _step(finalize_steps, "Open or update the sync PR")
    assert 'bash "$TRUSTED/tools/human-merge-label.sh" "$PR" "$HAS_CONFLICTS"' in pr["run"]
    assert "A person must merge this PR" in pr["run"]


def test_open_pr_is_rebuilt_when_if_main_moved(sync_steps: list[dict]) -> None:
    run = _step(sync_steps, "Decide whether a sync is needed")["run"]
    assert 'git diff --name-only "origin/$STAGING_BRANCH...origin/$MAIN_BRANCH"' in run
    assert "grep -vxF .if-fork/patch-ledger.jsonl" in run
    # A PR with Claude's resolution waits for a person; it is not rebuilt.
    assert "Conflict-Resolved-By" in run
