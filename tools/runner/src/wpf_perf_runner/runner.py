"""Runner — orchestrates a single (or multi-run) perf scenario execution.

Sequence per run:
  1. Validate inputs (executables, env vars).
  2. Stage perf-hive (copy XMLs, apply seed.sql if present).
  3. Set env vars (MC_PERF_MODE, MC_HEADLESS).
  4. Start PerfView ETW capture.
  5. Launch MC via UiMcpHost broker (mc_connect / Mode 1).
  6. Execute scenario steps sequentially.
  7. Shutdown MC (mc_disconnect + wait for process exit).
  8. Stop PerfView (flush ETL).
  9. Emit run result JSON.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import tempfile
import time
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .mcp_driver import McpDriver, _find_ui_mcp_host
from .perfview import PerfViewCapture, find_perfview
from .scenario import (
    PerfEventStep,
    Scenario,
    ShutdownStep,
    Step,
    ToolStep,
    WaitIdleStep,
    WaitMsStep,
    WaitReadyStep,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class StepTiming:
    name: str
    step_type: str
    elapsed_ms: float
    ok: bool
    error: str = ""


@dataclass
class RunResult:
    scenario: str
    run: int
    wpf_sha: str
    etl_path: str
    started_utc: str
    ended_utc: str
    duration_ms: float
    step_timings: list[StepTiming]
    exit_code: int
    scenario_complete: bool
    is_warmup: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stepTimings"] = [
            {
                "name": st["name"],
                "stepType": st["step_type"],
                "elapsedMs": st["elapsed_ms"],
                "ok": st["ok"],
                "error": st["error"],
            }
            for st in d.pop("step_timings")
        ]
        d.pop("step_timings", None)
        return {
            "scenario": d["scenario"],
            "run": d["run"],
            "wpfSha": d["wpf_sha"],
            "etlPath": d["etl_path"],
            "startedUtc": d["started_utc"],
            "endedUtc": d["ended_utc"],
            "durationMs": d["duration_ms"],
            "stepTimings": d["stepTimings"],
            "exitCode": d["exit_code"],
            "scenarioComplete": d["scenario_complete"],
            "isWarmup": d["is_warmup"],
            "errors": d["errors"],
        }


# ---------------------------------------------------------------------------
# Runner configuration
# ---------------------------------------------------------------------------

@dataclass
class RunnerConfig:
    scenario_path: Path
    wpf_sha: str
    runs: int
    out_dir: Path
    mc_build: Path
    ui_mcp_host_bin: str | None = None
    perfview_path: str | None = None
    perf_hive_dir: Path | None = None
    screens_yml: Path | None = None


# ---------------------------------------------------------------------------
# Hive staging
# ---------------------------------------------------------------------------

def _stage_hive(perf_hive_src: Path | None, run_index: int, tmp_root: Path) -> Path:
    """Create a fresh temporary hive for this run.

    Copies XMLs from perf_hive_src (if provided) and applies seed.sql
    if present in the source dir.
    """
    hive_dir = tmp_root / f"perf-hive-run{run_index}"
    hive_dir.mkdir(parents=True, exist_ok=True)
    settings_dir = hive_dir / "settings"
    settings_dir.mkdir(exist_ok=True)

    if perf_hive_src and perf_hive_src.exists():
        for xml_file in perf_hive_src.glob("*.xml"):
            shutil.copy2(xml_file, settings_dir / xml_file.name)
            print(f"[hive] Copied {xml_file.name} → {settings_dir}", flush=True)

        seed_sql = perf_hive_src / "seed.sql"
        if seed_sql.exists():
            db_path = hive_dir / "perf.s3db"
            print(f"[hive] Applying seed.sql → {db_path}", flush=True)
            try:
                conn = sqlite3.connect(str(db_path))
                conn.executescript(seed_sql.read_text(encoding="utf-8"))
                conn.commit()
                conn.close()
                print("[hive] seed.sql applied.", flush=True)
            except Exception as exc:
                print(f"[hive] WARNING: seed.sql failed: {exc}", flush=True)
    else:
        print("[hive] No perf-hive source configured; using empty hive.", flush=True)

    return hive_dir


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class RunnerError(Exception):
    """Non-recoverable runner configuration error."""


def _validate_inputs(cfg: RunnerConfig) -> tuple[str, str]:
    """Validate required executables and env vars.

    Returns:
        (perfview_win_path, ui_mcp_host_bin_path)

    Raises:
        RunnerError on any validation failure.
    """
    errors: list[str] = []

    # PerfView
    try:
        perfview_path = find_perfview(cfg.perfview_path)
    except FileNotFoundError as exc:
        errors.append(str(exc))
        perfview_path = cfg.perfview_path or ""

    # UiMcpHost
    try:
        ui_mcp_host = _find_ui_mcp_host(cfg.ui_mcp_host_bin)
    except FileNotFoundError as exc:
        errors.append(str(exc))
        ui_mcp_host = cfg.ui_mcp_host_bin or ""

    # MC build
    mc_exe = cfg.mc_build / "MotionCatalyst-cli.exe"
    if not cfg.mc_build.exists():
        errors.append(f"MC build dir not found: {cfg.mc_build}")
    elif not mc_exe.exists():
        errors.append(f"MotionCatalyst-cli.exe not found in: {cfg.mc_build}")

    # _NT_SYMBOL_PATH warning (not fatal)
    if not os.environ.get("_NT_SYMBOL_PATH"):
        warnings.warn(
            "_NT_SYMBOL_PATH is not set. Symbol resolution in traces will be degraded. "
            "Set: _NT_SYMBOL_PATH=srv*C:\\symbols*https://msdl.microsoft.com/download/symbols",
            stacklevel=2,
        )

    if errors:
        raise RunnerError("Input validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    return perfview_path, ui_mcp_host


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def _step_name(step: Step, index: int) -> str:
    """Return a display name for a step."""
    if isinstance(step, ToolStep) and step.name:
        return step.name
    if isinstance(step, (WaitIdleStep, WaitMsStep)) and step.name:
        return step.name
    if isinstance(step, ToolStep):
        return f"tool:{step.tool}"
    if isinstance(step, WaitReadyStep):
        return "wait-ready"
    if isinstance(step, WaitIdleStep):
        return "wait-idle"
    if isinstance(step, WaitMsStep):
        return f"wait-ms:{step.ms}"
    if isinstance(step, PerfEventStep):
        return f"perf-event:{step.event}"
    if isinstance(step, ShutdownStep):
        return "shutdown"
    return f"step-{index}"


def _execute_step(
    step: Step,
    driver: McpDriver,
    index: int,
) -> StepTiming:
    """Execute one scenario step and return its timing.

    Never raises — errors are captured in StepTiming.error.
    """
    name = _step_name(step, index)
    t0 = time.perf_counter()
    ok = True
    error = ""

    try:
        if isinstance(step, WaitReadyStep):
            # wait-ready is handled before the step loop (via SentinelWatcher).
            # If we see it mid-scenario, it's a no-op (already ready).
            pass

        elif isinstance(step, ToolStep):
            result = driver.call_tool(step.tool, step.args, timeout=step.timeout_ms / 1000.0)
            envelope = result.get("ok_envelope") or {}
            if envelope and not envelope.get("ok", True):
                err_info = envelope.get("error") or {}
                ok = False
                error = f"{err_info.get('code', 'UNKNOWN')}: {err_info.get('message', '')}"
                print(f"[step] {name} FAILED: {error}", flush=True)
            else:
                print(f"[step] {name} OK", flush=True)

        elif isinstance(step, WaitIdleStep):
            args: dict[str, Any] = {}
            if step.max_timeout_ms:
                args["maxTimeoutMs"] = step.max_timeout_ms
            if step.wait_for_layout_quiescence:
                args["waitForLayoutQuiescence"] = True
            result = driver.call_tool("wpf_pump_until_idle", args,
                                      timeout=(step.max_timeout_ms + 5_000) / 1000.0)
            print(f"[step] {name} OK", flush=True)

        elif isinstance(step, WaitMsStep):
            time.sleep(step.ms / 1000.0)
            print(f"[step] {name} OK (slept {step.ms}ms)", flush=True)

        elif isinstance(step, PerfEventStep):
            # Emit perf event — for now, just log to stdout.
            # When MotionCatalyst-PerfHarness EventSource is available (W1.3),
            # this will call the mc_* tool that triggers it.
            print(
                f"[perf-event] event={step.event} label={step.label!r}",
                flush=True,
            )

        elif isinstance(step, ShutdownStep):
            # Shutdown is handled at the runner level after the step loop.
            pass

    except Exception as exc:
        ok = False
        error = str(exc)
        print(f"[step] {name} ERROR: {exc}", flush=True)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return StepTiming(
        name=name,
        step_type=step.type,
        elapsed_ms=round(elapsed_ms, 2),
        ok=ok,
        error=error,
    )


# ---------------------------------------------------------------------------
# Single-run execution
# ---------------------------------------------------------------------------

def _execute_run(
    scenario: Scenario,
    cfg: RunnerConfig,
    run_index: int,
    is_warmup: bool,
    perfview_path: str,
    ui_mcp_host_bin: str,
    tmp_dir: Path,
) -> RunResult:
    """Execute one perf run. Handles PerfView + MC lifecycle."""
    ts = datetime.datetime.now(datetime.UTC)
    label = f"{scenario.name}-{cfg.wpf_sha}-run{run_index}"
    if is_warmup:
        label += "-warmup"

    etl_path = cfg.out_dir / f"{label}.etl"
    log_path = cfg.out_dir / f"{label}-perfview.log"
    result_path = cfg.out_dir / f"{label}.runner.json"

    started_utc = ts.isoformat()
    run_errors: list[str] = []
    step_timings: list[StepTiming] = []
    scenario_complete = False
    mc_exit_code = 0

    # Stage hive.
    hive_dir = _stage_hive(cfg.perf_hive_dir, run_index, tmp_dir)

    # MC environment variables.
    mc_env: dict[str, str] = {
        "MC_PERF_MODE": "1",
        "MC_HEADLESS": "1",
    }

    driver = McpDriver(
        ui_mcp_host_bin=ui_mcp_host_bin,
        mc_build=cfg.mc_build,
        hive_dir=hive_dir,
        extra_env=mc_env,
        screens_yml=cfg.screens_yml,
    )

    pv = PerfViewCapture(
        perfview_path=perfview_path,
        etl_path=etl_path,
        log_path=log_path,
    )

    t_run_start = time.perf_counter()

    try:
        # Step 4: Start PerfView.
        print(f"\n[runner] Run {run_index} {'(warmup)' if is_warmup else ''}: starting PerfView...",
              flush=True)
        pv.start()
        if not pv.wait_started(timeout=10.0):
            run_errors.append("PerfView did not confirm collection start within 10s.")

        # Step 5: Launch MC via UiMcpHost.
        print("[runner] Starting UiMcpHost + MC...", flush=True)
        driver.start()

        print("[runner] Connecting MC (mc_connect)...", flush=True)
        try:
            driver.connect_mc(
                wait_for_idle=True,
                idle_timeout_ms=60_000,
                wait_for_element="HomeViewControl",
                element_timeout_ms=90_000,
            )
        except RuntimeError as exc:
            run_errors.append(f"MC launch failed: {exc}")
            mc_exit_code = 1

        # Step 6: Execute warmup steps (not timed in results).
        if scenario.warmup_steps:
            print(f"[runner] Executing {len(scenario.warmup_steps)} warmup steps...", flush=True)
            for i, step in enumerate(scenario.warmup_steps):
                _execute_step(step, driver, i)

        # Execute measured steps.
        print(f"[runner] Executing {len(scenario.steps)} scenario steps...", flush=True)
        for i, step in enumerate(scenario.steps):
            if not driver.is_alive():
                run_errors.append(f"UiMcpHost died before step {i} ({_step_name(step, i)})")
                mc_exit_code = driver.return_code or 1
                break
            timing = _execute_step(step, driver, i)
            step_timings.append(timing)

        scenario_complete = True  # steps completed without crash

        # Step 7: Shutdown MC.
        print("[runner] Disconnecting MC...", flush=True)
        try:
            driver.disconnect(close_target=True)
            # Give MC time to exit cleanly.
            shutdown_timeout = scenario.shutdown_timeout_ms / 1000.0
            _wait_for_process_exit(driver, timeout=shutdown_timeout)
        except Exception as exc:
            run_errors.append(f"Disconnect error: {exc}")

    except KeyboardInterrupt:
        run_errors.append("Interrupted by user (Ctrl+C).")
        scenario_complete = False

    except Exception as exc:
        run_errors.append(f"Unexpected runner error: {exc}")
        scenario_complete = False

    finally:
        # Always stop PerfView, even on error.
        print("[runner] Stopping PerfView...", flush=True)
        try:
            pv.stop()
        except Exception as exc:
            run_errors.append(f"PerfView stop error: {exc}")

        # Kill UiMcpHost if it's still alive (error path).
        driver.kill()

    t_run_end = time.perf_counter()
    ended_utc = datetime.datetime.now(datetime.UTC).isoformat()
    duration_ms = round((t_run_end - t_run_start) * 1000.0, 2)

    result = RunResult(
        scenario=scenario.name,
        run=run_index,
        wpf_sha=cfg.wpf_sha,
        etl_path=str(etl_path),
        started_utc=started_utc,
        ended_utc=ended_utc,
        duration_ms=duration_ms,
        step_timings=step_timings,
        exit_code=mc_exit_code,
        scenario_complete=scenario_complete,
        is_warmup=is_warmup,
        errors=run_errors,
    )

    # Write sidecar result JSON.
    result_dict = result.to_dict()
    result_path.write_text(json.dumps(result_dict, indent=2), encoding="utf-8")
    print(f"[runner] Result written: {result_path}", flush=True)

    return result


def _wait_for_process_exit(driver: McpDriver, timeout: float) -> None:
    """Wait for UiMcpHost process to exit after disconnect."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not driver.is_alive():
            return
        time.sleep(0.5)
    print(f"[runner] WARNING: UiMcpHost still alive after {timeout:.0f}s shutdown timeout.",
          flush=True)


