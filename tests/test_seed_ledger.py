"""Tests for tools/seed-ledger.py."""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loader (hyphenated filename)
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "tools" / "seed-ledger.py"
_spec = importlib.util.spec_from_file_location("seed_ledger", _SCRIPT)
assert _spec is not None and _spec.loader is not None
seed_ledger: types.ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_ledger)  # type: ignore[union-attr]

# Fixture path
_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_SAMPLE_INPUT = _FIXTURE_DIR / "seed-input-sample.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pr(
    pr_number: int = 1001,
    head_sha: str = "aabbccdd" * 5,
    title: str = "Test PR",
    if_tier: str = "S",
) -> dict[str, Any]:
    return {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "title": title,
        "url": f"https://github.com/dotnet/wpf/pull/{pr_number}",
        "state": "OPEN",
        "additions": 10,
        "deletions": 5,
        "changed_files": 1,
        "labels": ["PR"],
        "if_tier": if_tier,
        "if_already_applied_in_local_main": False,
        "if_in_upstream_release_10_0": False,
        "if_in_upstream_main": False,
        "created_at": "2025-01-01T00:00:00Z",
        "merged_at": None,
    }


def _sample_candidates() -> list[dict[str, Any]]:
    return json.loads(_SAMPLE_INPUT.read_text())


# ---------------------------------------------------------------------------
# 1 — parse_args: defaults are applied correctly
# ---------------------------------------------------------------------------
class TestParseArgs:
    def test_defaults(self) -> None:
        ns = seed_ledger.parse_args(["--input", str(_SAMPLE_INPUT)])
        assert ns.actor == "seed-bulk-import"
        assert ns.batch_size == 50
        assert ns.dry_run is False

    def test_explicit_actor(self) -> None:
        ns = seed_ledger.parse_args([
            "--input", str(_SAMPLE_INPUT),
            "--actor", "my-custom-actor",
        ])
        assert ns.actor == "my-custom-actor"

    def test_dry_run_flag(self) -> None:
        ns = seed_ledger.parse_args([
            "--input", str(_SAMPLE_INPUT),
            "--dry-run",
        ])
        assert ns.dry_run is True


# ---------------------------------------------------------------------------
# 2 — _validate_candidate: rejects malformed inputs
# ---------------------------------------------------------------------------
class TestValidateCandidate:
    def test_valid_candidate_passes(self) -> None:
        assert seed_ledger._validate_candidate(_make_pr()) is None

    def test_non_dict_rejected(self) -> None:
        error = seed_ledger._validate_candidate("not-a-dict")
        assert error is not None
        assert "dict" in error

    def test_missing_pr_number(self) -> None:
        pr: dict[str, Any] = {"head_sha": "abc123"}
        error = seed_ledger._validate_candidate(pr)
        assert error is not None
        assert "pr_number" in error

    def test_non_int_pr_number(self) -> None:
        pr: dict[str, Any] = {"pr_number": "1234", "head_sha": "abc123"}
        error = seed_ledger._validate_candidate(pr)
        assert error is not None
        assert "int" in error

    def test_missing_head_sha(self) -> None:
        pr: dict[str, Any] = {"pr_number": 42}
        error = seed_ledger._validate_candidate(pr)
        assert error is not None
        assert "head_sha" in error

    def test_empty_head_sha(self) -> None:
        pr: dict[str, Any] = {"pr_number": 42, "head_sha": ""}
        error = seed_ledger._validate_candidate(pr)
        assert error is not None
        assert "head_sha" in error


