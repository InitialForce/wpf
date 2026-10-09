"""
Tests for .github/workflows/pr-ingestion.yml

Validates YAML structure, job dependencies, gate conditions, and
security invariants — without executing the workflow.

Structural assertions (>= 18):
  1.  YAML parses cleanly
  2.  repository_dispatch trigger with correct type
  3.  workflow_dispatch trigger with pr_number and head_sha inputs
  4.  All 7 jobs present
  5.  Needs chain is correct (gate → pre-flight → sha-smuggling-check → cherry-pick
      → [resolve-conflicts → finalize] → open-pr). Build runs in build.yml on the PR.
  6.  sha-smuggling-check is NOT a direct dependency of cherry-pick (it must come BEFORE)
  7.  SHA-smuggling check job exists and references ledger-event.py
  8.  tools/cherry-pick-pre-flight.sh is referenced
  9.  tools/check-denylist.py is referenced
  10. PR targets if/main and no merge happens here (automerge.yml merges on green)
  11. Branch is cut from if/main; conflicts on denylisted files skip Claude
  12. No reference to secrets.GH_APP_TOKEN (must use GH App inline token)
  13. Concurrency group references pr_number
  14. Permissions include contents:write, pull-requests:write, issues:write, id-token:write
  15. cherry_picked ledger event is emitted in open-pr job
  16. gate job passes requested_action: cherry-pick (CRIT-1)
  17. All downstream jobs guard on needs.gate.outputs.proceed == 'true' (CRIT-1)
  18. SHA-smuggling check reads from patch-ledger.jsonl (HIGH-3)
  19. Denylist check uses full diff range origin/if/main...HEAD (HIGH-4)
  20. open-pr declares cherry-pick in needs (HIGH-5)
  21. An existing PR for the branch is reused
  22. Canonical ledger CLI args (--head-sha, --actor, --details-json, --push) are used (CRIT-2)
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pr-ingestion.yml"

EXPECTED_JOBS = {
    "gate",
    "pre-flight",
    "sha-smuggling-check",
    "cherry-pick",
    "resolve-conflicts",
    "finalize",
    "open-pr",
}

# Expected linear needs chain (each job → its direct predecessor(s))
EXPECTED_NEEDS: dict[str, set[str]] = {
    "gate": set(),
    "pre-flight": {"gate"},
    "sha-smuggling-check": {"pre-flight"},
    "cherry-pick": {"sha-smuggling-check"},
    "resolve-conflicts": {"cherry-pick"},
    "finalize": {"resolve-conflicts"},
    "open-pr": {"cherry-pick", "finalize"},
}

# Jobs that must guard on the gate proceed output (all downstream jobs)
PROCEED_GUARDED_JOBS = {
    "pre-flight",
    "sha-smuggling-check",
    "cherry-pick",
    "resolve-conflicts",
    "finalize",
    "open-pr",
}


def _normalize_on_key(doc: dict) -> dict:
    """
    PyYAML parses bare YAML key ``on`` as Python boolean ``True`` (YAML 1.1).
    Normalize it back to the string ``"on"`` for consistent test access.
    """
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


def _raw_text() -> str:
    """Return raw workflow file text for pattern searches."""
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Parse and return the pr-ingestion.yml document."""
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


def _normalize_needs(needs_value) -> set[str]:
    """Normalize a YAML needs value to a set of strings."""
    if needs_value is None:
        return set()
    if isinstance(needs_value, str):
        return {needs_value}
    return set(needs_value)


# ---------------------------------------------------------------------------
# 1. YAML parses cleanly
# ---------------------------------------------------------------------------

def test_yaml_parses() -> None:
    """Workflow YAML must parse without error."""
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert doc is not None, "YAML produced None — file is likely empty"
    assert isinstance(doc, dict), f"Expected dict at top level, got {type(doc)}"


# ---------------------------------------------------------------------------
# 2. Trigger: repository_dispatch with correct type
# ---------------------------------------------------------------------------

