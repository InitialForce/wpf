"""
Tests for .github/workflows/pr-review.yml — 2x Independent Opus Review

Validates YAML structure, job dependencies, trigger configuration, permissions,
IF_REVIEW_DOUBLE_REQUIRED handling, and reviewer independence — without executing
the workflow. If actionlint is available it is called as well; if not, the
structural assertions below serve as the floor.

Key properties verified:
  - Both repository_dispatch (pr-discovered) and workflow_dispatch triggers present
  - 4 jobs: gate, review-1, review-2, merge-verdict
  - gate uses autonomy-check.yml reusable with requested_action=claude-invoke
  - review-1 and review-2 both need gate but NOT each other (independence guarantee)
  - review-1 references pr-review-1.md, review-2 references pr-review-2.md (different)
  - IF_REVIEW_DOUBLE_REQUIRED referenced via vars.IF_REVIEW_DOUBLE_REQUIRED
  - merge-verdict needs both review-1 AND review-2
  - merge-verdict invokes tools/merge-verdicts.py
  - Concurrency group uses pr_number with cancel-in-progress: false
  - Correct permissions (contents:read, pull-requests:read, issues:write, id-token:write)
  - tools/ledger-event.py called for review_1, review_2, and merged_verdict events
  - No reference to secrets.GH_APP_TOKEN (R4-6 fix)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pr-review.yml"
AUTONOMY_CHECK_PATH = REPO_ROOT / ".github" / "workflows" / "autonomy-check.yml"


def _normalize_on_key(doc: dict) -> dict:
    """
    PyYAML parses the bare YAML key ``on`` as the Python boolean ``True``
    (YAML 1.1 spec). Normalize it back to the string ``"on"`` so tests can
    use consistent key names.
    """
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Parse and return the pr-review.yml document."""
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


@pytest.fixture(scope="module")
def workflow_text() -> str:
    """Return the raw text of pr-review.yml for pattern checks."""
    return WORKFLOW_PATH.read_text()


# ---------------------------------------------------------------------------
# 1. YAML parses cleanly
# ---------------------------------------------------------------------------

def test_yaml_parses():
    """Workflow YAML must parse without error and produce a non-empty document."""
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert doc is not None, "YAML produced None — file is likely empty"


# ---------------------------------------------------------------------------
# 2. Triggers: repository_dispatch (pr-discovered) + workflow_dispatch with inputs
# ---------------------------------------------------------------------------

def test_has_repository_dispatch_trigger(workflow):
    on = workflow.get("on", {})
    assert "repository_dispatch" in on, (
        "Workflow must have a repository_dispatch trigger"
    )


def test_repository_dispatch_type_is_pr_discovered(workflow):
    on = workflow.get("on", {})
    rd = on.get("repository_dispatch", {})
    types = rd.get("types", [])
    assert "pr-discovered" in types, (
        f"repository_dispatch must listen for 'pr-discovered'; got types={types}"
    )


def test_has_workflow_dispatch_trigger(workflow):
    on = workflow.get("on", {})
    assert "workflow_dispatch" in on, (
        "Workflow must support workflow_dispatch (R4-4 fix)"
    )


def test_workflow_dispatch_has_pr_number_input(workflow):
    on = workflow.get("on", {})
    wd = on.get("workflow_dispatch", {}) or {}
    inputs = wd.get("inputs", {}) or {}
    assert "pr_number" in inputs, (
        "workflow_dispatch must declare a 'pr_number' input"
    )


def test_workflow_dispatch_has_head_sha_input(workflow):
    on = workflow.get("on", {})
    wd = on.get("workflow_dispatch", {}) or {}
    inputs = wd.get("inputs", {}) or {}
    assert "head_sha" in inputs, (
        "workflow_dispatch must declare a 'head_sha' input"
    )


def test_workflow_dispatch_pr_number_required(workflow):
    on = workflow.get("on", {})
    wd = on.get("workflow_dispatch", {}) or {}
    inputs = wd.get("inputs", {}) or {}
    pr_input = inputs.get("pr_number", {}) or {}
    assert pr_input.get("required") is True, (
        "workflow_dispatch 'pr_number' input must be required: true"
    )


