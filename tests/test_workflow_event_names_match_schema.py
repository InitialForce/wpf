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

# `gh run list --event <trigger>` uses the same flag for GitHub trigger names.
_GITHUB_TRIGGER_NAMES: frozenset[str] = frozenset(
    {"push", "pull_request", "schedule", "workflow_dispatch", "repository_dispatch",
     "workflow_run"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _events_in_workflow(path: Path) -> list[tuple[Path, int, str]]:
    """Return all (path, line_number, event_name) triples found in *path*."""
    findings: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        for m in EVENT_RE.finditer(line):
            if m.group(1) in _GITHUB_TRIGGER_NAMES:
                # `gh run list --event push` filters by trigger; not a ledger event.
                continue
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
        # Operator-only / manual ops events — emitted by humans via ledger-event.py CLI,
        # not by any workflow YAML. Documented in docs/operator-runbook.md.
        "automerge_frozen",       # operator toggles IF_AUTOMERGE_FROZEN repo variable
        "automerge_thawed",       # operator unfreezes after 4-PR validation passes
        "autonomy_paused",        # operator/kill-switch pause; unpause emits autonomy_resumed
        "autonomy_resumed",       # operator unpause (nightly-rebase no longer misuses it)
        "pre_flight_failed",      # superseded: cherry-pick-pre-flight.sh records 'escalated'
        # Future-use: emitted by review/build/perf/smoke gates wired in Phase-0/1
        # (per Bootstrap Status: wpf-2hh, wpf-1pt, wpf-2xo). Workflows currently
        # use placeholder steps and don't yet call ledger-event for these.
        "build_passed",           # emitted by build.yml per arch (Phase-0 wpf-1pt)
        "build_failed",           # emitted by build.yml per arch on failure
        "smoke_passed",           # emitted by smoke job after 22-scenario harness wired
        "smoke_failed",           # emitted by smoke job on failure
        "perf_passed",            # emitted by perf job after BenchmarkDotNet wired
        "perf_failed",            # emitted by perf job on regression
        "graduated_upstream",     # emitted by upstream-stable-adoption when patch lands
        "reverted",               # emitted on emergency revert path
    }
)


# Events that workflows record in batches (ledger-event.py --events-file).
# The workflow YAML never spells them as --event <name>; the tool that builds
# the batch does. event -> tool that must contain the quoted name.
_TOOLS_DIR: Path = Path(__file__).parent.parent / "tools"
_EMITTED_BY_TOOLS: dict[str, str] = {
    "discovered": "discover-prs.py",       # pr-discovery.yml
    "review_1": "merge-verdicts.py",       # pr-review.yml merge-verdict job
    "review_2": "merge-verdicts.py",
    "merged_verdict": "merge-verdicts.py",
    "approved": "merge-verdicts.py",
    "rejected": "merge-verdicts.py",
    "escalated": "merge-verdicts.py",
    "review_error": "merge-verdicts.py",
    "ingestion_requested": "merge-verdicts.py",
}


@pytest.mark.parametrize("event", sorted(_EMITTED_BY_TOOLS))
def test_tool_emitted_events_are_valid_and_present(event: str) -> None:
    assert event in VALID_EVENTS
    source = (_TOOLS_DIR / _EMITTED_BY_TOOLS[event]).read_text()
    assert f'"{event}"' in source, f"{_EMITTED_BY_TOOLS[event]} no longer emits {event!r}"


@pytest.mark.parametrize("expected_event", sorted(VALID_EVENTS))
def test_every_schema_event_is_used_or_explicitly_unused(expected_event: str) -> None:
    """Every event in VALID_EVENTS should appear in at least one workflow YAML.

    Events that are intentionally unused (e.g. operator-only or future-use)
    must be listed in _UNUSED_EVENTS_OK at the top of this test module.
    """
    if expected_event in _UNUSED_EVENTS_OK:
        pytest.skip(f"event '{expected_event}' explicitly allowed unused")

    used: set[str] = {ev for _, _, ev in all_workflow_events()} | set(_EMITTED_BY_TOOLS)
    assert expected_event in used, (
        f"Event '{expected_event}' is in VALID_EVENTS but is not referenced by any "
        f"workflow YAML under {WORKFLOW_DIR}.  Either add a workflow that emits it, "
        f"or add it to _UNUSED_EVENTS_OK with a rationale comment."
    )