def test_repository_dispatch_trigger(workflow: dict) -> None:
    """Must have repository_dispatch trigger with type pr-ingestion-requested."""
    on = workflow.get("on", {})
    assert "repository_dispatch" in on, (
        "Workflow must have a repository_dispatch trigger"
    )
    rd = on["repository_dispatch"]
    types = rd.get("types", [])
    assert "pr-ingestion-requested" in types, (
        f"Expected type 'pr-ingestion-requested' in {types}"
    )


# ---------------------------------------------------------------------------
# 3. Trigger: workflow_dispatch with required inputs
# ---------------------------------------------------------------------------

def test_workflow_dispatch_inputs(workflow: dict) -> None:
    """workflow_dispatch must define pr_number and head_sha inputs."""
    on = workflow.get("on", {})
    assert "workflow_dispatch" in on, "Workflow must have workflow_dispatch trigger"
    wd = on["workflow_dispatch"] or {}
    inputs = wd.get("inputs", {}) or {}
    assert "pr_number" in inputs, (
        f"workflow_dispatch must have pr_number input; found: {list(inputs)}"
    )
    assert "head_sha" in inputs, (
        f"workflow_dispatch must have head_sha input; found: {list(inputs)}"
    )


# ---------------------------------------------------------------------------
# 4. All 6 jobs present
# ---------------------------------------------------------------------------

def test_all_jobs_present(workflow: dict) -> None:
    """Workflow must define all required jobs."""
    jobs = workflow.get("jobs", {})
    missing = EXPECTED_JOBS - set(jobs.keys())
    assert not missing, f"Missing jobs: {missing}. Found: {set(jobs.keys())}"


# ---------------------------------------------------------------------------
# 5. Needs chain is correct for every job
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("job_name,expected_needs", [
    ("gate", set()),
    ("pre-flight", {"gate"}),
    ("sha-smuggling-check", {"pre-flight"}),
    ("cherry-pick", {"sha-smuggling-check"}),
    ("resolve-conflicts", {"cherry-pick"}),
    ("finalize", {"cherry-pick", "resolve-conflicts"}),
    ("open-pr", {"cherry-pick", "finalize"}),
])
def test_needs_chain(workflow: dict, job_name: str, expected_needs: set) -> None:
    """Each job must declare the correct needs dependencies."""
    jobs = workflow.get("jobs", {})
    assert job_name in jobs, f"Job '{job_name}' not found in workflow"
    actual_needs = _normalize_needs(jobs[job_name].get("needs"))
    assert expected_needs.issubset(actual_needs), (
        f"Job '{job_name}' needs {actual_needs!r} but expected at least {expected_needs!r}"
    )


# ---------------------------------------------------------------------------
# 6. SHA-smuggling check comes BEFORE cherry-pick in needs chain
# ---------------------------------------------------------------------------

def test_sha_smuggling_check_before_cherry_pick(workflow: dict) -> None:
    """cherry-pick must list sha-smuggling-check in its needs (runs after it)."""
    jobs = workflow.get("jobs", {})
    cherry_needs = _normalize_needs(jobs.get("cherry-pick", {}).get("needs"))
    assert "sha-smuggling-check" in cherry_needs, (
        f"'cherry-pick' must need 'sha-smuggling-check'; got needs={cherry_needs!r}"
    )


# ---------------------------------------------------------------------------
# 7. SHA-smuggling check job exists and references escalated ledger event
# ---------------------------------------------------------------------------

def test_sha_smuggling_job_has_escalated_event(workflow: dict) -> None:
    """sha-smuggling-check job must reference the escalated ledger event."""
    raw = _raw_text()
    # The sha-smuggling-check job should call ledger-event.py with --event escalated
    assert "escalated" in raw, (
        "sha-smuggling-check must emit an 'escalated' ledger event on SHA mismatch"
    )
    assert "sha_smuggling" in raw or "sha-smuggling" in raw, (
        "Workflow must mention sha_smuggling in the escalation details"
    )


# ---------------------------------------------------------------------------
# 8. cherry-pick-pre-flight.sh is referenced
# ---------------------------------------------------------------------------