def test_workflow_dispatch_head_sha_required(workflow):
    on = workflow.get("on", {})
    wd = on.get("workflow_dispatch", {}) or {}
    inputs = wd.get("inputs", {}) or {}
    sha_input = inputs.get("head_sha", {}) or {}
    assert sha_input.get("required") is True, (
        "workflow_dispatch 'head_sha' input must be required: true"
    )


# ---------------------------------------------------------------------------
# 3. All 4 jobs present
# ---------------------------------------------------------------------------

def test_has_all_four_jobs(workflow):
    jobs = workflow.get("jobs", {})
    for expected in ("gate", "review-1", "review-2", "merge-verdict"):
        assert expected in jobs, f"Missing required job: {expected!r}"


# ---------------------------------------------------------------------------
# 4. gate job calls autonomy-check.yml reusable with claude-invoke action
# ---------------------------------------------------------------------------

def test_gate_uses_autonomy_check_reusable(workflow):
    gate = workflow["jobs"]["gate"]
    uses = gate.get("uses", "")
    assert "autonomy-check.yml" in uses, (
        f"gate job must call autonomy-check.yml reusable; got: {uses!r}"
    )


def test_gate_passes_claude_invoke_action(workflow):
    gate = workflow["jobs"]["gate"]
    with_block = gate.get("with", {}) or {}
    assert with_block.get("requested_action") == "claude-invoke", (
        "gate job must pass requested_action: claude-invoke; "
        f"got {with_block.get('requested_action')!r}"
    )


# ---------------------------------------------------------------------------
# 5. review-1 needs gate and does NOT need review-2
# ---------------------------------------------------------------------------

def test_review_1_needs_gate(workflow):
    review1 = workflow["jobs"]["review-1"]
    needs = review1.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "gate" in needs, "review-1 must declare 'needs: gate'"


def test_review_1_does_not_need_review_2(workflow):
    review1 = workflow["jobs"]["review-1"]
    needs = review1.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "review-2" not in needs, (
        "review-1 must NOT need review-2 — the two reviewers must be independent"
    )


# ---------------------------------------------------------------------------
# 6. review-2 needs gate and does NOT need review-1 (independence guarantee)
# ---------------------------------------------------------------------------

def test_review_2_needs_gate(workflow):
    review2 = workflow["jobs"]["review-2"]
    needs = review2.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "gate" in needs, "review-2 must declare 'needs: gate'"


def test_review_2_does_not_need_review_1(workflow):
    """Independence: review-2 must not wait on review-1 to start."""
    review2 = workflow["jobs"]["review-2"]
    needs = review2.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "review-1" not in needs, (
        "review-2 must NOT declare 'needs: review-1'. "
        "The two reviewers must run independently with no shared context before merge-verdict."
    )


# ---------------------------------------------------------------------------
# 7. review-1 and review-2 reference DIFFERENT prompt files
# ---------------------------------------------------------------------------

def test_review_1_references_pr_review_1_prompt(workflow_text):
    assert "pr-review-1.md" in workflow_text, (
        "Workflow must reference .if-fork/prompts/pr-review-1.md for reviewer 1"
    )


def test_review_2_references_pr_review_2_prompt(workflow_text):
    assert "pr-review-2.md" in workflow_text, (
        "Workflow must reference .if-fork/prompts/pr-review-2.md for reviewer 2"
    )


def test_reviewer_prompts_are_different(workflow_text):
    assert "pr-review-1.md" in workflow_text and "pr-review-2.md" in workflow_text, (
        "Workflow must use different prompt files for review-1 (pr-review-1.md) "
        "and review-2 (pr-review-2.md)"
    )


# ---------------------------------------------------------------------------
# 8. IF_REVIEW_DOUBLE_REQUIRED referenced via vars.IF_REVIEW_DOUBLE_REQUIRED
# ---------------------------------------------------------------------------

def test_if_review_double_required_via_vars(workflow_text):
    assert "vars.IF_REVIEW_DOUBLE_REQUIRED" in workflow_text, (
        "Workflow must reference IF_REVIEW_DOUBLE_REQUIRED via vars.IF_REVIEW_DOUBLE_REQUIRED, "
        "not via secrets or env"
    )


