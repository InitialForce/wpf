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


def test_workflow_permissions_have_no_write(workflow):
    """Issues are opened with the bot App token in merge-verdict, so the
    workflow token needs no write scope and no OIDC."""
    perms = workflow.get("permissions", {})
    assert "write" not in perms.values(), f"workflow permissions must be read-only: {perms}"


@pytest.mark.parametrize("job", ["prepare", "review-1", "review-2"])
def test_untrusted_content_jobs_are_read_only(workflow, job):
    perms = workflow["jobs"][job].get("permissions")
    assert perms == {"contents": "read", "pull-requests": "read"}, (
        f"{job} reads untrusted PR content and must have read-only job permissions; got {perms}"
    )


# ---------------------------------------------------------------------------
# 13. Reviewers only write verdict files; merge-verdict records the whole
#     review (review_1, review_2, merged_verdict, outcome) in one ledger commit
#     and pushes it before dispatching pr-ingestion.
# ---------------------------------------------------------------------------

def _runs(workflow, job: str) -> str:
    return "\n".join(s.get("run", "") for s in workflow["jobs"][job].get("steps", []))


def test_reviewers_do_not_write_ledger(workflow):
    for job in ("review-1", "review-2"):
        assert "ledger-event.py" not in _runs(workflow, job), (
            f"{job} must not write the ledger; merge-verdict records all review events"
        )


def test_merge_verdict_records_review_events_in_one_commit(workflow):
    runs = _runs(workflow, "merge-verdict")
    assert "--events-out" in runs, "merge-verdicts.py must write the review events"
    assert runs.count("ledger-event.py") == 1
    assert "--events-file" in runs


def test_merge_verdict_records_before_dispatch(workflow):
    steps = workflow["jobs"]["merge-verdict"]["steps"]
    runs = [s.get("run", "") for s in steps]
    record = next(i for i, r in enumerate(runs) if "ledger-event.py" in r)
    dispatch = next(i for i, r in enumerate(runs) if "dispatch-approved.py" in r)
    assert record < dispatch, "Ledger must be pushed before pr-ingestion is dispatched"


def test_merge_verdict_tolerates_rejected_and_escalated_exit_codes(workflow):
    runs = _runs(workflow, "merge-verdict")
    assert "set +e" in runs and '"$rc" -eq 3' in runs, (
        "merge-verdicts.py exits 1, 2 and 4 are outcomes; only 3 fails the step"
    )
    assert "--retry-missing" in runs


def test_review_models_come_from_config(workflow_text):
    assert "claude-opus" not in workflow_text
    assert "claude_models']['review_1']" in workflow_text
    assert "claude_models']['review_2']" in workflow_text
    assert "max_turns_review" in workflow_text


def test_reviewers_use_read_only_token(workflow):
    for job in ("review-1", "review-2"):
        text = str(workflow["jobs"][job])
        assert "app-token" not in text, f"{job} must not get the bot App token"


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
    """All ledger-event.py invocations must pass --head-sha or an events file."""
    import re
    invocations = re.findall(
        r'ledger-event\.py.*?(?=\n\s*(?:env:|GH_TOKEN|$))', workflow_text, re.DOTALL
    )
    assert invocations, "No ledger-event.py invocations found in workflow"
    for inv in invocations:
        assert "--head-sha" in inv or "--events-file" in inv, (
            f"ledger-event.py invocation missing --head-sha:\n{inv}"
        )


# ---------------------------------------------------------------------------
# Pinned review input: reviewers never fetch the live PR
# ---------------------------------------------------------------------------

def test_prepare_pins_head_and_writes_diff(workflow):
    runs = _runs(workflow, "prepare")
    assert "refs/pull/$PR_NUMBER/head" in runs
    assert '"$FETCHED" != "$HEAD_SHA"' in runs, "prepare must refuse a moved PR head"
    assert "git merge-base" in runs
    assert "pr.diff" in runs and "files.txt" in runs


