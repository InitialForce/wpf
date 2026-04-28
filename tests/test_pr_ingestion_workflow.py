"""
Tests for .github/workflows/pr-ingestion.yml

Validates YAML structure, job dependencies, gate conditions, and
security invariants — without executing the workflow.

Structural assertions (>= 18):
  1.  YAML parses cleanly
  2.  repository_dispatch trigger with correct type
  3.  workflow_dispatch trigger with pr_number and head_sha inputs
  4.  All 8 jobs present
  5.  Needs chain is correct (gate → pre-flight → sha-smuggling-check → cherry-pick
      → denylist-check → build-and-test → perf-gate → open-pr)
  6.  sha-smuggling-check is NOT a direct dependency of cherry-pick (it must come BEFORE)
  7.  SHA-smuggling check job exists and references ledger-event.py
  8.  tools/cherry-pick-pre-flight.sh is referenced
  9.  tools/check-denylist.py is referenced
  10. tools/check-regression.py is referenced with --current-sha flag
  11. Auto-merge step is gated on IF_AUTOMERGE_FROZEN != 'true' (MED-6)
  12. No reference to secrets.GH_APP_TOKEN (must use GH App inline token)
  13. Concurrency group references pr_number
  14. Permissions include contents:write, pull-requests:write, issues:write, id-token:write
  15. cherry_picked ledger event is emitted in open-pr job
  16. gate job passes requested_action: cherry-pick (CRIT-1)
  17. All downstream jobs guard on needs.gate.outputs.proceed == 'true' (CRIT-1)
  18. SHA-smuggling check reads from patch-ledger.jsonl (HIGH-3)
  19. Denylist check uses full diff range origin/if/staging...HEAD (HIGH-4)
  20. perf-gate and open-pr both declare cherry-pick in needs (HIGH-5)
  21. build-and-test uses gh run watch (HIGH-6)
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
    "denylist-check",
    "build-and-test",
    "perf-gate",
    "open-pr",
}

# Expected linear needs chain (each job → its direct predecessor(s))
EXPECTED_NEEDS: dict[str, set[str]] = {
    "gate": set(),
    "pre-flight": {"gate"},
    "sha-smuggling-check": {"pre-flight"},
    "cherry-pick": {"sha-smuggling-check"},
    "denylist-check": {"cherry-pick"},
    "build-and-test": {"denylist-check"},
    "perf-gate": {"build-and-test"},
    "open-pr": {"perf-gate"},
}

# Jobs that must guard on the gate proceed output (all downstream jobs)
PROCEED_GUARDED_JOBS = {
    "pre-flight",
    "sha-smuggling-check",
    "cherry-pick",
    "denylist-check",
    "build-and-test",
    "perf-gate",
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
# 4. All 8 jobs present
# ---------------------------------------------------------------------------

def test_all_eight_jobs_present(workflow: dict) -> None:
    """Workflow must define all 8 required jobs."""
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
    ("denylist-check", {"cherry-pick"}),
    ("build-and-test", {"denylist-check"}),
    ("perf-gate", {"build-and-test"}),
    ("open-pr", {"perf-gate"}),
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
# 10. check-regression.py is referenced with --current-sha flag (R4-3 fix)
# ---------------------------------------------------------------------------

def test_check_regression_referenced_with_current_sha() -> None:
    """tools/check-regression.py must be called with --current-sha (R4-3 fix)."""
    raw = _raw_text()
    assert "check-regression.py" in raw, (
        "Workflow must reference tools/check-regression.py"
    )
    assert "--current-sha" in raw, (
        "check-regression.py invocation must include --current-sha (R4-3 fix)"
    )


# ---------------------------------------------------------------------------
# 11. Auto-merge step is gated on IF_AUTOMERGE_FROZEN != 'true' (MED-6)
# ---------------------------------------------------------------------------

def test_automerge_gated_on_frozen_var_not_true() -> None:
    """Auto-merge step must use != 'true' so unset var defaults to enabled (MED-6)."""
    raw = _raw_text()
    assert "IF_AUTOMERGE_FROZEN" in raw, (
        "Workflow must reference vars.IF_AUTOMERGE_FROZEN"
    )
    # The condition must use != 'true' (not == 'false') so unset var defaults correctly
    assert re.search(r"IF_AUTOMERGE_FROZEN\s*!=\s*['\"]true['\"]", raw), (
        "Auto-merge condition must check IF_AUTOMERGE_FROZEN != 'true' (MED-6: correct default for unset var)"
    )


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
        f"Job '{job_name}' must check needs.gate.outputs.proceed in its 'if' condition; got: {job_if!r}"
    )
    assert "proceed" in job_if and "'true'" in job_if, (
        f"Job '{job_name}' proceed condition must compare to 'true'; got: {job_if!r}"
    )


# ---------------------------------------------------------------------------
# 18. SHA-smuggling check reads from patch-ledger.jsonl (HIGH-3)
# ---------------------------------------------------------------------------

def test_sha_smuggling_reads_ledger_file() -> None:
    """SHA-smuggling check must read from .if-fork/patch-ledger.jsonl (HIGH-3)."""
    raw = _raw_text()
    assert "patch-ledger.jsonl" in raw, (
        "SHA-smuggling check must read from .if-fork/patch-ledger.jsonl ledger file (HIGH-3)"
    )
    # Must check for review_1 or review_2 event type in ledger
    assert "review_1" in raw or "review_2" in raw, (
        "SHA-smuggling check must look for review_1/review_2 entries in the ledger (HIGH-3)"
    )


# ---------------------------------------------------------------------------
# 19. Denylist check uses full diff range origin/if/staging...HEAD (HIGH-4)
# ---------------------------------------------------------------------------

def test_denylist_uses_full_diff_range() -> None:
    """Denylist check must use origin/if/staging...HEAD to catch all cherry-pick commits (HIGH-4)."""
    raw = _raw_text()
    assert "origin/if/staging...HEAD" in raw, (
        "Denylist check must use 'origin/if/staging...HEAD' diff range to cover all commits (HIGH-4)"
    )


# ---------------------------------------------------------------------------
# 20. perf-gate and open-pr both declare cherry-pick in needs (HIGH-5)
# ---------------------------------------------------------------------------

def test_perf_gate_needs_cherry_pick(workflow: dict) -> None:
    """perf-gate must declare cherry-pick in its needs to access branch output (HIGH-5)."""
    jobs = workflow.get("jobs", {})
    perf_needs = _normalize_needs(jobs.get("perf-gate", {}).get("needs"))
    assert "cherry-pick" in perf_needs, (
        f"perf-gate must need 'cherry-pick' to access branch output; got needs={perf_needs!r}"
    )


def test_open_pr_needs_cherry_pick(workflow: dict) -> None:
    """open-pr must declare cherry-pick in its needs to access branch output (HIGH-5)."""
    jobs = workflow.get("jobs", {})
    open_pr_needs = _normalize_needs(jobs.get("open-pr", {}).get("needs"))
    assert "cherry-pick" in open_pr_needs, (
        f"open-pr must need 'cherry-pick' to access branch output; got needs={open_pr_needs!r}"
    )


# ---------------------------------------------------------------------------
# 21. build-and-test uses gh run watch (HIGH-6)
# ---------------------------------------------------------------------------

def test_build_and_test_uses_gh_run_watch() -> None:
    """build-and-test must use 'gh run watch' (not racy gh run list --limit 1) (HIGH-6)."""
    raw = _raw_text()
    assert "gh run watch" in raw, (
        "build-and-test must use 'gh run watch' to track the build run (HIGH-6)"
    )
    assert "gh run list --limit 1" not in raw, (
        "build-and-test must NOT use 'gh run list --limit 1' — race condition (HIGH-6)"
    )


# ---------------------------------------------------------------------------
# 22. Canonical ledger CLI args used (CRIT-2)
# ---------------------------------------------------------------------------

def test_ledger_event_canonical_cli_args() -> None:
    """All ledger-event.py calls must use canonical CLI args: --head-sha, --actor, --details-json, --push (CRIT-2)."""
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
