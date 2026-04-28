"""Tests for tools/ledger-validate.py.

Covers all validation gates specified in bead wpf-2nf:
  - Gate 1: valid ledger → exit 0, valid: true
  - Gate 2: broken prev_hash chain → exit 2, prev_hash_mismatch error
  - Gate 3: invalid event type → exit 2, invalid_event_type error
  - Plus: missing field, invalid JSON, line_hash mismatch, empty ledger,
          missing ledger, --strict-signature flag (no-op without git),
          and end-to-end subprocess calls for exit code verification.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Load module from hyphenated filename
_SCRIPT = Path(__file__).parent.parent / "tools" / "ledger-validate.py"
_spec = importlib.util.spec_from_file_location("ledger_validate", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ledger_validate: types.ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger_validate)  # type: ignore[union-attr]

# Fixture directory
FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_valid_line(
    event: str = "discovered",
    pr_number: int = 10628,
    head_sha: str = "abc1234567890abcdef",
    actor: str = "claude-wpf-bot",
    details: dict[str, Any] | None = None,
    prev_hash: str = "",
    ts: str = "2026-04-27T12:00:00Z",
) -> str:
    """Build a correctly-hashed ledger line (mirrors ledger-event.py logic)."""
    if details is None:
        details = {}
    record: dict[str, Any] = {
        "schema_version": 1,
        "ts": ts,
        "event": event,
        "actor": actor,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "details": details,
        "prev_hash": prev_hash,
    }
    body = json.dumps(record, separators=(",", ":"), sort_keys=True)
    record["line_hash"] = hashlib.sha256(body.encode()).hexdigest()
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def _write_ledger(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1 — Gate 1: valid fixture → valid: true, exit 0
# ---------------------------------------------------------------------------

def test_valid_fixture_is_valid() -> None:
    result = ledger_validate.validate(FIXTURES / "ledger-valid.jsonl")
    assert result["valid"] is True
    assert result["lines_checked"] == 2
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# 2 — Gate 1 (subprocess): valid fixture → exit code 0
# ---------------------------------------------------------------------------

def test_valid_fixture_exit_0() -> None:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--ledger-path", str(FIXTURES / "ledger-valid.jsonl")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout
    out = json.loads(proc.stdout)
    assert out["valid"] is True


# ---------------------------------------------------------------------------
# 3 — Gate 2: broken chain fixture → prev_hash_mismatch error
# ---------------------------------------------------------------------------

def test_broken_chain_has_prev_hash_mismatch_error() -> None:
    result = ledger_validate.validate(FIXTURES / "ledger-broken-chain.jsonl")
    assert result["valid"] is False
    kinds = [e["kind"] for e in result["errors"]]
    assert "prev_hash_mismatch" in kinds


# ---------------------------------------------------------------------------
# 4 — Gate 2 (subprocess): broken chain → exit code 2
# ---------------------------------------------------------------------------

def test_broken_chain_exit_2() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--ledger-path",
            str(FIXTURES / "ledger-broken-chain.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# 5 — Gate 3: bad-event fixture → invalid_event_type error
# ---------------------------------------------------------------------------

def test_bad_event_has_invalid_event_type_error() -> None:
    result = ledger_validate.validate(FIXTURES / "ledger-bad-event.jsonl")
    assert result["valid"] is False
    assert result["errors"][0]["kind"] == "invalid_event_type"


# ---------------------------------------------------------------------------
# 6 — Gate 3 (subprocess): bad-event → exit code 2
# ---------------------------------------------------------------------------

def test_bad_event_exit_2() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--ledger-path",
            str(FIXTURES / "ledger-bad-event.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# 7 — Empty ledger is valid (genesis state)
# ---------------------------------------------------------------------------

def test_empty_ledger_is_valid(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    ledger.write_text("")
    result = ledger_validate.validate(ledger)
    assert result["valid"] is True
    assert result["lines_checked"] == 0


# ---------------------------------------------------------------------------
# 8 — Missing ledger is valid (nothing to violate)
# ---------------------------------------------------------------------------

def test_missing_ledger_is_valid(tmp_path: Path) -> None:
    ledger = tmp_path / "nonexistent.jsonl"
    result = ledger_validate.validate(ledger)
    assert result["valid"] is True
    assert result["lines_checked"] == 0


# ---------------------------------------------------------------------------
# 9 — Line that is not valid JSON produces invalid_json error
# ---------------------------------------------------------------------------

def test_invalid_json_line(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    _write_ledger(ledger, ["not-json-at-all"])
    result = ledger_validate.validate(ledger)
    assert result["valid"] is False
    assert any(e["kind"] == "invalid_json" for e in result["errors"])


# ---------------------------------------------------------------------------
# 10 — Missing required field produces missing_field error
# ---------------------------------------------------------------------------

def test_missing_required_field(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    # Build a line without 'actor'
    obj: dict[str, Any] = {
        "schema_version": 1,
        "ts": "2026-04-27T12:00:00Z",
        "event": "discovered",
        # 'actor' intentionally omitted
        "pr_number": 10628,
        "head_sha": "abc",
        "details": {},
        "prev_hash": "",
    }
    body = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    obj["line_hash"] = hashlib.sha256(body.encode()).hexdigest()
    _write_ledger(ledger, [json.dumps(obj, separators=(",", ":"), sort_keys=True)])
    result = ledger_validate.validate(ledger)
    assert result["valid"] is False
    assert any(
        e["kind"] == "missing_field" and "actor" in e["details"]
        for e in result["errors"]
    )


# ---------------------------------------------------------------------------
# 11 — Tampered line_hash produces line_hash_mismatch error
# ---------------------------------------------------------------------------

def test_tampered_line_hash(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    obj = json.loads(_build_valid_line())
    obj["line_hash"] = "deadbeef" * 8  # tampered
    _write_ledger(ledger, [json.dumps(obj, separators=(",", ":"), sort_keys=True)])
    result = ledger_validate.validate(ledger)
    assert result["valid"] is False
    assert any(e["kind"] == "line_hash_mismatch" for e in result["errors"])


# ---------------------------------------------------------------------------
# 12 — Multi-line valid chain passes
# ---------------------------------------------------------------------------

def test_multi_line_valid_chain(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    line1 = _build_valid_line(event="discovered", prev_hash="")
    hash1 = json.loads(line1)["line_hash"]
    line2 = _build_valid_line(event="review_1", prev_hash=hash1)
    hash2 = json.loads(line2)["line_hash"]
    line3 = _build_valid_line(event="review_2", prev_hash=hash2)
    _write_ledger(ledger, [line1, line2, line3])
    result = ledger_validate.validate(ledger)
    assert result["valid"] is True
    assert result["lines_checked"] == 3


# ---------------------------------------------------------------------------
# 13 — prev_hash_mismatch reported with correct line number
# ---------------------------------------------------------------------------

def test_prev_hash_mismatch_line_number(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    line1 = _build_valid_line(event="discovered", prev_hash="")
    # line2 intentionally uses wrong prev_hash
    line2 = _build_valid_line(event="review_1", prev_hash="wrong" * 12 + "0000")
    _write_ledger(ledger, [line1, line2])
    result = ledger_validate.validate(ledger)
    mismatch_errors = [e for e in result["errors"] if e["kind"] == "prev_hash_mismatch"]
    assert len(mismatch_errors) == 1
    assert mismatch_errors[0]["line_number"] == 2


# ---------------------------------------------------------------------------
# 14 — lines_checked reflects actual non-blank lines
# ---------------------------------------------------------------------------

def test_lines_checked_count(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    line1 = _build_valid_line(event="discovered", prev_hash="")
    h1 = json.loads(line1)["line_hash"]
    line2 = _build_valid_line(event="review_1", prev_hash=h1)
    _write_ledger(ledger, [line1, line2])
    result = ledger_validate.validate(ledger)
    assert result["lines_checked"] == 2


# ---------------------------------------------------------------------------
# 15 — --strict-signature flag accepted; skips gracefully when git unavailable
# ---------------------------------------------------------------------------

def test_strict_signature_no_git_skips_gracefully(tmp_path: Path) -> None:
    """When no commit is found for a line, an error is reported (not a crash)."""
    ledger = tmp_path / "patch-ledger.jsonl"
    line1 = _build_valid_line(event="discovered", prev_hash="")
    _write_ledger(ledger, [line1])

    # Simulate git being unavailable / commit not found by returning None
    with patch.object(ledger_validate, "_find_commit_for_line_hash", return_value=None):
        result = ledger_validate.validate(ledger, strict_signature=True)

    # When no commit can be found, validation fails with missing_gpg_signature
    assert result["valid"] is False
    assert any(e["kind"] == "missing_gpg_signature" for e in result["errors"])


# ---------------------------------------------------------------------------
# 16 — Output JSON structure has all expected top-level keys
# ---------------------------------------------------------------------------

def test_output_json_structure() -> None:
    result = ledger_validate.validate(FIXTURES / "ledger-valid.jsonl")
    assert "valid" in result
    assert "lines_checked" in result
    assert "errors" in result
    assert isinstance(result["valid"], bool)
    assert isinstance(result["lines_checked"], int)
    assert isinstance(result["errors"], list)


# ---------------------------------------------------------------------------
# 17 — merged_verdict event passes validation (CRIT-2 fix)
# ---------------------------------------------------------------------------

def test_merged_verdict_event_is_valid(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    line1 = _build_valid_line(event="merged_verdict", prev_hash="")
    _write_ledger(ledger, [line1])
    result = ledger_validate.validate(ledger)
    assert result["valid"] is True
    assert result["lines_checked"] == 1


# ---------------------------------------------------------------------------
# 18 — --strict-signature uses _find_commit_for_line_hash, not index (MED-5 fix)
# ---------------------------------------------------------------------------

def test_strict_signature_uses_line_hash_lookup(tmp_path: Path) -> None:
    """Strict-signature must call _find_commit_for_line_hash per line, not use index."""
    ledger = tmp_path / "patch-ledger.jsonl"
    line1 = _build_valid_line(event="discovered", prev_hash="")
    _write_ledger(ledger, [line1])

    line1_hash = json.loads(line1)["line_hash"]
    looked_up_hashes: list[str] = []

    def fake_find(lh: str) -> str | None:
        looked_up_hashes.append(lh)
        return "deadbeef" * 5  # fake commit SHA

    with patch.object(ledger_validate, "_find_commit_for_line_hash", side_effect=fake_find):
        with patch.object(ledger_validate, "_check_gpg_signature", return_value=True):
            result = ledger_validate.validate(ledger, strict_signature=True)

    # The lookup should have been called with the actual line_hash
    assert line1_hash in looked_up_hashes, (
        "strict-signature should look up commit by line_hash, not array index"
    )
    assert result["valid"] is True


# ---------------------------------------------------------------------------
# 19 — --strict-signature: commit found but unsigned → missing_gpg_signature error
# ---------------------------------------------------------------------------

def test_strict_signature_unsigned_commit_fails(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    line1 = _build_valid_line(event="discovered", prev_hash="")
    _write_ledger(ledger, [line1])

    with patch.object(ledger_validate, "_find_commit_for_line_hash", return_value="abc123"):
        with patch.object(ledger_validate, "_check_gpg_signature", return_value=False):
            result = ledger_validate.validate(ledger, strict_signature=True)

    assert result["valid"] is False
    assert any(e["kind"] == "missing_gpg_signature" for e in result["errors"])
