"""Smoke tests for the runner using fakes — does not launch MC, UiMcpHost, or PerfView.

Tests the runner's step execution logic, timing collection, and result
structure using in-memory fakes for the MCP driver and PerfView.
"""

from __future__ import annotations

import json
import time
from typing import Any

from wpf_perf_runner.runner import (
    RunResult,
    StepTiming,
    _execute_step,
    _step_name,
)
from wpf_perf_runner.scenario import (
    PerfEventStep,
    ShutdownStep,
    ToolStep,
    WaitIdleStep,
    WaitMsStep,
    WaitReadyStep,
)

# ---------------------------------------------------------------------------
# Fake McpDriver
# ---------------------------------------------------------------------------

class FakeMcpDriver:
    """In-memory fake for McpDriver. Records calls and returns configurable responses."""

    def __init__(self, *, response_ok: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response_ok = response_ok
        self._alive = True

    def call_tool(
        self, name: str, args: dict[str, Any], *, timeout: float = 60.0
    ) -> dict[str, Any]:
        self.calls.append((name, args))
        if self.response_ok:
            return {"ok_envelope": {"ok": True, "data": {}}}
        return {"ok_envelope": {"ok": False, "error": {"code": "FAKE_ERROR", "message": "fake"}}}

    def is_alive(self) -> bool:
        return self._alive

    @property
    def return_code(self) -> int | None:
        return None if self._alive else 0


# ---------------------------------------------------------------------------
# Tests for _step_name
# ---------------------------------------------------------------------------

class TestStepName:
    def test_tool_step_with_name(self) -> None:
        step = ToolStep(type="tool", tool="wpf_click", name="click-play")
        assert _step_name(step, 0) == "click-play"

    def test_tool_step_without_name(self) -> None:
        step = ToolStep(type="tool", tool="wpf_click")
        assert _step_name(step, 0) == "tool:wpf_click"

    def test_wait_ready(self) -> None:
        step = WaitReadyStep(type="wait-ready")
        assert _step_name(step, 0) == "wait-ready"

    def test_wait_idle_with_name(self) -> None:
        step = WaitIdleStep(type="wait-idle", name="idle-after-launch")
        assert _step_name(step, 1) == "idle-after-launch"

    def test_wait_idle_without_name(self) -> None:
        step = WaitIdleStep(type="wait-idle")
        assert _step_name(step, 2) == "wait-idle"

    def test_wait_ms(self) -> None:
        step = WaitMsStep(type="wait-ms", ms=500)
        assert _step_name(step, 3) == "wait-ms:500"

    def test_perf_event(self) -> None:
        step = PerfEventStep(type="perf-event", event="ScenarioStart")
        assert _step_name(step, 4) == "perf-event:ScenarioStart"

    def test_shutdown(self) -> None:
        step = ShutdownStep(type="shutdown")
        assert _step_name(step, 5) == "shutdown"


# ---------------------------------------------------------------------------
# Tests for _execute_step
# ---------------------------------------------------------------------------

class TestExecuteStep:
    def test_tool_step_ok(self) -> None:
        driver = FakeMcpDriver(response_ok=True)
        step = ToolStep(type="tool", tool="wpf_click", args={"nodeId": "0:1"})
        timing = _execute_step(step, driver, 0)  # type: ignore[arg-type]
        assert timing.ok is True
        assert timing.step_type == "tool"
        assert timing.elapsed_ms >= 0
        assert ("wpf_click", {"nodeId": "0:1"}) in driver.calls

    def test_tool_step_fail(self) -> None:
        driver = FakeMcpDriver(response_ok=False)
        step = ToolStep(type="tool", tool="wpf_click", args={})
        timing = _execute_step(step, driver, 0)  # type: ignore[arg-type]
        assert timing.ok is False
        assert "FAKE_ERROR" in timing.error

    def test_wait_ms_step_timing(self) -> None:
        driver = FakeMcpDriver()
        step = WaitMsStep(type="wait-ms", ms=50)
        t0 = time.perf_counter()
        timing = _execute_step(step, driver, 0)  # type: ignore[arg-type]
        elapsed = (time.perf_counter() - t0) * 1000.0
        assert timing.ok is True
        assert timing.elapsed_ms >= 40  # Allow some slack on slow CI.
        assert elapsed >= 40

    def test_wait_idle_step(self) -> None:
        driver = FakeMcpDriver(response_ok=True)
        step = WaitIdleStep(type="wait-idle", max_timeout_ms=5000)
        timing = _execute_step(step, driver, 0)  # type: ignore[arg-type]
        assert timing.ok is True
        assert any(name == "wpf_pump_until_idle" for name, _ in driver.calls)

    def test_wait_idle_passes_args(self) -> None:
        driver = FakeMcpDriver(response_ok=True)
        step = WaitIdleStep(
            type="wait-idle",
            max_timeout_ms=15000,
            wait_for_layout_quiescence=True,
        )
        _execute_step(step, driver, 0)  # type: ignore[arg-type]
        call_args = next(args for name, args in driver.calls if name == "wpf_pump_until_idle")
        assert call_args.get("maxTimeoutMs") == 15000
        assert call_args.get("waitForLayoutQuiescence") is True

    def test_perf_event_step_noop(self) -> None:
        """perf-event step is a no-op log at this stage (W1.3 not yet implemented)."""
        driver = FakeMcpDriver()
        step = PerfEventStep(type="perf-event", event="ScenarioStart", label="v1")
        timing = _execute_step(step, driver, 0)  # type: ignore[arg-type]
        assert timing.ok is True
        assert len(driver.calls) == 0  # no MCP call made

    def test_wait_ready_step_noop_when_already_ready(self) -> None:
        """wait-ready mid-scenario is treated as a no-op."""
        driver = FakeMcpDriver()
        step = WaitReadyStep(type="wait-ready")
        timing = _execute_step(step, driver, 0)  # type: ignore[arg-type]
        assert timing.ok is True
        assert len(driver.calls) == 0

    def test_shutdown_step_noop_in_execute(self) -> None:
        """shutdown step is handled at the runner level, not in _execute_step."""
        driver = FakeMcpDriver()
        step = ShutdownStep(type="shutdown")
        timing = _execute_step(step, driver, 0)  # type: ignore[arg-type]
        assert timing.ok is True
        assert len(driver.calls) == 0

    def test_exception_captured_in_timing(self) -> None:
        """Exceptions from call_tool are captured, not propagated."""
        class BrokenDriver:
            def call_tool(self, name: str, args: dict, *, timeout: float = 60.0) -> dict:
                raise RuntimeError("boom")

            def is_alive(self) -> bool:
                return True

        driver = BrokenDriver()
        step = ToolStep(type="tool", tool="wpf_click", args={})
        timing = _execute_step(step, driver, 0)  # type: ignore[arg-type]
        assert timing.ok is False
        assert "boom" in timing.error


# ---------------------------------------------------------------------------
# Tests for RunResult.to_dict
# ---------------------------------------------------------------------------

class TestRunResultToDict:
    def test_to_dict_structure(self) -> None:
        result = RunResult(
            scenario="v1",
            run=1,
            wpf_sha="if.72",
            etl_path="/traces/v1.etl",
            started_utc="2026-05-01T10:00:00+00:00",
            ended_utc="2026-05-01T10:01:00+00:00",
            duration_ms=60000.0,
            step_timings=[
                StepTiming(name="click-play", step_type="tool", elapsed_ms=120.0, ok=True)
            ],
            exit_code=0,
            scenario_complete=True,
            is_warmup=False,
        )
        d = result.to_dict()

        # Check camelCase keys.
        assert d["scenario"] == "v1"
        assert d["wpfSha"] == "if.72"
        assert d["etlPath"] == "/traces/v1.etl"
        assert d["durationMs"] == 60000.0
        assert d["exitCode"] == 0
        assert d["scenarioComplete"] is True
        assert d["isWarmup"] is False

        # Check step timings shape.
        assert len(d["stepTimings"]) == 1
        st = d["stepTimings"][0]
        assert st["name"] == "click-play"
        assert st["stepType"] == "tool"
        assert st["elapsedMs"] == 120.0
        assert st["ok"] is True
        assert st["error"] == ""

    def test_to_dict_roundtrip_json(self) -> None:
        """RunResult.to_dict() must be JSON-serialisable."""
        result = RunResult(
            scenario="smoke",
            run=0,
            wpf_sha="baseline",
            etl_path="",
            started_utc="2026-01-01T00:00:00+00:00",
            ended_utc="2026-01-01T00:00:01+00:00",
            duration_ms=1000.0,
            step_timings=[],
            exit_code=0,
            scenario_complete=True,
            is_warmup=True,
        )
        json_str = json.dumps(result.to_dict())
        parsed = json.loads(json_str)
        assert parsed["isWarmup"] is True
        assert parsed["stepTimings"] == []