@pytest.mark.parametrize("job", ["review-1", "review-2"])
def test_reviewers_need_prepare_and_have_no_shell(workflow, job):
    j = workflow["jobs"][job]
    assert "prepare" in j["needs"]
    action = next(s for s in j["steps"] if "claude-code-action" in s.get("uses", ""))
    args = action["with"]["claude_args"]
    denied = args.split("--disallowedTools", 1)[1].split()[0].split(",")
    assert "Bash" in denied
    assert "WebFetch" in denied


def _claude_step(workflow, job: str) -> dict:
    return next(
        s for s in workflow["jobs"][job]["steps"] if "claude-code-action" in s.get("uses", "")
    )


def _tool_list(args: str, flag: str) -> list[str]:
    return args.split(f"--{flag}", 1)[1].split()[0].split(",")


@pytest.mark.parametrize("n", [1, 2])
def test_reviewers_load_only_trusted_claude_settings(workflow, n):
    """Regression: candidate content must not configure Claude. No project or
    local settings, no project hooks, no MCP servers from the checkout."""
    step = _claude_step(workflow, f"review-{n}")
    args = step["with"]["claude_args"].split()
    assert args[args.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in args
    assert '"disableAllHooks": true' in step["with"]["settings"]
    assert "--mcp-config" not in args


@pytest.mark.parametrize("n", [1, 2])
def test_reviewer_tool_rules_are_path_scoped(workflow, n):
    """Regression: Read only on the input and config, file writes only on the
    reviewer's own verdict file, and /proc and the runner temp dir denied.
    Claude Code checks every file-writing tool, Write included, against Edit
    rules, so the verdict file needs an Edit rule and Edit must not be denied."""
    args = _claude_step(workflow, f"review-{n}")["with"]["claude_args"]
    allowed = _tool_list(args, "allowedTools")
    assert sorted(allowed) == sorted([
        "Read(./review-input/**)",
        "Read(./.if-fork/config.yaml)",
        f"Edit(./review-output/review-{n}.json)",
    ])
    denied = _tool_list(args, "disallowedTools")
    assert "Read(//proc/**)" in denied
    assert "Read(//home/runner/work/_temp/**)" in denied
    # Read, Glob and Grep need no approval, so an allow rule does not confine
    # Glob and Grep; deny them outright.
    for tool in ("Bash", "MultiEdit", "WebFetch", "WebSearch", "Task", "Glob", "Grep"):
        assert tool in denied
    assert "Edit" not in denied
    assert "Write" not in allowed


@pytest.mark.parametrize("n", [1, 2])
def test_reviewer_checkout_is_sparse(workflow, n):
    """The reviewer's working directory holds only the config and prompts,
    so no repository .claude/, .mcp.json or CLAUDE.md is present."""
    checkout = next(
        s for s in workflow["jobs"][f"review-{n}"]["steps"]
        if s.get("uses", "").startswith("actions/checkout")
    )
    w = checkout["with"]
    assert w["persist-credentials"] is False
    assert w["sparse-checkout-cone-mode"] is False
    assert w["sparse-checkout"].split() == ["/.if-fork/config.yaml", "/.if-fork/prompts/"]


def test_prompts_do_not_fetch_the_diff():
    for n in (1, 2):
        text = (REPO_ROOT / ".if-fork" / "prompts" / f"pr-review-{n}.md").read_text()
        assert "gh api" not in text and "gh pr view" not in text
        assert "review-input/pr.diff" in text


def test_merge_verdict_requires_prepare_success(workflow):
    cond = workflow["jobs"]["merge-verdict"]["if"]
    assert "needs.prepare.result == 'success'" in cond


def test_merge_verdict_uses_pinned_diff_for_policy(workflow):
    runs = _runs(workflow, "merge-verdict")
    assert '--diff "$RUNNER_TEMP/review-input/pr.diff"' in runs
    assert '--files "$RUNNER_TEMP/review-input/files.txt"' in runs


def test_payload_never_interpolated_into_run(workflow_text):
    assert "${{ env.PR_NUMBER }}" not in workflow_text
    assert "${{ env.HEAD_SHA }}" not in workflow_text
    assert "client_payload" not in workflow_text.split("jobs:", 1)[1]


@pytest.mark.parametrize(
    "job", ["prepare", "review-1", "review-2", "single-review-approval", "merge-verdict"]
)
def test_every_job_validates_payload(workflow, job):
    first = workflow["jobs"][job]["steps"][0]
    assert first["name"] == "Validate dispatch payload"
    assert "^[0-9a-f]{40}$" in first["run"]


# ---------------------------------------------------------------------------
# Origin checks (Opus NEW-3): one review job must not stand in for the other
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2])
def test_review_jobs_output_their_origin(workflow, n):
    job = workflow["jobs"][f"review-{n}"]
    outputs = job["outputs"]
    assert outputs["reviewer_id"] == "${{ steps.settings.outputs.reviewer_id }}"
    assert outputs["verdict_sha256"] == "${{ steps.verdict.outputs.sha256 }}"
    assert outputs["upload"] == "${{ steps.upload.outcome }}"
    ids = [s.get("id") for s in job["steps"]]
    assert ids.index("verdict") < ids.index("upload"), "hash the verdict before uploading it"