def test_cherry_pick_pre_flight_referenced() -> None:
    """tools/cherry-pick-pre-flight.sh must be called in the workflow."""
    raw = _raw_text()
    assert "cherry-pick-pre-flight.sh" in raw, (
        "Workflow must reference tools/cherry-pick-pre-flight.sh"
    )


# ---------------------------------------------------------------------------
# 9. check-denylist.py is referenced
# ---------------------------------------------------------------------------

def test_check_denylist_referenced() -> None:
    """tools/check-denylist.py must be called in the workflow."""
    raw = _raw_text()
    assert "check-denylist.py" in raw, (
        "Workflow must reference tools/check-denylist.py"
    )


# ---------------------------------------------------------------------------
# 10. PR targets if/main; this workflow never merges (automerge.yml does)
# ---------------------------------------------------------------------------

def test_pr_targets_if_main_and_does_not_merge() -> None:
    raw = _raw_text()
    assert "--base if/main" in raw
    assert "if/staging" not in raw, "cherry-picks no longer go through if/staging"
    assert "gh pr merge" not in raw, "automerge.yml merges after Build passes"
    assert "gh workflow run build.yml" not in raw, "Build runs on the PR itself"


# ---------------------------------------------------------------------------
# 11. Branch from if/main; denylisted conflicts never reach Claude
# ---------------------------------------------------------------------------

APPLY_SCRIPT = REPO_ROOT / "tools" / "apply-upstream-pr.sh"
FINISH_SCRIPT = REPO_ROOT / "tools" / "finish-ingestion-branch.sh"


def test_branch_cut_from_if_main(workflow: dict) -> None:
    steps = {s.get("name"): s for s in workflow["jobs"]["cherry-pick"]["steps"]}
    assert 'bash "$TRUSTED/tools/apply-upstream-pr.sh"' in steps["Apply upstream change"]["run"]
    assert 'git checkout -q -B "$BRANCH" "$BASE_SHA"' in APPLY_SCRIPT.read_text()
    assert "--changed-files" in steps["Refuse conflicts on denylisted files"]["run"]
    resolve_if = workflow["jobs"]["resolve-conflicts"]["if"]
    assert "needs.cherry-pick.outputs.conflicts_allowed == 'true'" in resolve_if


def test_reviewed_head_and_commit_denylist_checked() -> None:
    run = APPLY_SCRIPT.read_text()
    assert '"$UP_HEAD" != "$PINNED_HEAD_SHA"' in run
    assert '"$TRUSTED/tools/check-commit-denylist.py"' in run
    assert '"$TRUSTED/.if-fork/config.yaml"' in run


# ---------------------------------------------------------------------------
# Security: the agent job holds no credentials (security round, item 1)
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
    claude = next(s for s in job["steps"] if "claude-code-action" in str(s.get("uses", "")))
    assert claude["with"]["github_token"] == "${{ github.token }}"
    args = claude["with"]["claude_args"]
    # The rules come from tools/prepare-resolver.sh: Edit only on the conflicts.
    assert '--allowedTools "${{ steps.prepare.outputs.allowed }}"' in args
    assert claude["if"] == "steps.prepare.outputs.run == 'true'"
    deny = re.search(r'--disallowedTools "([^"]+)"', args)
    assert deny and "Bash" in deny.group(1).split(","), args
    assert "upload-artifact" in text


def test_finalize_takes_only_conflicted_paths_and_checks_before_push(workflow: dict) -> None:
    steps = {s.get("name"): s for s in workflow["jobs"]["finalize"]["steps"]}
    overlay = steps["Recreate the conflict and apply the resolution"]["run"]
    assert 'bash "$TRUSTED/tools/apply-upstream-pr.sh"' in overlay
    assert 'bash "$TRUSTED/tools/conflict-handoff.sh" apply' in overlay
    assert 'bash "$TRUSTED/tools/finish-ingestion-branch.sh"' in (
        steps["Check the whole diff, add trailers and push"]["run"]
    )