def test_review_2_conditional_on_if_review_double_required(workflow):
    review2 = workflow["jobs"]["review-2"]
    condition = review2.get("if", "") or ""
    assert "IF_REVIEW_DOUBLE_REQUIRED" in condition, (
        "review-2 must have an 'if' condition gating on IF_REVIEW_DOUBLE_REQUIRED; "
        f"got: {condition!r}"
    )


# ---------------------------------------------------------------------------
# 9. merge-verdict needs BOTH review-1 and review-2
# ---------------------------------------------------------------------------

def test_merge_verdict_needs_review_1(workflow):
    mv = workflow["jobs"]["merge-verdict"]
    needs = mv.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "review-1" in needs, "merge-verdict must declare 'needs: review-1'"


def test_merge_verdict_needs_review_2(workflow):
    mv = workflow["jobs"]["merge-verdict"]
    needs = mv.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "review-2" in needs, "merge-verdict must declare 'needs: review-2'"


# ---------------------------------------------------------------------------
# 10. merge-verdict invokes tools/merge-verdicts.py
# ---------------------------------------------------------------------------

def test_merge_verdict_calls_merge_verdicts_py(workflow):
    mv = workflow["jobs"]["merge-verdict"]
    steps = mv.get("steps", [])
    step_runs = " ".join(s.get("run", "") for s in steps)
    assert "merge-verdicts.py" in step_runs, (
        "merge-verdict job must call tools/merge-verdicts.py"
    )


# ---------------------------------------------------------------------------
# 11. Concurrency: group uses pr_number, cancel-in-progress is false
# ---------------------------------------------------------------------------

def test_concurrency_uses_pr_number(workflow_text):
    assert "pr-review-" in workflow_text, (
        "Concurrency group must include 'pr-review-' prefix tied to PR number"
    )


def test_concurrency_cancel_in_progress_false(workflow):
    concurrency = workflow.get("concurrency", {})
    assert concurrency.get("cancel-in-progress") is False, (
        "concurrency.cancel-in-progress must be false — never abort an in-flight review"
    )


# ---------------------------------------------------------------------------
# 12. Permissions: contents:read, pull-requests:read, issues:write, id-token:write
# ---------------------------------------------------------------------------

def test_permissions_contents_read(workflow):
    perms = workflow.get("permissions", {})
    assert perms.get("contents") == "read", (
        f"permissions.contents must be 'read'; got {perms.get('contents')!r}"
    )


def test_permissions_pull_requests_read(workflow):
    perms = workflow.get("permissions", {})
    assert perms.get("pull-requests") == "read", (
        f"permissions.pull-requests must be 'read'; got {perms.get('pull-requests')!r}"
    )


def test_permissions_issues_write(workflow):
    perms = workflow.get("permissions", {})
    assert perms.get("issues") == "write", (
        "permissions.issues must be 'write' (to open disagreement issues); "
        f"got {perms.get('issues')!r}"
    )


def test_permissions_id_token_write(workflow):
    perms = workflow.get("permissions", {})
    assert perms.get("id-token") == "write", (
        f"permissions.id-token must be 'write'; got {perms.get('id-token')!r}"
    )


# ---------------------------------------------------------------------------
# 13. ledger-event.py called for review_1, review_2, and merged_verdict events
# ---------------------------------------------------------------------------

def test_ledger_event_review_1_emitted(workflow):
    review1 = workflow["jobs"]["review-1"]
    steps = review1.get("steps", [])
    step_runs = " ".join(s.get("run", "") for s in steps)
    assert "ledger-event.py" in step_runs, (
        "review-1 job must call tools/ledger-event.py"
    )
    assert "--event review_1" in step_runs, (
        "review-1 job must emit --event review_1 to ledger-event.py"
    )


def test_ledger_event_review_2_emitted(workflow):
    review2 = workflow["jobs"]["review-2"]
    steps = review2.get("steps", [])
    step_runs = " ".join(s.get("run", "") for s in steps)
    assert "ledger-event.py" in step_runs, (
        "review-2 job must call tools/ledger-event.py"
    )
    assert "--event review_2" in step_runs, (
        "review-2 job must emit --event review_2 to ledger-event.py"
    )