# ---------------------------------------------------------------------------
# Multi-run orchestration
# ---------------------------------------------------------------------------

class Runner:
    """Top-level runner that orchestrates N perf runs of a scenario."""

    def __init__(self, cfg: RunnerConfig) -> None:
        self.cfg = cfg

    def run(self, scenario: Scenario) -> list[RunResult]:
        """Execute all runs and return their results.

        The first run is always treated as a warmup run (discarded from
        aggregates but kept as a file). Subsequent runs are measured.

        Returns:
            List of RunResult objects (warmup first, then measured runs).
        """
        cfg = self.cfg

        # Validate executables.
        perfview_path, ui_mcp_host_bin = _validate_inputs(cfg)

        # Prepare output directory.
        cfg.out_dir.mkdir(parents=True, exist_ok=True)

        # Temporary working root for hives etc.
        tmp_dir = Path(tempfile.mkdtemp(prefix="wpf-perf-"))
        print(f"[runner] Temp dir: {tmp_dir}", flush=True)

        results: list[RunResult] = []
        total_runs = cfg.runs + 1  # +1 for warmup

        try:
            for i in range(total_runs):
                is_warmup = (i == 0)
                run_index = i  # 0=warmup, 1..N=measured
                print(
                    f"\n[runner] ===== Run {run_index}/{total_runs - 1} "
                    f"{'(WARMUP)' if is_warmup else '(MEASURED)'} =====",
                    flush=True,
                )
                result = _execute_run(
                    scenario=scenario,
                    cfg=cfg,
                    run_index=run_index,
                    is_warmup=is_warmup,
                    perfview_path=perfview_path,
                    ui_mcp_host_bin=ui_mcp_host_bin,
                    tmp_dir=tmp_dir,
                )
                results.append(result)
                print(
                    f"[runner] Run {run_index} complete: "
                    f"durationMs={result.duration_ms:.0f} "
                    f"scenarioComplete={result.scenario_complete} "
                    f"exitCode={result.exit_code}",
                    flush=True,
                )

        finally:
            # Clean up temp dir.
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        # Write aggregate summary.
        measured = [r for r in results if not r.is_warmup]
        summary = {
            "scenario": scenario.name,
            "wpfSha": cfg.wpf_sha,
            "runs": [r.to_dict() for r in results],
            "measuredRunCount": len(measured),
            "successCount": sum(1 for r in measured if r.scenario_complete),
            "totalDurationMs": sum(r.duration_ms for r in measured),
        }
        summary_path = cfg.out_dir / f"{scenario.name}-{cfg.wpf_sha}-summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\n[runner] Aggregate summary: {summary_path}", flush=True)

        # Emit summary to stdout.
        print(json.dumps(summary, indent=2), flush=True)

        return results