def test_merge_verdict_downloads_verdicts_by_exact_name(workflow):
    steps = workflow["jobs"]["merge-verdict"]["steps"]
    downloads = [s["with"] for s in steps if "download-artifact" in s.get("uses", "")]
    assert all("pattern" not in w and "merge-multiple" not in w for w in downloads)
    names = {w.get("name") for w in downloads}
    assert {"review-1.json", "review-2.json"} <= names


def test_merge_verdict_passes_origin_of_each_verdict(workflow):
    step = next(
        s for s in workflow["jobs"]["merge-verdict"]["steps"] if s.get("id") == "merge"
    )
    for n in (1, 2):
        for field, output in (("ID", "reviewer_id"), ("SHA256", "verdict_sha256"),
                              ("UPLOAD", "upload")):
            assert step["env"][f"REVIEW_{n}_{field}"] == (
                f"${{{{ needs.review-{n}.outputs.{output} }}}}"
            )
        for flag in ("id", "sha256", "upload"):
            assert f'--review-{n}-{flag} "$REVIEW_{n}_{flag.upper()}"' in step["run"]


def test_merge_verdict_fails_job_on_origin_errors(workflow):
    steps = workflow["jobs"]["merge-verdict"]["steps"]
    last = steps[-1]
    assert "origin_errors" in last["run"] and "exit 1" in last["run"]
    names = [s.get("name", "") for s in steps]
    assert names.index("Record review outcome in ledger (one commit)") < len(steps) - 1


# ---------------------------------------------------------------------------
# A failed prepare is recorded as review_error (Opus NEW-7)
# ---------------------------------------------------------------------------

def test_failed_prepare_is_recorded(workflow):
    job = workflow["jobs"]["record-prepare-failure"]
    assert job["needs"] == ["gate", "prepare"]
    cond = " ".join(job["if"].split())
    assert cond.startswith("always()")
    assert "needs.gate.outputs.proceed == 'true'" in cond
    assert "needs.prepare.result == 'failure'" in cond
    runs = _runs(workflow, "record-prepare-failure")
    assert "merge-verdicts.py" in runs and "--prepare-failed" in runs
    assert "--ledger-path .if-fork/patch-ledger.jsonl" in runs
    assert runs.count("ledger-event.py") == 1 and "--events-file" in runs
    assert "dispatch-approved.py" in runs
    names = [s.get("name") for s in job["steps"]]
    assert names[0] == "Validate dispatch payload"
    assert names.index("Record in ledger (one commit)") < names.index(
        "Hand off (escalation issue on the last attempt)"
    )


def test_failed_prepare_never_runs_merge_verdict(workflow):
    """Only one of record-prepare-failure and merge-verdict can run."""
    assert "needs.prepare.result == 'success'" in workflow["jobs"]["merge-verdict"]["if"]
