"""Tests for tools/ledger_schema.py."""

from __future__ import annotations

from tools.ledger_schema import VALID_EVENTS


class TestNewEventTypes:
    """Verify that the four newly-added event types are present."""

    def test_rebase_failed_present(self) -> None:
        assert "rebase_failed" in VALID_EVENTS

    def test_failure_analyzed_present(self) -> None:
        assert "failure_analyzed" in VALID_EVENTS

    def test_pre_flight_failed_present(self) -> None:
        assert "pre_flight_failed" in VALID_EVENTS

    def test_review_single_path_warning_present(self) -> None:
        assert "review_single_path_warning" in VALID_EVENTS


class TestSchemaImmutability:
    """VALID_EVENTS must be immutable so callers cannot mutate the schema."""

    def test_is_frozenset(self) -> None:
        assert isinstance(VALID_EVENTS, frozenset)

    def test_cannot_add_element(self) -> None:
        try:
            VALID_EVENTS.add("should_not_work")  # type: ignore[attr-defined]
            raise AssertionError("add() should have raised AttributeError")
        except AttributeError:
            pass  # expected — frozenset has no add()


class TestNoRegression:
    """Original events must still be present; the set must be large enough."""

    _ORIGINAL_EVENTS = {
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
        "merged_verdict",
    }

    def test_original_events_still_present(self) -> None:
        for event in self._ORIGINAL_EVENTS:
            assert event in VALID_EVENTS, f"original event missing: {event!r}"

    def test_at_least_twenty_five_events(self) -> None:
        # 21 original + 4 new = 25
        assert len(VALID_EVENTS) >= 25


class TestNoInvalidEntries:
    """The set must contain no empty strings or None values."""

    def test_no_empty_strings(self) -> None:
        assert "" not in VALID_EVENTS

    def test_no_none_values(self) -> None:
        # frozenset[str] — None would be a type violation, but guard at runtime too
        assert None not in VALID_EVENTS  # type: ignore[operator]

    def test_all_entries_are_strings(self) -> None:
        for entry in VALID_EVENTS:
            assert isinstance(entry, str), f"non-string entry found: {entry!r}"