# ---------------------------------------------------------------------------
# 3 — _load_existing_pr_numbers: reads a real ledger
# ---------------------------------------------------------------------------
class TestLoadExistingPrNumbers:
    def test_empty_file_returns_empty_set(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text("")
        assert seed_ledger._load_existing_pr_numbers(ledger) == set()

    def test_missing_file_returns_empty_set(self, tmp_path: Path) -> None:
        ledger = tmp_path / "nonexistent.jsonl"
        assert seed_ledger._load_existing_pr_numbers(ledger) == set()

    def test_reads_pr_numbers(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        lines = [
            json.dumps({"pr_number": 101, "event": "discovered", "line_hash": "aaa"}),
            json.dumps({"pr_number": 202, "event": "discovered", "line_hash": "bbb"}),
        ]
        ledger.write_text("\n".join(lines) + "\n")
        result = seed_ledger._load_existing_pr_numbers(ledger)
        assert result == {101, 202}

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            json.dumps({"pr_number": 5, "line_hash": "x"}) + "\n\n"
        )
        assert seed_ledger._load_existing_pr_numbers(ledger) == {5}

    def test_tolerates_invalid_json_line(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            json.dumps({"pr_number": 7, "line_hash": "y"}) + "\n"
            + "NOT_JSON\n"
        )
        # Should not raise; just skip the bad line
        result = seed_ledger._load_existing_pr_numbers(ledger)
        assert 7 in result


# ---------------------------------------------------------------------------
# 4 — _load_candidates: rejects bad input files
# ---------------------------------------------------------------------------
class TestLoadCandidates:
    def test_loads_valid_json_list(self) -> None:
        candidates = seed_ledger._load_candidates(_SAMPLE_INPUT)
        assert isinstance(candidates, list)
        assert len(candidates) == 8

    def test_missing_file_exits_2(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc_info:
            seed_ledger._load_candidates(tmp_path / "missing.json")
        assert exc_info.value.code == 2

    def test_non_json_file_exits_2(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("this is not JSON")
        with pytest.raises(SystemExit) as exc_info:
            seed_ledger._load_candidates(bad)
        assert exc_info.value.code == 2

    def test_json_object_not_list_exits_2(self, tmp_path: Path) -> None:
        obj = tmp_path / "obj.json"
        obj.write_text(json.dumps({"key": "value"}))
        with pytest.raises(SystemExit) as exc_info:
            seed_ledger._load_candidates(obj)
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 5 — _build_details: extracts the right fields
# ---------------------------------------------------------------------------
class TestBuildDetails:
    def test_includes_known_fields(self) -> None:
        pr = _make_pr()
        details = seed_ledger._build_details(pr)
        assert "title" in details
        assert "url" in details
        assert "if_tier" in details

    def test_excludes_pr_number_and_head_sha(self) -> None:
        pr = _make_pr()
        details = seed_ledger._build_details(pr)
        assert "pr_number" not in details
        assert "head_sha" not in details

    def test_tolerates_missing_optional_fields(self) -> None:
        pr: dict[str, Any] = {"pr_number": 1, "head_sha": "abc"}
        details = seed_ledger._build_details(pr)
        assert isinstance(details, dict)


# ---------------------------------------------------------------------------
# 6 — _invoke_ledger_event: dry-run passes --dry-run through
# ---------------------------------------------------------------------------
class TestInvokeLedgerEvent:
    def test_dry_run_includes_flag(self, tmp_path: Path) -> None:
        """Verify --dry-run is forwarded to the subprocess command."""
        pr = _make_pr()
        captured_cmd: list[list[str]] = []

        def _fake_run(
            cmd: list[str], **kwargs: Any
        ) -> MagicMock:
            captured_cmd.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=_fake_run):
            err = seed_ledger._invoke_ledger_event(
                pr=pr,
                ledger_path=tmp_path / "ledger.jsonl",
                actor="test-actor",
                dry_run=True,
            )

        assert err is None
        assert "--dry-run" in captured_cmd[0]

    def test_non_dry_run_omits_flag(self, tmp_path: Path) -> None:
        pr = _make_pr()
        captured_cmd: list[list[str]] = []

        def _fake_run(
            cmd: list[str], **kwargs: Any
        ) -> MagicMock:
            captured_cmd.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=_fake_run):
            err = seed_ledger._invoke_ledger_event(
                pr=pr,
                ledger_path=tmp_path / "ledger.jsonl",
                actor="test-actor",
                dry_run=False,
            )

        assert err is None
        assert "--dry-run" not in captured_cmd[0]

    def test_subprocess_failure_returns_error_string(self, tmp_path: Path) -> None:
        pr = _make_pr()

        def _fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            mock = MagicMock()
            mock.returncode = 1
            mock.stderr = "some error"
            mock.stdout = ""
            return mock

        with patch("subprocess.run", side_effect=_fake_run):
            err = seed_ledger._invoke_ledger_event(
                pr=pr,
                ledger_path=tmp_path / "ledger.jsonl",
                actor="test-actor",
                dry_run=False,
            )

        assert err is not None
        assert "1" in err  # exit code in message


# ---------------------------------------------------------------------------
# 7 — main: deduplication logic (idempotency)
# ---------------------------------------------------------------------------
class TestMainDedup:
    def test_existing_prs_are_skipped(self, tmp_path: Path, capsys: Any) -> None:
        """PRs already in the ledger must not be sent to ledger-event.py."""
        ledger = tmp_path / "ledger.jsonl"
        # Pre-populate ledger with pr_number 9199 (first entry in sample)
        ledger.write_text(
            json.dumps({"pr_number": 9199, "event": "discovered", "line_hash": "aaa"})
            + "\n"
        )

        invoke_calls: list[int] = []

        def _fake_invoke(
            *,
            pr: dict[str, Any],
            ledger_path: Path,
            actor: str,
            dry_run: bool,
            no_commit: bool = False,
        ) -> None:
            invoke_calls.append(pr["pr_number"])
            return None

        with patch.object(seed_ledger, "_invoke_ledger_event", side_effect=_fake_invoke):
            rc = seed_ledger.main([
                "--input", str(_SAMPLE_INPUT),
                "--ledger-path", str(ledger),
                "--dry-run",
            ])

        assert rc == 0
        # pr 9199 should have been skipped
        assert 9199 not in invoke_calls
        # all others from sample should have been processed
        sample = json.loads(_SAMPLE_INPUT.read_text())
        remaining = [p["pr_number"] for p in sample if p["pr_number"] != 9199]
        for pr_num in remaining:
            assert pr_num in invoke_calls


# ---------------------------------------------------------------------------
# 8 — main: malformed entry produces error but continues (error-tolerant)
# ---------------------------------------------------------------------------
class TestMainErrorTolerance:
    def test_invalid_entry_reported_but_rest_processed(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        bad_input = tmp_path / "input.json"
        # Mix one malformed entry among valid ones
        data: list[Any] = [
            _make_pr(pr_number=1),
            {"not_a_pr": True},  # malformed — missing pr_number and head_sha
            _make_pr(pr_number=2),
        ]
        bad_input.write_text(json.dumps(data))
        ledger = tmp_path / "ledger.jsonl"

        invoke_calls: list[int] = []

        def _fake_invoke(
            *,
            pr: dict[str, Any],
            ledger_path: Path,
            actor: str,
            dry_run: bool,
            no_commit: bool = False,
        ) -> None:
            invoke_calls.append(pr["pr_number"])
            return None

        with patch.object(seed_ledger, "_invoke_ledger_event", side_effect=_fake_invoke):
            rc = seed_ledger.main([
                "--input", str(bad_input),
                "--ledger-path", str(ledger),
                "--dry-run",
            ])

        # Exits 1 because there were errors
        assert rc == 1
        # Valid entries were still processed
        assert 1 in invoke_calls
        assert 2 in invoke_calls

    def test_all_valid_exits_zero(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"

        def _fake_invoke(
            *,
            pr: dict[str, Any],
            ledger_path: Path,
            actor: str,
            dry_run: bool,
            no_commit: bool = False,
        ) -> None:
            return None

        with patch.object(seed_ledger, "_invoke_ledger_event", side_effect=_fake_invoke):
            rc = seed_ledger.main([
                "--input", str(_SAMPLE_INPUT),
                "--ledger-path", str(ledger),
                "--dry-run",
            ])

        assert rc == 0


# ---------------------------------------------------------------------------
# 9 — main: summary JSON printed at end
# ---------------------------------------------------------------------------
class TestMainSummary:
    def test_summary_fields_present(self, tmp_path: Path, capsys: Any) -> None:
        ledger = tmp_path / "ledger.jsonl"

        def _fake_invoke(
            *,
            pr: dict[str, Any],
            ledger_path: Path,
            actor: str,
            dry_run: bool,
            no_commit: bool = False,
        ) -> None:
            return None

        with patch.object(seed_ledger, "_invoke_ledger_event", side_effect=_fake_invoke):
            seed_ledger.main([
                "--input", str(_SAMPLE_INPUT),
                "--ledger-path", str(ledger),
                "--dry-run",
            ])

        captured = capsys.readouterr()
        # Summary JSON is the last block printed
        lines = [ln.strip() for ln in captured.out.strip().splitlines() if ln.strip()]
        # Find the JSON summary block (last {...} span)
        json_text = "\n".join(lines[lines.index("{"):]) if "{" in lines else ""
        summary = json.loads(json_text)
        assert "total" in summary
        assert "added" in summary
        assert "skipped" in summary
        assert "errors" in summary
        assert summary["total"] == 8
