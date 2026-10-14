"""
Tests for .github/workflows/pr-discovery.yml

Validates YAML structure, job dependencies, permissions, autonomy-gate wiring,
the one-commit ledger write and the pr-discovered dispatch payload — without
executing the workflow.
If actionlint is available it is called as well; if not, the structural assertions
below serve as the floor.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pr-discovery.yml"
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
    """Parse and return the pr-discovery.yml document."""
    with WORKFLOW_PATH.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


# ---------------------------------------------------------------------------
# 1. YAML parses cleanly
# ---------------------------------------------------------------------------

def test_yaml_parses():
    """Workflow YAML must parse without error."""
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    assert doc is not None, "YAML produced None — file is likely empty"


# ---------------------------------------------------------------------------
# 2. Trigger: schedule + workflow_dispatch
# ---------------------------------------------------------------------------

def test_has_schedule_trigger(workflow):
    on = workflow.get("on", {})
    assert "schedule" in on, "Workflow must have a schedule trigger"
    crons = [entry.get("cron") for entry in on["schedule"]]
    assert any(c == "17 4 * * *" for c in crons), (
        f"Expected cron '17 4 * * *'; found {crons}"
    )


def test_has_workflow_dispatch_trigger(workflow):
    on = workflow.get("on", {})
    assert "workflow_dispatch" in on, "Workflow must support workflow_dispatch"


# ---------------------------------------------------------------------------
# 3. Jobs: gate, discover
# ---------------------------------------------------------------------------

def test_has_gate_and_discover_jobs(workflow):
    jobs = workflow.get("jobs", {})
    assert set(jobs) == {"gate", "discover"}, f"Unexpected jobs: {sorted(jobs)}"


def test_discover_needs_gate(workflow):
    jobs = workflow.get("jobs", {})
    needs = jobs["discover"].get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "gate" in needs, "discover job must declare 'needs: gate'"


# ---------------------------------------------------------------------------
# 4. Gate job calls autonomy-check.yml reusable with discovery-scan action
# ---------------------------------------------------------------------------

def test_gate_uses_autonomy_check_reusable(workflow):
    gate = workflow["jobs"]["gate"]
    uses = gate.get("uses", "")
    assert "autonomy-check.yml" in uses, (
        f"gate job must call autonomy-check.yml reusable; got: {uses!r}"
    )


def test_gate_passes_discovery_scan_action(workflow):
    gate = workflow["jobs"]["gate"]
    with_block = gate.get("with", {})
    assert with_block.get("requested_action") == "discovery-scan", (
        "gate job must pass requested_action: discovery-scan"
    )


# ---------------------------------------------------------------------------
# 5. discover job: conditional on gate proceed output
# ---------------------------------------------------------------------------

def test_discover_conditional_on_gate_proceed(workflow):
    discover = workflow["jobs"]["discover"]
    condition = discover.get("if", "")
    assert "gate" in condition and "proceed" in condition, (
        "discover job must have 'if' condition referencing gate.outputs.proceed; "
        f"got: {condition!r}"
    )


# ---------------------------------------------------------------------------
# 6. discover job: deterministic tool, one ledger commit, dispatch payload
# ---------------------------------------------------------------------------

def _step_runs(workflow) -> str:
    return "\n".join(s.get("run", "") for s in workflow["jobs"]["discover"]["steps"])


def test_discover_uses_discover_prs_tool_not_a_model(workflow):
    runs = _step_runs(workflow)
    assert "tools/discover-prs.py" in runs
    uses = [s.get("uses", "") for s in workflow["jobs"]["discover"]["steps"]]
    assert not any("claude-code-action" in u for u in uses), (
        "Discovery is deterministic; it must not call a model"
    )


def test_ledger_written_once_with_events_file(workflow):
    runs = _step_runs(workflow)
    assert runs.count("ledger-event.py") == 1, "Exactly one ledger-event.py call per run"
    assert "--events-file" in runs, "All discovered events must go into one commit"


def test_dispatch_payload_has_pr_number_and_head_sha(workflow):
    runs = _step_runs(workflow)
    assert '"pr-discovered"' in runs
    assert "pr_number:" in runs and "head_sha:" in runs, (
        "pr-discovered client_payload must carry pr_number and head_sha for pr-review.yml"
    )


def test_dispatch_happens_after_ledger_push(workflow):
    names = [s.get("name", "") for s in workflow["jobs"]["discover"]["steps"]]
    record = next(i for i, n in enumerate(names) if n.startswith("Record discovered"))
    dispatch = next(i for i, n in enumerate(names) if n.startswith("Dispatch reviews"))
    assert record < dispatch


# ---------------------------------------------------------------------------
# 7. Permissions: no write except id-token; contents and pull-requests are read
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


def test_permissions_no_id_token_write(workflow):
    # id-token: write removed — pr-discovery does not use OIDC.
    perms = workflow.get("permissions", {})
    assert perms.get("id-token") != "write", (
        "permissions.id-token must NOT be 'write' in pr-discovery — no OIDC usage; "
        f"got {perms.get('id-token')!r}"
    )


def test_no_contents_write(workflow):
    perms = workflow.get("permissions", {})
    assert perms.get("contents") != "write", (
        "permissions.contents must NOT be 'write' — only 'read' is allowed"
    )


# ---------------------------------------------------------------------------
# 8. No reference to secrets.GH_APP_TOKEN (R4-6 fix)
# ---------------------------------------------------------------------------

def test_no_stale_gh_app_token_secret():
    """R4-6: token comes from tibdex/github-app-token output, not secrets.GH_APP_TOKEN."""
    content = WORKFLOW_PATH.read_text()
    assert "secrets.GH_APP_TOKEN" not in content, (
        "Workflow must not reference secrets.GH_APP_TOKEN (R4-6). "
        "Use tibdex/github-app-token@v2 output instead."
    )


# ---------------------------------------------------------------------------
# 9. GH App token generated via tibdex/github-app-token@v2
# ---------------------------------------------------------------------------

def test_uses_tibdex_github_app_token(workflow):
    jobs = workflow.get("jobs", {})
    all_steps = []
    for job in jobs.values():
        all_steps.extend(job.get("steps", []))
    uses_values = [s.get("uses", "") for s in all_steps]
    assert any("tibdex/github-app-token" in u for u in uses_values), (
        "At least one job step must use tibdex/github-app-token@v2 for GH App auth"
    )


# ---------------------------------------------------------------------------
# 10. The retired Haiku discovery prompt stays retired
# ---------------------------------------------------------------------------

def test_discovery_prompt_removed():
    prompt = REPO_ROOT / ".if-fork" / "prompts" / "pr-discovery.md"
    assert not prompt.exists(), "pr-discovery.md is unused since discovery has no model call"


# ---------------------------------------------------------------------------
# 11. actionlint (optional)
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


def test_lost_ingestion_handoffs_are_resent_after_ledger_push(workflow):
    steps = workflow["jobs"]["discover"]["steps"]
    names = [s.get("name", "") for s in steps]
    record = next(i for i, n in enumerate(names) if n.startswith("Record discovered"))
    resend = next(i for i, n in enumerate(names) if n.startswith("Re-send lost ingestion"))
    assert record < resend
    run = steps[resend]["run"]
    assert '"pr-ingestion-requested"' in run
    assert "pr_number:" in run and "head_sha:" in run
    assert "--ingest-out" in _step_runs(workflow)
