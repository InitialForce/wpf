"""Tests for tools/check-config-schema.py.

Covers all validation gates specified in bead wpf-1d7:
  - Gate 1: valid config → exit 0, valid: True
  - Gate 2: malformed YAML → error
  - Gate 3: missing required field → validation error
  - Gate 4: type mismatch → validation error
  - Gate 5: unknown enum value (schema_version) → validation error
  - Gate 6: missing config file → exit 2
  - Gate 7: missing schema file → exit 2
  - Gate 8: output file is written when --output given
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Module loader for hyphenated filename
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).parent.parent / "tools" / "check-config-schema.py"
_SCHEMA = Path(__file__).parent.parent / "tools" / "config-schema.json"
_CONFIG = Path(__file__).parent.parent / ".if-fork" / "config.yaml"

_spec = importlib.util.spec_from_file_location("check_config_schema", _SCRIPT)
assert _spec is not None and _spec.loader is not None
check_config_schema: types.ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_config_schema)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(path: Path, data: object) -> None:
    """Serialise *data* as YAML and write to *path*."""
    path.write_text(yaml.dump(data), encoding="utf-8")


def _minimal_valid_config() -> dict[str, object]:
    """Return a minimal config dict that satisfies the schema."""
    return {
        "schema_version": 1,
        "upstream": {
            "repo": "dotnet/wpf",
            "tracked_branch": "release/10.0",
            "remote_name": "upstream",
        },
        "fork": {
            "org": "InitialForce",
            "repo": "wpf",
            "active_release_branches": ["if/release/10.0"],
            "staging_branch": "if/staging",
            "mirror_branch": "if/mirror/release/10.0",
        },
        "author_allowlist": ["h3xds1nz"],
        "file_denylist": ["NuGet.config"],
        "tier_predicates": {
            "s": {
                "max_files_touched": 5,
                "max_loc_delta": 200,
                "min_review_confidence": 0.92,
                "allowed_categories": ["perf"],
            },
            "a": {
                "max_files_touched": 20,
                "max_loc_delta": 800,
                "min_review_confidence": 0.80,
            },
            "b": {
                "min_review_confidence": 0.60,
            },
        },
        "review_hard_fail_patterns": ["\\[DllImport"],
        "perf": {
            "regression_threshold_pct": 5.0,
            "auto_reject_threshold_pct": 15.0,
        },
        "human_gate_environments": {
            "release": "wpf-nuget-publish",
            "branch_promotion": "branch-promotion",
            "bot_credentials": "bot-credentials",
        },
        "conflict_resolution": {
            "max_hunk_count": 3,
            "max_conflict_lines": 80,
            "min_confidence": 0.85,
            "pause_run_if_escalation_pct_exceeds": 30,
        },
        "stable_adoption": {
            "abort_run_if_escalation_pct_exceeds": 30,
        },
        "ledger": {
            "path": ".if-fork/patch-ledger.jsonl",
            "state_path": ".if-fork/patch-state.json",
            "absorbed_manifest_path": ".if-fork/absorbed-upstream.json",
        },
        "claude_models": {
            "triage": "claude-haiku-4-5",
            "review_1": "claude-opus-4-7",
            "review_2": "claude-opus-4-7",
            "cherry_pick": "claude-sonnet-4-6",
            "conflict_resolve": "claude-sonnet-4-6",
            "release_notes": "claude-sonnet-4-6",
            "failure_analysis": "claude-sonnet-4-6",
        },
        "claude_limits": {
            "max_turns_triage": 8,
            "max_turns_review": 12,
            "max_turns_cherry_pick": 15,
            "max_turns_conflict": 20,
            "daily_token_cap_usd": 25,
            "monthly_token_cap_usd": 200,
        },
        "bot_identity": {
            "name": "Claude (Initial Force WPF Bot)",
            "email": "wpf-bot@initialforce.com",
            "github_app_id_secret": "GH_APP_ID",
            "github_app_private_key_secret": "GH_APP_PRIVATE_KEY",
        },
        "autonomy_kill_switches": {
            "enabled_var": "IF_AUTONOMY_ENABLED",
            "automerge_frozen_var": "IF_AUTOMERGE_FROZEN",
        },
    }


# ---------------------------------------------------------------------------
# Gate 1 — canonical config.yaml validates successfully
# ---------------------------------------------------------------------------


def test_canonical_config_is_valid() -> None:
    """The committed .if-fork/config.yaml must pass schema validation."""
    result = check_config_schema.validate(_CONFIG, _SCHEMA)
    assert result["valid"] is True, f"Validation errors: {result['errors']}"
    assert result["errors"] == []


def test_canonical_config_exit_0() -> None:
    """CLI invocation on canonical config exits 0."""
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--config",
            str(_CONFIG),
            "--schema",
            str(_SCHEMA),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    out = json.loads(proc.stdout)
    assert out["valid"] is True


# ---------------------------------------------------------------------------
# Gate 2 — malformed YAML is rejected
# ---------------------------------------------------------------------------


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    """A YAML syntax error should propagate as an exception from validate()."""
    bad_yaml = tmp_path / "config.yaml"
    bad_yaml.write_text("schema_version: [\nnot closed", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        check_config_schema.validate(bad_yaml, _SCHEMA)


def test_malformed_yaml_exit_1(tmp_path: Path) -> None:
    """CLI exits 1 (internal error) when YAML parsing fails."""
    bad_yaml = tmp_path / "config.yaml"
    bad_yaml.write_text("schema_version: [\nnot closed", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--config",
            str(bad_yaml),
            "--schema",
            str(_SCHEMA),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1


# ---------------------------------------------------------------------------
# Gate 3 — missing required field
# ---------------------------------------------------------------------------


def test_missing_required_field_detected(tmp_path: Path) -> None:
    """Dropping a required top-level field produces a validation error."""
    cfg = _minimal_valid_config()
    del cfg["upstream"]  # type: ignore[misc]
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, cfg)
    result = check_config_schema.validate(config_path, _SCHEMA)
    assert result["valid"] is False
    assert any("upstream" in e["message"] for e in result["errors"])


def test_missing_nested_required_field(tmp_path: Path) -> None:
    """Dropping a nested required field (upstream.repo) is caught."""
    cfg = _minimal_valid_config()
    del cfg["upstream"]["repo"]  # type: ignore[index,union-attr]
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, cfg)
    result = check_config_schema.validate(config_path, _SCHEMA)
    assert result["valid"] is False


# ---------------------------------------------------------------------------
# Gate 4 — type mismatch
# ---------------------------------------------------------------------------


def test_type_mismatch_schema_version(tmp_path: Path) -> None:
    """schema_version must be an integer; a string value is rejected."""
    cfg = _minimal_valid_config()
    cfg["schema_version"] = "one"  # type: ignore[assignment]
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, cfg)
    result = check_config_schema.validate(config_path, _SCHEMA)
    assert result["valid"] is False


def test_type_mismatch_perf_threshold(tmp_path: Path) -> None:
    """perf.regression_threshold_pct must be a number; string is rejected."""
    cfg = _minimal_valid_config()
    cfg["perf"]["regression_threshold_pct"] = "five"  # type: ignore[index]
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, cfg)
    result = check_config_schema.validate(config_path, _SCHEMA)
    assert result["valid"] is False


# ---------------------------------------------------------------------------
# Gate 5 — unknown enum value (schema_version not in [1])
# ---------------------------------------------------------------------------


def test_unknown_schema_version_rejected(tmp_path: Path) -> None:
    """schema_version values outside the allowed enum are rejected."""
    cfg = _minimal_valid_config()
    cfg["schema_version"] = 99  # type: ignore[assignment]
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, cfg)
    result = check_config_schema.validate(config_path, _SCHEMA)
    assert result["valid"] is False


# ---------------------------------------------------------------------------
# Gate 6 — missing config file → exit 2
# ---------------------------------------------------------------------------


def test_missing_config_file_exit_2(tmp_path: Path) -> None:
    """CLI exits 2 when the config file does not exist."""
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--config",
            str(tmp_path / "nonexistent.yaml"),
            "--schema",
            str(_SCHEMA),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    out = json.loads(proc.stdout)
    assert out["valid"] is False


# ---------------------------------------------------------------------------
# Gate 7 — missing schema file → exit 2
# ---------------------------------------------------------------------------


def test_missing_schema_file_exit_2(tmp_path: Path) -> None:
    """CLI exits 2 when the schema file does not exist."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _minimal_valid_config())
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--config",
            str(config_path),
            "--schema",
            str(tmp_path / "nonexistent-schema.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    out = json.loads(proc.stdout)
    assert out["valid"] is False


# ---------------------------------------------------------------------------
# Gate 8 — --output writes JSON report file
# ---------------------------------------------------------------------------


def test_output_flag_writes_file(tmp_path: Path) -> None:
    """--output <path> writes the JSON report to disk."""
    report_path = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--config",
            str(_CONFIG),
            "--schema",
            str(_SCHEMA),
            "--output",
            str(report_path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["valid"] is True


# ---------------------------------------------------------------------------
# Additional: result dict structure
# ---------------------------------------------------------------------------


def test_result_structure_keys() -> None:
    """validate() always returns a dict with the expected top-level keys."""
    result = check_config_schema.validate(_CONFIG, _SCHEMA)
    assert "valid" in result
    assert "config_path" in result
    assert "schema_path" in result
    assert "errors" in result
    assert isinstance(result["valid"], bool)
    assert isinstance(result["errors"], list)
