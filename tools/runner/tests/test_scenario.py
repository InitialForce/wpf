"""Unit tests for scenario loading and validation.

These tests do not require MC, UiMcpHost, or PerfView to be installed.
All validation uses the real schema from scenarios/schema.json.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from wpf_perf_runner.scenario import (
    PerfEventStep,
    ScenarioValidationError,
    ShutdownStep,
    ToolStep,
    WaitIdleStep,
    WaitMsStep,
    WaitReadyStep,
    _load_schema,
    _parse_step,
    load_scenario,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tmp(data: dict, suffix: str = ".json") -> Path:
    """Write a dict as JSON to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    )
    json.dump(data, f)
    f.close()
    return Path(f.name)


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

class TestSchemaLoading:
    def test_schema_loads_without_error(self) -> None:
        """The schema file must be findable and loadable from the repo root."""
        schema = _load_schema()
        assert schema.get("$schema") is not None
        assert "step" in schema.get("$defs", {})

    def test_schema_env_override(self, tmp_path: Path) -> None:
        """WPFPERF_SCHEMA_PATH env var overrides auto-discovery."""
        # Build a minimal valid schema.
        minimal = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        }
        schema_file = tmp_path / "test-schema.json"
        schema_file.write_text(json.dumps(minimal))
        orig = os.environ.get("WPFPERF_SCHEMA_PATH")
        try:
            os.environ["WPFPERF_SCHEMA_PATH"] = str(schema_file)
            result = _load_schema()
            assert result["type"] == "object"
        finally:
            if orig is None:
                os.environ.pop("WPFPERF_SCHEMA_PATH", None)
            else:
                os.environ["WPFPERF_SCHEMA_PATH"] = orig


# ---------------------------------------------------------------------------
# Scenario loading — valid cases
# ---------------------------------------------------------------------------

class TestLoadScenarioValid:
    def test_minimal_scenario(self) -> None:
        """Minimal required fields: name + steps."""
        data = {
            "name": "smoke",
            "steps": [{"type": "wait-ready"}],
        }
        path = _write_tmp(data)
        try:
            scenario = load_scenario(path)
            assert scenario.name == "smoke"
            assert len(scenario.steps) == 1
            assert isinstance(scenario.steps[0], WaitReadyStep)
        finally:
            path.unlink()

    def test_full_scenario_all_step_types(self) -> None:
        """Scenario with all step types validates and parses correctly."""
        data = {
            "name": "v1",
            "description": "Full scenario",
            "scenarioVersion": "1.0",
            "warmupSteps": [
                {"type": "wait-ready", "timeoutMs": 90000},
                {"type": "wait-idle", "maxTimeoutMs": 30000},
            ],
            "steps": [
                {"type": "tool", "tool": "mc_navigate_to",
                 "args": {"screenName": "Analysis"}, "timeoutMs": 30000,
                 "name": "navigate-to-analysis", "comment": "Go to analysis"},
                {"type": "wait-idle", "name": "idle-after-nav",
                 "maxTimeoutMs": 15000, "waitForLayoutQuiescence": True},
                {"type": "wait-ms", "ms": 500, "name": "settle"},
                {"type": "perf-event", "event": "ScenarioStart", "label": "v1"},
                {"type": "tool", "tool": "wpf_click",
                 "args": {"selector": {"automationId": "PlayButton"}}},
                {"type": "perf-event", "event": "ScenarioEnd", "label": "v1"},
                {"type": "shutdown", "timeoutMs": 15000},
            ],
            "shutdown": {"timeoutMs": 20000},
        }
        path = _write_tmp(data)
        try:
            scenario = load_scenario(path)
            assert scenario.name == "v1"
            assert scenario.description == "Full scenario"
            assert scenario.scenario_version == "1.0"
            assert len(scenario.warmup_steps) == 2
            assert len(scenario.steps) == 7
            assert scenario.shutdown_timeout_ms == 20000

            # Check warmup step types.
            assert isinstance(scenario.warmup_steps[0], WaitReadyStep)
            assert scenario.warmup_steps[0].timeout_ms == 90000
            assert isinstance(scenario.warmup_steps[1], WaitIdleStep)

            # Check measured step types.
            s = scenario.steps
            assert isinstance(s[0], ToolStep)
            assert s[0].tool == "mc_navigate_to"
            assert s[0].name == "navigate-to-analysis"
            assert s[0].args == {"screenName": "Analysis"}
            assert s[0].timeout_ms == 30000

            assert isinstance(s[1], WaitIdleStep)
            assert s[1].wait_for_layout_quiescence is True
            assert s[1].max_timeout_ms == 15000

            assert isinstance(s[2], WaitMsStep)
            assert s[2].ms == 500

            assert isinstance(s[3], PerfEventStep)
            assert s[3].event == "ScenarioStart"
            assert s[3].label == "v1"

            assert isinstance(s[4], ToolStep)
            assert s[4].tool == "wpf_click"

            assert isinstance(s[5], PerfEventStep)
            assert s[5].event == "ScenarioEnd"

            assert isinstance(s[6], ShutdownStep)
            assert s[6].timeout_ms == 15000
        finally:
            path.unlink()

    def test_tool_step_defaults(self) -> None:
        """Tool step has sensible defaults for optional fields."""
        step = _parse_step({"type": "tool", "tool": "wpf_click"})
        assert isinstance(step, ToolStep)
        assert step.args == {}
        assert step.timeout_ms == 60_000
        assert step.name == ""
        assert step.comment == ""

    def test_wait_idle_defaults(self) -> None:
        wait = _parse_step({"type": "wait-idle"})
        assert isinstance(wait, WaitIdleStep)
        assert wait.max_timeout_ms == 30_000
        assert wait.wait_for_layout_quiescence is False

    def test_wait_ms_step(self) -> None:
        step = _parse_step({"type": "wait-ms", "ms": 250})
        assert isinstance(step, WaitMsStep)
        assert step.ms == 250

    def test_perf_event_step(self) -> None:
        step = _parse_step({"type": "perf-event", "event": "StepStart", "label": "open"})
        assert isinstance(step, PerfEventStep)
        assert step.event == "StepStart"
        assert step.label == "open"

    def test_shutdown_step(self) -> None:
        step = _parse_step({"type": "shutdown", "timeoutMs": 45000})
        assert isinstance(step, ShutdownStep)
        assert step.timeout_ms == 45000

    def test_comment_field_ignored(self) -> None:
        """Comments on any step type are stored but don't affect behavior."""
        step = _parse_step({
            "type": "wait-ms", "ms": 100, "comment": "Let things settle"
        })
        assert isinstance(step, WaitMsStep)
        assert step.comment == "Let things settle"


