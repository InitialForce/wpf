"""
Gate-chain invariant tests for all workflows.

Validates the root-cause fix for CRIT-1: every job that has
    if: needs.gate.outputs.proceed == 'true'  (or any expression referencing
    needs.gate.outputs.proceed)
MUST also declare 'gate' in its own `needs:` list.

GitHub Actions does NOT propagate job outputs transitively.  If job C needs
job B which needs job A, C cannot access A's outputs unless C also lists A in
its own needs.  Without this, needs.gate.outputs.proceed evaluates to '' and
the condition '' == 'true' is always false — silently skipping all downstream
jobs even when the gate approved the run.

Workflow files covered:
  - pr-review.yml
  - pr-ingestion.yml
  - release.yml
  - pr-discovery.yml
  - nightly-rebase.yml
  - upstream-stable-adoption.yml
  - weekly-differential.yml
  - claude-on-failure.yml
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

WORKFLOW_FILES = [
    "pr-review.yml",
    "pr-ingestion.yml",
    "release.yml",
    "pr-discovery.yml",
    "nightly-rebase.yml",
    "upstream-stable-adoption.yml",
    "weekly-differential.yml",
    "claude-on-failure.yml",
]


def _normalize_on_key(doc: dict) -> dict:
    """
    PyYAML parses bare YAML key ``on`` as Python boolean ``True`` (YAML 1.1 spec).
    Normalize it back to the string ``"on"`` for consistent test access.
    """
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


def _normalize_needs(needs_value: Any) -> list[str]:
    """Return a list of job names from a YAML needs value (str | list | None)."""
    if needs_value is None:
        return []
    if isinstance(needs_value, str):
        return [needs_value]
    return list(needs_value)


def _load_workflow(filename: str) -> dict:
    path = WORKFLOWS_DIR / filename
    assert path.exists(), f"Workflow file not found: {path}"
    with path.open() as fh:
        return _normalize_on_key(yaml.safe_load(fh))


def _violations_in_workflow(filename: str) -> list[str]:
    """
    Return a list of human-readable violation strings for the workflow file.

    A violation is: job J has needs.gate.outputs.proceed in its 'if' condition
    but 'gate' is NOT in J's needs list.
    """
    doc = _load_workflow(filename)
    jobs = doc.get("jobs", {})
    violations = []
    for job_name, job_def in jobs.items():
        if not isinstance(job_def, dict):
            continue
        if_condition = str(job_def.get("if", ""))
        if "needs.gate.outputs.proceed" not in if_condition:
            continue
        needs = _normalize_needs(job_def.get("needs"))
        if "gate" not in needs:
            violations.append(
                f"{filename}::{job_name}: references needs.gate.outputs.proceed "
                f"in its 'if' condition but 'gate' is NOT in its needs list "
                f"(needs={needs!r}). "
                f"GitHub Actions cannot access gate outputs without gate in needs."
            )
    return violations


# ---------------------------------------------------------------------------
# Per-workflow gate-chain tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow_file", WORKFLOW_FILES)
def test_yaml_parses(workflow_file: str) -> None:
    """Every workflow file must parse as valid YAML."""
    doc = _load_workflow(workflow_file)
    assert isinstance(doc, dict) and doc, (
        f"{workflow_file}: YAML parsed to empty or non-dict"
    )


@pytest.mark.parametrize("workflow_file", WORKFLOW_FILES)
def test_gate_in_needs_when_gate_output_referenced(workflow_file: str) -> None:
    """
    CRIT-1 root-cause invariant: every job that references
    needs.gate.outputs.proceed in its 'if' condition must also list 'gate'
    in its own needs: array.

    Without this, GitHub Actions evaluates needs.gate.outputs.proceed as ''
    (empty string) and the condition '' == 'true' is always false, silently
    skipping all gated downstream jobs regardless of the gate result.
    """
    violations = _violations_in_workflow(workflow_file)
    assert not violations, (
        "Gate-in-needs invariant violated:\n" + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Cross-workflow summary test (all workflows at once)
# ---------------------------------------------------------------------------

def test_all_workflows_gate_chain_invariant() -> None:
    """
    Omnibus check: confirms no workflow in the set violates the gate-chain
    invariant.  Complements the per-file parametrized test above.
    """
    all_violations: list[str] = []
    for wf in WORKFLOW_FILES:
        all_violations.extend(_violations_in_workflow(wf))

    assert not all_violations, (
        f"Gate-in-needs violations found across {len(WORKFLOW_FILES)} workflows:\n"
        + "\n".join(f"  - {v}" for v in all_violations)
    )


# ---------------------------------------------------------------------------
# Sanity: workflows with a gate job define the gate as a reusable workflow
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow_file", WORKFLOW_FILES)
def test_gate_job_is_reusable_autonomy_check(workflow_file: str) -> None:
    """
    Every workflow that defines a 'gate' job must wire it to
    autonomy-check.yml as a reusable workflow call.
    """
    doc = _load_workflow(workflow_file)
    jobs = doc.get("jobs", {})
    if "gate" not in jobs:
        pytest.skip(f"{workflow_file}: no 'gate' job — skipping")
    gate_job = jobs["gate"]
    uses = gate_job.get("uses", "")
    assert "autonomy-check.yml" in uses, (
        f"{workflow_file}: 'gate' job must call autonomy-check.yml via 'uses:'; "
        f"got: {uses!r}"
    )


@pytest.mark.parametrize("workflow_file", WORKFLOW_FILES)
def test_gate_job_has_requested_action(workflow_file: str) -> None:
    """
    Every 'gate' job that calls autonomy-check.yml must supply the required
    'requested_action' input.  autonomy-check.yml requires this input
    (required: true, no default) and will fail if it is absent.
    """
    doc = _load_workflow(workflow_file)
    jobs = doc.get("jobs", {})
    if "gate" not in jobs:
        pytest.skip(f"{workflow_file}: no 'gate' job — skipping")
    gate_job = jobs["gate"]
    uses = gate_job.get("uses", "")
    if "autonomy-check.yml" not in uses:
        pytest.skip(f"{workflow_file}: gate does not call autonomy-check.yml")
    with_block = gate_job.get("with", {}) or {}
    requested_action = with_block.get("requested_action", "")
    assert requested_action, (
        f"{workflow_file}: gate job must supply 'with: requested_action: <value>'; "
        f"currently: with={with_block!r}"
    )
