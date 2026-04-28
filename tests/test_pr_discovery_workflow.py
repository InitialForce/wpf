"""
Tests for .github/workflows/pr-discovery.yml and .if-fork/prompts/pr-discovery.md

Validates YAML structure, job dependencies, permissions, autonomy-gate wiring,
and the Haiku prompt file — without executing the workflow.
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
PROMPT_PATH = REPO_ROOT / ".if-fork" / "prompts" / "pr-discovery.md"


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
# 3. Jobs: gate, discover, record — all present with correct needs
# ---------------------------------------------------------------------------

def test_has_all_three_jobs(workflow):
    jobs = workflow.get("jobs", {})
    for expected in ("gate", "discover", "record"):
        assert expected in jobs, f"Missing job: {expected}"


def test_discover_needs_gate(workflow):
    jobs = workflow.get("jobs", {})
    needs = jobs["discover"].get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "gate" in needs, "discover job must declare 'needs: gate'"


def test_record_needs_discover(workflow):
    jobs = workflow.get("jobs", {})
    needs = jobs["record"].get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "discover" in needs, "record job must declare 'needs: discover'"


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
# 6. record job calls ledger-event.py
# ---------------------------------------------------------------------------

def test_record_calls_ledger_event(workflow):
    record = workflow["jobs"]["record"]
    steps = record.get("steps", [])
    step_runs = " ".join(s.get("run", "") for s in steps)
    assert "ledger-event.py" in step_runs, (
        "record job must call tools/ledger-event.py"
    )


def test_record_emits_discovered_event(workflow):
    record = workflow["jobs"]["record"]
    steps = record.get("steps", [])
    step_runs = " ".join(s.get("run", "") for s in steps)
    assert "--event discovered" in step_runs, (
        "record job must call ledger-event.py with --event discovered"
    )


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


def test_permissions_id_token_write(workflow):
    perms = workflow.get("permissions", {})
    assert perms.get("id-token") == "write", (
        f"permissions.id-token must be 'write'; got {perms.get('id-token')!r}"
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
# 10. Haiku prompt file exists and meets size/content requirements
# ---------------------------------------------------------------------------

def test_prompt_file_exists():
    assert PROMPT_PATH.exists(), f"Prompt file not found: {PROMPT_PATH}"


def test_prompt_file_character_count():
    content = PROMPT_PATH.read_text()
    char_count = len(content)
    assert 800 <= char_count <= 5000, (
        f"Prompt file must be between 800 and 5000 characters; got {char_count}"
    )


def test_prompt_file_has_sections():
    content = PROMPT_PATH.read_text()
    # Count markdown section headers (## or ###)
    sections = [line for line in content.splitlines() if line.startswith("##")]
    assert len(sections) >= 3, (
        "Prompt file must have at least 3 sections (## headings); "
        f"found {len(sections)}: {sections}"
    )


def test_prompt_mentions_ledger_event():
    content = PROMPT_PATH.read_text()
    assert "ledger-event.py" in content, (
        "Prompt must reference tools/ledger-event.py as the write path"
    )


def test_prompt_mentions_discovered_event():
    content = PROMPT_PATH.read_text()
    assert "discovered" in content, (
        "Prompt must reference the 'discovered' ledger event type"
    )


def test_prompt_mentions_dotnet_wpf():
    content = PROMPT_PATH.read_text()
    assert "dotnet/wpf" in content, (
        "Prompt must reference the upstream dotnet/wpf repository"
    )


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