# ---------------------------------------------------------------------------
# Security: checker code and policy come from if/main (item 2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("job_name", ["cherry-pick", "finalize"])
def test_pushing_jobs_use_trusted_checkout(workflow: dict, job_name: str) -> None:
    job = workflow["jobs"][job_name]
    trusted = [s for s in job["steps"] if (s.get("with") or {}).get("path") == "trusted"]
    assert trusted and trusted[0]["with"]["ref"] == "if/main"
    assert trusted[0]["with"]["persist-credentials"] is False
    for step in job["steps"]:
        run = step.get("run", "")
        assert "python3 tools/" not in run and "bash tools/" not in run, step.get("name")
        assert "--config .if-fork/config.yaml" not in run, step.get("name")


def test_finish_runs_denylist_on_branch_diff_before_push() -> None:
    text = FINISH_SCRIPT.read_text()
    assert 'git diff --name-only "$BASE_SHA...HEAD"' in text
    assert '"$TRUSTED/tools/check-denylist.py" --config "$TRUSTED/.if-fork/config.yaml"' in text
    assert text.index("check-denylist.py") < text.index("git push")


# ---------------------------------------------------------------------------
# Security: payload validation and no expressions in shell (items 3 and 6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("job_name", sorted(EXPECTED_JOBS - {"gate"}))
def test_every_job_validates_payload_first(workflow: dict, job_name: str) -> None:
    first = workflow["jobs"][job_name]["steps"][0]
    assert first["name"] == "Validate payload"
    assert '"$PR_NUMBER" =~ ^[0-9]+$' in first["run"]
    assert '"$PINNED_HEAD_SHA" =~ ^[0-9a-f]{40}$' in first["run"]


def test_no_expressions_inside_run_blocks(workflow: dict) -> None:
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), f"{name}: {step.get('name')}"


def test_conflict_prompt_exists() -> None:
    raw = _raw_text()
    for name in re.findall(r"\.if-fork/prompts/([\w.-]+\.md)", raw):
        assert (REPO_ROOT / ".if-fork" / "prompts" / name).exists(), name


# ---------------------------------------------------------------------------
# 12. No stale GH_APP_TOKEN secret reference (R4-6 fix)
# ---------------------------------------------------------------------------

def test_no_stale_gh_app_token_reference() -> None:
    """Workflow must NOT reference secrets.GH_APP_TOKEN (use inline App token instead)."""
    raw = _raw_text()
    assert "secrets.GH_APP_TOKEN" not in raw, (
        "R4-6 fix: no 'secrets.GH_APP_TOKEN' reference allowed — use tibdex/github-app-token inline"
    )


# ---------------------------------------------------------------------------
# 13. Concurrency group references pr_number
# ---------------------------------------------------------------------------

def test_concurrency_group_references_pr_number(workflow: dict) -> None:
    """Concurrency group must reference pr_number for per-PR isolation."""
    concurrency = workflow.get("concurrency", {})
    group = str(concurrency.get("group", ""))
    assert "pr_number" in group or "pr-number" in group.lower() or "pr_number" in group, (
        f"Concurrency group must reference pr_number; got: {group!r}"
    )
    assert concurrency.get("cancel-in-progress") is False, (
        "cancel-in-progress must be false for ingestion workflow"
    )


# ---------------------------------------------------------------------------
# 14. Permissions block
# ---------------------------------------------------------------------------

def test_permissions(workflow: dict) -> None:
    """Workflow must declare required permissions."""
    perms = workflow.get("permissions", {})
    assert perms.get("contents") == "write", f"contents must be write; got {perms.get('contents')}"
    assert perms.get("pull-requests") == "write", (
        f"pull-requests must be write; got {perms.get('pull-requests')}"
    )
    assert perms.get("issues") == "write", f"issues must be write; got {perms.get('issues')}"
    assert perms.get("id-token") == "write", f"id-token must be write; got {perms.get('id-token')}"


# ---------------------------------------------------------------------------
# 15. cherry_picked ledger event emitted in open-pr job
# ---------------------------------------------------------------------------