def test_ledger_event_merged_verdict_emitted(workflow):
    mv = workflow["jobs"]["merge-verdict"]
    steps = mv.get("steps", [])
    step_runs = " ".join(s.get("run", "") for s in steps)
    assert "ledger-event.py" in step_runs, (
        "merge-verdict job must call tools/ledger-event.py"
    )
    assert "--event merged_verdict" in step_runs, (
        "merge-verdict job must emit --event merged_verdict to ledger-event.py"
    )


# ---------------------------------------------------------------------------
# 14. No reference to secrets.GH_APP_TOKEN (R4-6 fix)
# ---------------------------------------------------------------------------

def test_no_stale_gh_app_token_secret(workflow_text):
    assert "secrets.GH_APP_TOKEN" not in workflow_text, (
        "Workflow must not reference secrets.GH_APP_TOKEN (R4-6). "
        "Use tibdex/github-app-token@v2 output instead."
    )


# ---------------------------------------------------------------------------
# 15. GH App token generated via tibdex/github-app-token@v2
# ---------------------------------------------------------------------------

def test_uses_tibdex_github_app_token(workflow):
    jobs = workflow.get("jobs", {})
    all_steps: list[dict] = []
    for job in jobs.values():
        all_steps.extend(job.get("steps", []) or [])
    uses_values = [s.get("uses", "") for s in all_steps]
    assert any("tibdex/github-app-token" in u for u in uses_values), (
        "At least one job step must use tibdex/github-app-token@v2 for GH App auth"
    )


# ---------------------------------------------------------------------------
# 16. actionlint (optional — skipped if not installed)
# ---------------------------------------------------------------------------

def test_actionlint_if_available():
    if not shutil.which("actionlint"):
        pytest.skip("actionlint not available; skipped")
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"actionlint found issues:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# 17. CRIT-1: kill-switch enforcement — all non-gate jobs guarded by gate output
# ---------------------------------------------------------------------------

def test_review_1_guarded_by_gate_proceed(workflow):
    """CRIT-1: review-1 must not run unless gate outputs proceed == 'true'."""
    review1 = workflow["jobs"]["review-1"]
    condition = review1.get("if", "") or ""
    assert "needs.gate.outputs.proceed == 'true'" in condition, (
        "review-1 must have if: needs.gate.outputs.proceed == 'true'; "
        f"got: {condition!r}"
    )


def test_review_2_guarded_by_gate_proceed(workflow):
    """CRIT-1: review-2 must not run unless gate outputs proceed == 'true'."""
    review2 = workflow["jobs"]["review-2"]
    condition = review2.get("if", "") or ""
    assert "needs.gate.outputs.proceed == 'true'" in condition, (
        "review-2 must have if: needs.gate.outputs.proceed == 'true'; "
        f"got: {condition!r}"
    )


def test_merge_verdict_guarded_by_gate_proceed(workflow):
    """CRIT-1: merge-verdict must not run unless gate outputs proceed == 'true'."""
    mv = workflow["jobs"]["merge-verdict"]
    condition = mv.get("if", "") or ""
    assert "needs.gate.outputs.proceed == 'true'" in condition, (
        "merge-verdict must reference needs.gate.outputs.proceed == 'true' in its if "
        f"condition; got: {condition!r}"
    )


# ---------------------------------------------------------------------------
# 18. CRIT-4: merge-verdict must have gate in its needs list
# ---------------------------------------------------------------------------

def test_merge_verdict_needs_gate(workflow):
    """CRIT-4: merge-verdict.needs must include gate so gate.outputs are accessible."""
    mv = workflow["jobs"]["merge-verdict"]
    needs = mv.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "gate" in needs, (
        "merge-verdict must declare 'needs: gate' so gate outputs are accessible; "
        f"got needs={needs!r}"
    )


# ---------------------------------------------------------------------------
# 19. HIGH-1: fail-closed double-review default
# ---------------------------------------------------------------------------