# ---------------------------------------------------------------------------
# Scenario loading — validation failures
# ---------------------------------------------------------------------------

class TestLoadScenarioInvalid:
    def test_missing_name(self) -> None:
        """Scenario without 'name' must fail."""
        data = {"steps": [{"type": "wait-ready"}]}
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError, match="name"):
                load_scenario(path)
        finally:
            path.unlink()

    def test_missing_steps(self) -> None:
        """Scenario without 'steps' must fail."""
        data = {"name": "no-steps"}
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError):
                load_scenario(path)
        finally:
            path.unlink()

    def test_empty_steps_array(self) -> None:
        """'steps' must have at least one item."""
        data = {"name": "empty", "steps": []}
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError):
                load_scenario(path)
        finally:
            path.unlink()

    def test_invalid_step_type(self) -> None:
        """Unknown step type must fail."""
        data = {
            "name": "bad",
            "steps": [{"type": "unknown-type"}],
        }
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError):
                load_scenario(path)
        finally:
            path.unlink()

    def test_tool_step_missing_tool_field(self) -> None:
        """Tool step without 'tool' field must fail."""
        data = {
            "name": "bad",
            "steps": [{"type": "tool", "args": {}}],
        }
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError, match="tool"):
                load_scenario(path)
        finally:
            path.unlink()

    def test_wait_ms_missing_ms_field(self) -> None:
        """wait-ms without 'ms' field must fail."""
        data = {
            "name": "bad",
            "steps": [{"type": "wait-ms"}],
        }
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError):
                load_scenario(path)
        finally:
            path.unlink()

    def test_perf_event_missing_event_field(self) -> None:
        """perf-event without 'event' field must fail."""
        data = {
            "name": "bad",
            "steps": [{"type": "perf-event"}],
        }
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError):
                load_scenario(path)
        finally:
            path.unlink()

    def test_invalid_json(self) -> None:
        """Malformed JSON must fail with ScenarioValidationError."""
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        f.write("{ this is not json }")
        f.close()
        path = Path(f.name)
        try:
            with pytest.raises(ScenarioValidationError, match="parse error"):
                load_scenario(path)
        finally:
            path.unlink()

    def test_file_not_found(self) -> None:
        """Missing file must raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_scenario("/nonexistent/path/scenario.json")

    def test_extra_properties_rejected(self) -> None:
        """Scenario with extra top-level properties must fail (additionalProperties: false)."""
        data = {
            "name": "extra",
            "steps": [{"type": "wait-ready"}],
            "unexpectedField": "surprise",
        }
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError):
                load_scenario(path)
        finally:
            path.unlink()

    def test_invalid_scenario_version_format(self) -> None:
        """scenarioVersion must match major.minor pattern."""
        data = {
            "name": "bad-ver",
            "scenarioVersion": "1",  # missing .minor
            "steps": [{"type": "wait-ready"}],
        }
        path = _write_tmp(data)
        try:
            with pytest.raises(ScenarioValidationError):
                load_scenario(path)
        finally:
            path.unlink()