def test_cherry_picked_event_emitted() -> None:
    """open-pr job must emit the 'cherry_picked' ledger event."""
    raw = _raw_text()
    assert "cherry_picked" in raw, (
        "Workflow must emit a 'cherry_picked' ledger event in the open-pr job"
    )


# ---------------------------------------------------------------------------
# 16. gate job passes requested_action: cherry-pick (CRIT-1)
# ---------------------------------------------------------------------------

def test_gate_passes_requested_action(workflow: dict) -> None:
    """gate job must pass requested_action: cherry-pick to autonomy-check (CRIT-1)."""
    jobs = workflow.get("jobs", {})
    gate = jobs.get("gate", {})
    gate_with = gate.get("with", {}) or {}
    assert gate_with.get("requested_action") == "cherry-pick", (
        f"gate job must pass 'requested_action: cherry-pick' in 'with'; got: {gate_with!r}"
    )


# ---------------------------------------------------------------------------
# 17. All downstream jobs guard on needs.gate.outputs.proceed == 'true' (CRIT-1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("job_name", sorted(PROCEED_GUARDED_JOBS))
def test_downstream_jobs_check_proceed(workflow: dict, job_name: str) -> None:
    """Every downstream job must guard on needs.gate.outputs.proceed == 'true' (CRIT-1)."""
    jobs = workflow.get("jobs", {})
    assert job_name in jobs, f"Job '{job_name}' not found"
    job_if = str(jobs[job_name].get("if", ""))
    assert "needs.gate.outputs.proceed" in job_if, (
        f"Job '{job_name}' must check needs.gate.outputs.proceed in its 'if' condition; "
        f"got: {job_if!r}"
    )
    assert "proceed" in job_if and "'true'" in job_if, (
        f"Job '{job_name}' proceed condition must compare to 'true'; got: {job_if!r}"
    )


# ---------------------------------------------------------------------------
# 18. SHA-smuggling check reads from patch-ledger.jsonl (HIGH-3)
# ---------------------------------------------------------------------------

def test_sha_smuggling_reads_ledger_file(workflow: dict) -> None:
    """The exact (pr_number, head_sha) must have an approved outcome in the ledger."""
    steps = workflow["jobs"]["sha-smuggling-check"]["steps"]
    run = next(s["run"] for s in steps if "check-approval.py" in s.get("run", ""))
    assert "--ledger .if-fork/patch-ledger.jsonl" in run
    assert '--pr-number "$PR_NUMBER" --head-sha "$PINNED_HEAD_SHA"' in run


# ---------------------------------------------------------------------------
# 19. Denylist check uses full diff range origin/if/main...HEAD (HIGH-4)
# ---------------------------------------------------------------------------

def test_denylist_uses_full_diff_range() -> None:
    """Denylist check must diff BASE_SHA...HEAD to catch every commit (HIGH-4)."""
    assert '"$BASE_SHA...HEAD"' in FINISH_SCRIPT.read_text()


# ---------------------------------------------------------------------------
# 20. open-pr declares cherry-pick in needs (HIGH-5)
# ---------------------------------------------------------------------------

def test_open_pr_needs_cherry_pick(workflow: dict) -> None:
    """open-pr must declare cherry-pick in its needs to access branch output (HIGH-5)."""
    jobs = workflow.get("jobs", {})
    open_pr_needs = _normalize_needs(jobs.get("open-pr", {}).get("needs"))
    assert "cherry-pick" in open_pr_needs, (
        f"open-pr must need 'cherry-pick' to access branch output; got needs={open_pr_needs!r}"
    )


# ---------------------------------------------------------------------------
# 21. An existing PR for the branch is reused, label failure is not fatal
# ---------------------------------------------------------------------------

def test_open_pr_reuses_existing_pr(workflow: dict) -> None:
    steps = {s.get("name"): s for s in workflow["jobs"]["open-pr"]["steps"]}
    run = steps["Open internal PR"]["run"]
    assert "gh pr list" in run and 'if [ -n "$PR_NUM" ]' in run
    assert "--add-label automated" in run and "|| echo" in run


