"""Behavioral regression test: every --event <NAME> in .github/workflows/*.yml
must be an entry in tools.ledger_schema.VALID_EVENTS.

Motivation (CRIT-2): GPT-5.5-Pro and Review-6 found multiple workflows calling
``ledger-event.py --event <X>`` where X was not registered in VALID_EVENTS, causing
runtime crashes.  Prior structural tests only checked workflow shape (job counts,
step presence), not whether the event names they emit are actually valid.

This file prevents that regression class from recurring.  If it fails it means:
  (a) a workflow was added/changed to use an event name not yet in VALID_EVENTS, OR
  (b) a new event was added to VALID_EVENTS but no workflow actually emits it yet.
Case (a) must be fixed before merge.  Case (b) can be suppressed via _UNUSED_EVENTS_OK.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.ledger_schema import VALID_EVENTS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKFLOW_DIR: Path = Path(__file__).parent.parent / ".github" / "workflows"

# Matches:  --event foo_bar  /  --event foo-bar  /  --event FooBar123
EVENT_RE: re.Pattern[str] = re.compile(r"--event\s+([a-zA-Z_][a-zA-Z0-9_-]*)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _events_in_workflow(path: Path) -> list[tuple[Path, int, str]]:
    """Return all (path, line_number, event_name) triples found in *path*."""
    findings: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        for m in EVENT_RE.finditer(line):
            findings.append((path, lineno, m.group(1)))
    return findings


def all_workflow_events() -> list[tuple[Path, int, str]]:
    """Collect every --event reference across all workflow YAML files."""
    out: list[tuple[Path, int, str]] = []
    for p in sorted(WORKFLOW_DIR.glob("*.yml")):
        out.extend(_events_in_workflow(p))
    return out


# ---------------------------------------------------------------------------
# Test 1: every event name used in workflows must exist in the schema
# ---------------------------------------------------------------------------


def test_every_event_in_workflows_is_in_schema() -> None:
    """No workflow may pass an event name to ledger-event.py that is not in VALID_EVENTS.

    If this test fails a workflow file contains an unregistered event name that
    will cause a runtime crash.  Either add the name to VALID_EVENTS in
    tools/ledger_schema.py, or correct the typo in the workflow YAML.
    """
    events_in_yaml = all_workflow_events()
    assert events_in_yaml, (
        f"No --event references found in any *.yml under {WORKFLOW_DIR}. "
        "Check that WORKFLOW_DIR is correct and that the files use --event syntax."
    )

    invalid = [
        (p, ln, ev) for p, ln, ev in events_in_yaml if ev not in VALID_EVENTS
    ]
    assert not invalid, (
        "Workflow YAML references --event names not in tools.ledger_schema.VALID_EVENTS:\n"
        + "\n".join(
            f"  {p.name}:{ln}  '{ev}'" for p, ln, ev in invalid
        )
    )


# ---------------------------------------------------------------------------
# Test 2: every event in the schema is exercised by at least one workflow
#         (dead events indicate schema drift or orphaned constants)
# ---------------------------------------------------------------------------


# Intentionally not yet referenced in any workflow YAML.
# Add entries here when a new event exists in VALID_EVENTS but no workflow
# emits it yet (e.g. operator-only or future-use events), with a brief comment.
_UNUSED_EVENTS_OK: frozenset[str] = frozenset(
    {
        # Example:
        # "some_future_event",  # planned for Q3 operator tooling
    }
)


@pytest.mark.parametrize("expected_event", sorted(VALID_EVENTS))
def test_every_schema_event_is_used_or_explicitly_unused(expected_event: str) -> None:
    """Every event in VALID_EVENTS should appear in at least one workflow YAML.

    Events that are intentionally unused (e.g. operator-only or future-use)
    must be listed in _UNUSED_EVENTS_OK at the top of this test module.
    """
    if expected_event in _UNUSED_EVENTS_OK:
        pytest.skip(f"event '{expected_event}' explicitly allowed unused")

    used: set[str] = {ev for _, _, ev in all_workflow_events()}
    assert expected_event in used, (
        f"Event '{expected_event}' is in VALID_EVENTS but is not referenced by any "
        f"workflow YAML under {WORKFLOW_DIR}.  Either add a workflow that emits it, "
        f"or add it to _UNUSED_EVENTS_OK with a rationale comment."
    )
