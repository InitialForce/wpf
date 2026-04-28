"""Shared schema constants for the patch ledger.

Both ledger-event.py and ledger-validate.py import from this module so that
VALID_EVENTS stays in exactly one place and cannot drift between the two tools.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Event types — verbatim from exec-docs §10.5 event-types table
# ---------------------------------------------------------------------------
VALID_EVENTS: frozenset[str] = frozenset(
    [
        "discovered",
        "review_1",
        "review_2",
        "approved",
        "escalated",
        "cherry_picked",
        "build_failed",
        "build_passed",
        "smoke_failed",
        "smoke_passed",
        "perf_passed",
        "perf_failed",
        "published",
        "graduated_upstream",
        "rejected",
        "reverted",
        "autonomy_paused",
        "autonomy_resumed",
        "automerge_frozen",
        "automerge_thawed",
        # merge-verdict workflow outcome (CRIT-2 fix)
        "merged_verdict",
    ]
)