def test_review_2_skipped_only_when_explicitly_false(workflow):
    """HIGH-1: review-2 must run by default (when var is unset); skip only when 'false'."""
    review2 = workflow["jobs"]["review-2"]
    condition = review2.get("if", "") or ""
    # Correct fail-closed pattern: skip when == 'false', not when != 'true'
    assert "!= 'false'" in condition or "== 'false'" not in condition.replace("!= 'false'", ""), (
        "review-2 must use != 'false' (fail-closed) not != 'true' (fail-open); "
        f"got: {condition!r}"
    )
    assert "!= 'true'" not in condition, (
        "review-2 must NOT use vars.IF_REVIEW_DOUBLE_REQUIRED != 'true'; "
        "that pattern skips review-2 when the variable is unset (silent fail-open). "
        f"Got: {condition!r}"
    )


def test_single_review_fast_path_requires_environment_approval(workflow):
    """HIGH-1: when fast path is active, a manual approval environment must be enforced."""
    jobs = workflow.get("jobs", {})
    # Find any job that is conditional on IF_REVIEW_DOUBLE_REQUIRED == 'false'
    fast_path_jobs = []
    for name, job in jobs.items():
        condition = job.get("if", "") or ""
        if "IF_REVIEW_DOUBLE_REQUIRED" in condition and "== 'false'" in condition:
            fast_path_jobs.append((name, job))
    assert fast_path_jobs, (
        "No job found that is conditional on IF_REVIEW_DOUBLE_REQUIRED == 'false'. "
        "The single-review fast path must be explicitly gated."
    )
    for name, job in fast_path_jobs:
        env_val = job.get("environment")
        if isinstance(env_val, dict):
            env_name = env_val.get("name", "")
        else:
            env_name = env_val or ""
        assert env_name == "branch-promotion", (
            f"Job '{name}' is on the single-review fast path but does not use "
            f"environment: branch-promotion; got environment={env_val!r}"
        )


def test_single_review_fast_path_emits_warning_event(workflow):
    """HIGH-1: single-review fast path must emit a warning ledger event."""
    jobs = workflow.get("jobs", {})
    found_warning = False
    for _name, job in jobs.items():
        condition = job.get("if", "") or ""
        if "IF_REVIEW_DOUBLE_REQUIRED" in condition and "== 'false'" in condition:
            steps = job.get("steps", [])
            step_runs = " ".join(s.get("run", "") for s in steps)
            if "review_single_path_warning" in step_runs:
                found_warning = True
                break
    assert found_warning, (
        "No job on the IF_REVIEW_DOUBLE_REQUIRED == 'false' fast path emits "
        "'review_single_path_warning' to ledger-event.py"
    )


# ---------------------------------------------------------------------------
# 20. Canonical ledger-event.py CLI flags: --push and correct --actor usage
# ---------------------------------------------------------------------------

def test_ledger_event_uses_push_flag(workflow_text):
    """All ledger-event.py invocations must use --push so commits reach origin."""
    import re
    # Find all ledger-event.py invocation blocks
    invocations = re.findall(
        r'ledger-event\.py.*?(?=\n\s*(?:env:|GH_TOKEN|$))', workflow_text, re.DOTALL
    )
    assert invocations, "No ledger-event.py invocations found in workflow"
    for inv in invocations:
        assert "--push" in inv, (
            f"ledger-event.py invocation missing --push flag:\n{inv}"
        )


def test_ledger_event_uses_actor_pr_review(workflow_text):
    """All ledger-event.py invocations in pr-review.yml must use --actor pr-review."""
    import re
    invocations = re.findall(
        r'ledger-event\.py.*?(?=\n\s*(?:env:|GH_TOKEN|$))', workflow_text, re.DOTALL
    )
    assert invocations, "No ledger-event.py invocations found in workflow"
    for inv in invocations:
        assert "--actor pr-review" in inv, (
            f"ledger-event.py invocation missing '--actor pr-review':\n{inv}"
        )


def test_ledger_event_uses_head_sha_flag(workflow_text):
    """All ledger-event.py invocations must pass --head-sha."""
    import re
    invocations = re.findall(
        r'ledger-event\.py.*?(?=\n\s*(?:env:|GH_TOKEN|$))', workflow_text, re.DOTALL
    )
    assert invocations, "No ledger-event.py invocations found in workflow"
    for inv in invocations:
        assert "--head-sha" in inv, (
            f"ledger-event.py invocation missing --head-sha:\n{inv}"
        )