# ---------------------------------------------------------------------------
# 22. Canonical ledger CLI args used (CRIT-2)
# ---------------------------------------------------------------------------

def test_ledger_event_canonical_cli_args() -> None:
    """All ledger-event.py calls must use canonical CLI args:
    --head-sha, --actor, --details-json, --push (CRIT-2)."""
    raw = _raw_text()
    assert "--head-sha" in raw, (
        "ledger-event.py calls must include --head-sha (CRIT-2)"
    )
    assert "--actor" in raw, (
        "ledger-event.py calls must include --actor (CRIT-2)"
    )
    assert "--details-json" in raw, (
        "ledger-event.py calls must include --details-json (CRIT-2)"
    )
    assert "--push" in raw, (
        "ledger-event.py calls must include --push (CRIT-2)"
    )


# ---------------------------------------------------------------------------
# Bonus: actionlint integration (skipped if not installed)
# ---------------------------------------------------------------------------

def test_actionlint() -> None:
    """Run actionlint if available; skip gracefully if not installed."""
    actionlint = shutil.which("actionlint")
    if actionlint is None:
        pytest.skip("actionlint not installed — skipping lint check")

    result = subprocess.run(
        [actionlint, str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint found errors:\n{result.stdout}\n{result.stderr}"
    )


def test_run_blocks_are_valid_bash(workflow: dict) -> None:
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if "run" in step:
                r = subprocess.run(
                    ["bash", "-n"], input=step["run"], text=True, capture_output=True
                )
                assert r.returncode == 0, f"{name}: {step.get('name')}: {r.stderr}"


# ---------------------------------------------------------------------------
# Every job works on the if/main commit the cherry-pick job started from
# ---------------------------------------------------------------------------

def test_if_main_commit_is_pinned_across_jobs(workflow: dict) -> None:
    jobs = workflow["jobs"]
    pick = jobs["cherry-pick"]
    assert pick["outputs"]["main_sha"] == "${{ steps.base.outputs.sha }}"
    base = next(s for s in pick["steps"] if s.get("id") == "base")
    assert "git rev-parse origin/if/main" in base["run"]
    for step in pick["steps"]:
        if step.get("id") in ("apply", "finish"):
            assert step["env"]["BASE_SHA"] == "${{ steps.base.outputs.sha }}"
    for name in ("resolve-conflicts", "finalize"):
        job = jobs[name]
        assert job["env"]["BASE_SHA"] == "${{ needs.cherry-pick.outputs.main_sha }}"
        validate = job["steps"][0]["run"]
        assert '"$BASE_SHA" =~ ^[0-9a-f]{40}$' in validate
        refs = [
            s["with"]["ref"]
            for s in job["steps"]
            if "actions/checkout" in s.get("uses", "") and s["with"].get("path") != "trusted"
        ]
        assert refs == ["${{ env.BASE_SHA }}"], name


def test_scripts_refuse_a_missing_base_sha() -> None:
    for script in (APPLY_SCRIPT, FINISH_SCRIPT):
        assert '[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]]' in script.read_text(), script.name


def test_agent_resolution_is_marked_for_a_person(workflow: dict) -> None:
    finish = next(s for s in workflow["jobs"]["finalize"]["steps"] if s.get("id") == "finish")
    assert finish["env"]["CONFLICT_RESOLVED"] == "true"
    # The clean path in cherry-pick must not set it.
    pick_finish = next(
        s for s in workflow["jobs"]["cherry-pick"]["steps"] if s.get("id") == "finish"
    )
    assert "CONFLICT_RESOLVED" not in pick_finish["env"]
    open_pr = next(s for s in workflow["jobs"]["open-pr"]["steps"] if s.get("id") == "open-pr")
    assert open_pr["env"]["CONFLICT_RESOLVED"] == "${{ needs.finalize.outputs.pushed == 'true' }}"
    assert 'bash tools/human-merge-label.sh "$PR_NUM" "$CONFLICT_RESOLVED"' in open_pr["run"]
    assert "A person must merge this PR" in open_pr["run"]
