"""Tests for .github/workflows/resolution-review.yml, the Fable review of a
conflict resolution that pr-ingestion.yml and nightly-rebase.yml call, and
resolution-reviewer.yml, the review try it calls up to three times.

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
REVIEWER_PATH = REPO_ROOT / ".github" / "workflows" / "resolution-reviewer.yml"
CONFIG = yaml.safe_load((REPO_ROOT / ".if-fork" / "config.yaml").read_text())


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text())
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def reviewer() -> dict:
    return _load(REVIEWER_PATH)


@pytest.fixture(scope="module")
def jobs(workflow: dict, reviewer: dict) -> dict:
    """Every job with steps: prepare and review (one try) and record."""
    return {**reviewer["jobs"], "record": workflow["jobs"]["record"]}


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
    assert set(workflow["jobs"]) == {"try-1", "try-2", "try-3", "record"}


def test_at_most_three_tries_each_after_the_one_before_left_no_verdict(
    workflow: dict, reviewer: dict
) -> None:
    delays = {}
    for n in (1, 2, 3):
        job = workflow["jobs"][f"try-{n}"]
        assert job["uses"] == "./.github/workflows/resolution-reviewer.yml"
        assert job["secrets"] == "inherit"
        assert job["with"]["try"] == str(n)
        assert job["with"]["branch_sha"] == "${{ inputs.branch_sha }}"
        delays[n] = int(job["with"]["delay_seconds"])
        if n > 1:
            assert job["needs"] == f"try-{n - 1}"
            assert job["if"] == (
                f"${{{{ !cancelled() && needs.try-{n - 1}.outputs.done != 'true' }}}}"
            )
    assert delays[1] == 0 < delays[2] < delays[3] <= 900
    call = reviewer["on"]["workflow_call"]
    assert set(call["inputs"]) == {
        "kind", "pr_number", "upstream_sha", "base_sha", "branch_sha", "branch",
        "conflict_files", "try", "delay_seconds",
    }
    assert call["outputs"]["done"]["value"] == "${{ jobs.review.outputs.done }}"
    assert call["outputs"]["novel_sha256"]["value"] == "${{ jobs.prepare.outputs.novel_sha256 }}"
    assert reviewer["permissions"] == {"contents": "read"}
    wait = reviewer["jobs"]["prepare"]["steps"][0]
    assert wait["name"] == "Wait before a retry" and '[ "$DELAY" -le 900 ]' in wait["run"]


def test_done_needs_a_well_formed_verdict(reviewer: dict) -> None:
    job = reviewer["jobs"]["review"]
    done = _step(job, "done")
    assert done["if"] == "always()"
    assert '.reviewer_id == $id' in done["run"]
    assert '(.verdict == "approve" or .verdict == "reject")' in done["run"]
    assert '[ "$UPLOAD" = success ]' in done["run"]
    assert job["outputs"]["done"] == "${{ steps.done.outputs.done }}"


def test_inputs_reach_shell_only_through_env(jobs: dict) -> None:
    for name, job in jobs.items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), f"{name}: {step.get('name')}"
            if "run" in step:
                r = subprocess.run(["bash", "-n"], input=step["run"], text=True,
                                   capture_output=True)
                assert r.returncode == 0, f"{name}: {step.get('name')}: {r.stderr}"


@pytest.mark.parametrize("job_name", ["prepare", "review", "record"])
def test_every_job_validates_inputs_first(jobs: dict, job_name: str) -> None:
    steps = jobs[job_name]["steps"]
    names = [s.get("name") for s in steps]
    # prepare may wait first (a retry's delay); nothing else comes before.
    assert names.index("Validate inputs") <= (1 if job_name == "prepare" else 0)
    run = steps[names.index("Validate inputs")]["run"]
    for check in ('"$PR_NUMBER" =~ ^[0-9]+$', '"$UPSTREAM_SHA" =~ ^[0-9a-f]{40}$',
                  '"$BASE_SHA" =~ ^[0-9a-f]{40}$', '"$BRANCH_SHA" =~ ^[0-9a-f]{40}$'):
        assert check in run, (job_name, check)


@pytest.mark.parametrize("job_name", ["prepare", "record"])
def test_branch_must_match_the_kind(jobs: dict, job_name: str) -> None:
    run = next(s for s in jobs[job_name]["steps"] if s.get("name") == "Validate inputs")["run"]
    assert '[ "$BRANCH" = "claude/cherry-pick-pr-$PR_NUMBER" ]' in run
    assert '[ "$KIND" = sync ] && [ "$PR_NUMBER" = 0 ] && [ "$BRANCH" = if/staging ]' in run


def test_prepare_holds_no_secrets(workflow: dict, reviewer: dict) -> None:
    job = reviewer["jobs"]["prepare"]
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
    assert '--final-dir "$OUT/final"' in run
    # record checks the report against this job output before the novel cap.
    assert 'echo "novel_sha256=$(sha256sum "$OUT/novel-lines.json"' in run
    assert job["outputs"]["novel_sha256"] == "${{ steps.input.outputs.novel_sha256 }}"
    record = workflow["jobs"]["record"]
    decide = _step(record, "Apply the rules")
    assert "novel_sha256: ($t.outputs.novel_sha256" in decide["run"]
    upload = next(s for s in job["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["name"] == "resolution-review-input-${{ inputs.try }}"


def test_reviewer_is_locked_down(reviewer: dict) -> None:
    job = reviewer["jobs"]["review"]
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
    assert download["with"] == {
        "name": "resolution-review-input-${{ inputs.try }}", "path": "review-input"
    }
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
    text = (REPO_ROOT / ".if-fork" / "prompts" / "resolution-review.md").read_text()
    # The reviewer must not lean on a clean novel-lines count.
    assert "`novel_count: 0` does not mean the resolution is safe" in text
    assert "reordered_lines" in text and "duplicated_lines" in text
    hash_step = _step(job, "verdict")
    assert hash_step["if"] == "always()" and "sha256sum" in hash_step["run"]
    assert job["outputs"]["verdict_sha256"] == "${{ steps.verdict.outputs.sha256 }}"
    assert job["outputs"]["upload"] == "${{ steps.upload.outcome }}"
    upload = _step(job, "upload")
    assert upload["with"]["name"] == "resolution-verdict-${{ inputs.try }}.json"


def test_record_checks_origin_and_records_before_publishing(workflow: dict) -> None:
    job = workflow["jobs"]["record"]
    assert job["environment"] == "bot-credentials"
    # A cancelled run is no verdict and must not use up an attempt.
    assert job["if"] == "${{ !cancelled() }}"
    assert job["needs"] == ["try-1", "try-2", "try-3"]
    names = [s.get("name") for s in job["steps"]]
    for n in (1, 2, 3):
        verdict = _step(job, f"Download try {n}'s verdict")
        assert verdict["with"]["name"] == f"resolution-verdict-{n}.json"
        assert verdict["continue-on-error"] is True
        review_input = _step(job, f"Download try {n}'s review input")
        assert review_input["with"]["name"] == f"resolution-review-input-{n}"
    scan = _step(job, "Compute the diff to scan")["run"]
    assert '"$BRANCH_SHA $PARENTS"' in scan
    # A sync scans what the resolution wrote: result against git's own merge.
    assert 'git merge-tree --write-tree "$BASE_SHA" "$UPSTREAM_SHA"' in scan
    assert 'git diff --no-color --no-ext-diff --no-renames "$AUTO" "$BRANCH_SHA"' in scan
    decide = _step(job, "decide")
    # Results and hashes come from the tries' job outputs.
    assert decide["env"]["TRIES_JSON"] == "${{ toJSON(needs) }}"
    run = decide["run"]
    assert '--tries "$RUNNER_TEMP/tries.json"' in run
    for field in ("result: ($t.result", "verdict_sha256: ($t.outputs.verdict_sha256",
                  "upload: ($t.outputs.upload"):
        assert field in run, field
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
