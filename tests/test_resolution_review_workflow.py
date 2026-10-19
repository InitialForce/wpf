"""Tests for .github/workflows/resolution-review.yml, the Fable review of a
conflict resolution that pr-ingestion.yml and nightly-rebase.yml call.

Structure and security invariants only; nothing is executed. The scripts it
runs (novel-lines.py, record-resolution-verdict.py) have their own tests.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "resolution-review.yml"
CONFIG = yaml.safe_load((REPO_ROOT / ".if-fork" / "config.yaml").read_text())


@pytest.fixture(scope="module")
def workflow() -> dict:
    doc = yaml.safe_load(WORKFLOW_PATH.read_text())
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _step(job: dict, name: str) -> dict:
    return next(s for s in job["steps"] if s.get("name") == name or s.get("id") == name)


def test_is_reusable_with_inputs_and_outputs(workflow: dict) -> None:
    call = workflow["on"]["workflow_call"]
    assert set(call["inputs"]) == {
        "kind", "pr_number", "upstream_sha", "base_sha", "branch_sha", "branch",
        "conflict_files",
    }
    assert call["outputs"]["next_action"]["value"] == "${{ jobs.record.outputs.next_action }}"
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"prepare", "review", "record"}


def test_inputs_reach_shell_only_through_env(workflow: dict) -> None:
    for name, job in workflow["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), f"{name}: {step.get('name')}"
            if "run" in step:
                r = subprocess.run(["bash", "-n"], input=step["run"], text=True,
                                   capture_output=True)
                assert r.returncode == 0, f"{name}: {step.get('name')}: {r.stderr}"


@pytest.mark.parametrize("job_name", ["prepare", "review", "record"])
def test_every_job_validates_inputs_first(workflow: dict, job_name: str) -> None:
    run = workflow["jobs"][job_name]["steps"][0]["run"]
    for check in ('"$PR_NUMBER" =~ ^[0-9]+$', '"$UPSTREAM_SHA" =~ ^[0-9a-f]{40}$',
                  '"$BASE_SHA" =~ ^[0-9a-f]{40}$', '"$BRANCH_SHA" =~ ^[0-9a-f]{40}$'):
        assert check in run, (job_name, check)


@pytest.mark.parametrize("job_name", ["prepare", "record"])
def test_branch_must_match_the_kind(workflow: dict, job_name: str) -> None:
    run = workflow["jobs"][job_name]["steps"][0]["run"]
    assert '[ "$BRANCH" = "claude/cherry-pick-pr-$PR_NUMBER" ]' in run
    assert '[ "$KIND" = sync ] && [ "$PR_NUMBER" = 0 ] && [ "$BRANCH" = if/staging ]' in run


def test_prepare_holds_no_secrets(workflow: dict) -> None:
    job = workflow["jobs"]["prepare"]
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job
    text = yaml.safe_dump(job)
    assert "secrets." not in text and "import-bot-gpg" not in text
    checkouts = [s for s in job["steps"] if "actions/checkout" in s.get("uses", "")]
    assert all(s["with"]["persist-credentials"] is False for s in checkouts)
    refs = {s["with"]["path"]: s["with"]["ref"] for s in checkouts}
    assert refs == {"trusted": "if/main", "work": "${{ inputs.branch_sha }}"}
    run = _step(job, "Write the review input")["run"]
    # Parents: one commit on BASE_SHA, or for a sync the merge with upstream.
    assert 'PARENTS="$BASE_SHA $UPSTREAM_SHA"' in run and '"$BRANCH_SHA $PARENTS"' in run
    # Cherry-pick: the approved diff, pinned like pr-review.yml.
    assert '[ "$FETCHED" = "$UPSTREAM_SHA" ]' in run
    # Sync: release/10.0 against its merge base with if/main.
    assert 'MERGE_BASE=$(git merge-base "$BASE_SHA" "$UPSTREAM_SHA")' in run
    assert '"$MERGE_BASE" "$UPSTREAM_SHA" > "$OUT/upstream.diff"' in run
    assert 'git diff --no-color --no-ext-diff --no-renames "$BASE_SHA" "$BRANCH_SHA"' in run
    assert 'python3 "$TRUSTED/tools/novel-lines.py"' in run
    # record checks the report against this job output before the novel cap.
    assert 'echo "novel_sha256=$(sha256sum "$OUT/novel-lines.json"' in run
    assert job["outputs"]["novel_sha256"] == "${{ steps.input.outputs.novel_sha256 }}"
    record = workflow["jobs"]["record"]
    decide = _step(record, "Apply the rules")
    assert decide["env"]["NOVEL_SHA256"] == "${{ needs.prepare.outputs.novel_sha256 }}"
    assert '--novel-sha256 "$NOVEL_SHA256"' in decide["run"]
    upload = next(s for s in job["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["name"] == "resolution-review-input"


def test_reviewer_is_locked_down(workflow: dict) -> None:
    job = workflow["jobs"]["review"]
    assert job["environment"] == "claude"
    assert job["permissions"] == {"contents": "read"}
    text = yaml.safe_dump(job)
    assert "GH_APP" not in text and "GPG" not in text and "github-app-token" not in text
    checkout = next(s for s in job["steps"] if "actions/checkout" in s.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["sparse-checkout"].split() == [
        "/.if-fork/config.yaml", "/.if-fork/prompts/",
    ]
    download = next(s for s in job["steps"] if "download-artifact" in s.get("uses", ""))
    assert download["with"] == {"name": "resolution-review-input", "path": "review-input"}
    claude = next(s for s in job["steps"] if "claude-code-action" in s.get("uses", ""))
    w = claude["with"]
    assert w["allowed_bots"] == "initial-force-wpf-bot"
    assert w["github_token"] == "${{ github.token }}"
    assert w["settings"] == '{"disableAllHooks": true}'
    args = w["claude_args"]
    assert "--setting-sources user" in args and "--strict-mcp-config" in args
    assert "--model ${{ steps.settings.outputs.model }}" in args
    assert "--max-turns ${{ steps.settings.outputs.max_turns }}" in args
    allow = re.search(r'--allowedTools "([^"]+)"', args)
    assert allow and allow.group(1).split(",") == [
        "Read(./review-input/**)", "Read(./.if-fork/config.yaml)",
        "Edit(./review-output/resolution.json)",
    ]
    deny = re.search(r'--disallowedTools "([^"]+)"', args)
    assert deny
    for rule in ("Bash", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task",
                 "Glob", "Grep", "Read(//proc/**)", "Read(//home/runner/work/_temp/**)",
                 "Edit(//home/runner/work/_temp/**)"):
        assert rule in deny.group(1).split(","), rule
    settings = _step(job, "settings")["run"]
    assert "['claude_models']['resolution_review']" in settings
    assert "['claude_limits']['max_turns_resolution_review']" in settings
    assert CONFIG["claude_models"]["resolution_review"] == "claude-fable-5-1"
    prompt = _step(job, "load-prompt")["run"]
    assert "cat .if-fork/prompts/resolution-review.md" in prompt
    assert (REPO_ROOT / ".if-fork" / "prompts" / "resolution-review.md").exists()
    hash_step = _step(job, "verdict")
    assert hash_step["if"] == "always()" and "sha256sum" in hash_step["run"]
    assert job["outputs"]["verdict_sha256"] == "${{ steps.verdict.outputs.sha256 }}"
    assert job["outputs"]["upload"] == "${{ steps.upload.outcome }}"


def test_record_checks_origin_and_records_before_publishing(workflow: dict) -> None:
    job = workflow["jobs"]["record"]
    assert job["environment"] == "bot-credentials"
    assert job["if"] == "always()"
    names = [s.get("name") for s in job["steps"]]
    verdict = _step(job, "Download the verdict")
    assert verdict["with"]["name"] == "resolution-verdict.json"
    scan = _step(job, "Compute the diff to scan")["run"]
    assert '"$BRANCH_SHA $PARENTS"' in scan
    # A sync scans what the resolution wrote: result against git's own merge.
    assert 'git merge-tree --write-tree "$BASE_SHA" "$UPSTREAM_SHA"' in scan
    assert 'git diff --no-color --no-ext-diff --no-renames "$AUTO" "$BRANCH_SHA"' in scan
    decide = _step(job, "decide")
    assert decide["env"]["VERDICT_SHA256"] == "${{ needs.review.outputs.verdict_sha256 }}"
    assert decide["env"]["UPLOAD"] == "${{ needs.review.outputs.upload }}"
    run = decide["run"]
    assert "python tools/record-resolution-verdict.py" in run
    assert "--ledger .if-fork/patch-ledger.jsonl" in run
    assert 'SCAN_ARGS=(--added-only)' in run
    assert '--branch-sha "$BRANCH_SHA"' in run and '--head-sha "$UPSTREAM_SHA"' in run
    assert names.index("Record in ledger") < names.index("Publish the decision")
    ledger = _step(job, "Record in ledger")["run"]
    assert "--events-file" in ledger and "--push" in ledger
    publish = _step(job, "Publish the decision")["run"]
    assert "approved|retry|give_up" in publish
    assert job["outputs"]["next_action"] == "${{ steps.record.outputs.next_action }}"
