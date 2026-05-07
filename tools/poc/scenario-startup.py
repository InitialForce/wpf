#!/usr/bin/env python3
"""scenario-startup — cold app startup dotnet-trace capture.

Captures a cold-startup trace by:
  1. Dispatching mc_connect in a background thread (it blocks until MC reaches idle).
  2. Tight-polling tasklist for the new MC-CLI PID.
  3. Starting dotnet-trace immediately on PID spawn to capture JIT + WPF init.
  4. Waiting for mc_connect to return (startup complete signal) + SETTLE_S settle.
  5. Stopping the trace cleanly.
  6. Disconnecting MC and killing the tracked PID in the finally block.

Required env vars:
  SCENARIO_RESULT_DIR — directory where the .nettrace file lands
  SCENARIO_NAME       — filename stem for the output, e.g. "startup"

Exit 0 on success, non-zero on any failure.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, "/c/work/wpf-perf/tools/runner/src")
from wpf_perf_runner.mcp_driver import McpDriver, _win_path  # noqa: E402
from wpf_perf_runner.dotnet_trace import (  # noqa: E402
    DotnetTraceCapture, find_dotnet_trace,
)

# ---------------------------------------------------------------------------
# Config — paths
# ---------------------------------------------------------------------------

UIMCP_BIN = (
    "/c/work/desktop/wpf-test/Tools/ui-mcp-host/bin/Release/"
    "net10.0-windows10.0.19041.0/UiMcpHost.exe"
)
MC_BUILD = "/c/work/desktop/wpf-test/src/motioncatalyst/BUILD/x64_Release"
PERF_HIVE_SRC = Path("/c/work/wpf-perf/scenarios/perf-hive")

# ---------------------------------------------------------------------------
# Config — env vars (required)
# ---------------------------------------------------------------------------

_result_dir_raw = os.environ.get("SCENARIO_RESULT_DIR")
_scenario_name_raw = os.environ.get("SCENARIO_NAME")

if not _result_dir_raw:
    print(
        "ERROR: SCENARIO_RESULT_DIR is not set.\n"
        "Usage: SCENARIO_RESULT_DIR=/tmp/out SCENARIO_NAME=startup python3 scenario-startup.py",
        file=sys.stderr,
    )
    sys.exit(1)

if not _scenario_name_raw:
    print(
        "ERROR: SCENARIO_NAME is not set.\n"
        "Usage: SCENARIO_RESULT_DIR=/tmp/out SCENARIO_NAME=startup python3 scenario-startup.py",
        file=sys.stderr,
    )
    sys.exit(1)

RESULT_DIR = Path(_result_dir_raw)
SCENARIO_NAME = _scenario_name_raw

# ---------------------------------------------------------------------------
# Config — timing
# ---------------------------------------------------------------------------

# Cold MC-CLI startup empirically takes 10-25s to reach idle. 35s gives
# headroom for slow machines or post-optimize regressions. dotnet-trace
# stops itself at this duration; we do not try to interrupt it early because
# stdin-based stop is unreliable and leaves the .nettrace locked.
TRACE_DURATION_S = 35

# Extra settle time after mc_connect returns before stopping the trace.
# Allows post-startup lazy work (background GCs, first layout pass) to
# surface in the trace window.
SETTLE_S = 3.0

# How long to wait for mc_connect (=startup idle signal) before giving up.
MAX_IDLE_WAIT_S = 60

# Timeout for PID detection after mc_connect thread dispatches.
PID_POLL_DEADLINE_S = 30.0

# ---------------------------------------------------------------------------
# tasklist helpers (identical pattern to spike-9)
# ---------------------------------------------------------------------------


def _run_tasklist() -> str:
    r = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq MotionCatalyst-cli.exe",
         "/FO", "CSV", "/NH"],
        capture_output=True, timeout=30,
    )
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""


def list_mc_pids() -> set[int]:
    out, pids = _run_tasklist(), set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == "motioncatalyst-cli.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def find_new_pid(known: set[int]) -> int | None:
    for pid in list_mc_pids() - known:
        return pid
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    timeline: list[dict[str, object]] = []
    rc = 0

    def mark(lbl: str, **extra: object) -> None:
        e = time.perf_counter() - t0
        entry = {"t": round(e, 3), "label": lbl, **extra}
        timeline.append(entry)
        print(f"[t+{e:7.3f}s] {lbl} {extra if extra else ''}", flush=True)

    dotnet_trace = find_dotnet_trace()
    pre_pids = list_mc_pids()
    nettrace_path = RESULT_DIR / f"{SCENARIO_NAME}.nettrace"

    # Idempotent: remove prior .nettrace so a re-run produces exactly one file.
    if nettrace_path.exists():
        nettrace_path.unlink()

    # Use a Windows-visible temp root so MC-CLI can resolve hive path.
    win_tmp_root = Path("/c/tmp")
    win_tmp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"perf-scenario-startup-",
        dir=str(win_tmp_root),
        ignore_cleanup_errors=True,
    ) as tmp:
        hive = Path(tmp) / "hive"
        # Settings path mirrors AppLauncher.SetupHive() — ProgramData/settings/
        # under the hive root. Older spikes used <hive>/settings/ which MC
        # silently ignored, causing the OnboardingGuide dialog to appear.
        settings = hive / "ProgramData" / "settings"
        settings.mkdir(parents=True)
        for xml in PERF_HIVE_SRC.glob("*.xml"):
            shutil.copy2(xml, settings / xml.name)

        driver = McpDriver(
            ui_mcp_host_bin=UIMCP_BIN,
            mc_build=Path(MC_BUILD),
            hive_dir=hive,
            extra_env={
                "MC_PERF_MODE": "1",
                "MC_HEADLESS": "1",
            },
        )

        t0 = time.perf_counter()
        trace_capture: DotnetTraceCapture | None = None
        mc_pid: int | None = None

        try:
            mark("driver.start")
            driver.start()

            exe = Path(MC_BUILD) / "MotionCatalyst-cli.exe"
            launch_args = {
                "exePath": _win_path(str(exe)),
                "buildDir": _win_path(MC_BUILD),
                "hiveDir": _win_path(str(hive)),
                "waitForIdle": True,
                "idleTimeoutMs": MAX_IDLE_WAIT_S * 1000,
            }

            # Dispatch mc_connect in a background thread so we can start
            # dotnet-trace as soon as the new MC PID appears — before
            # mc_connect blocks waiting for idle. This is the spike-8 pattern
            # for cold-startup capture: trace from PID spawn through first-idle.
            connect_result: dict[str, object] = {}

            def _do_connect() -> None:
                try:
                    r = driver.call_tool(
                        "mc_connect", launch_args, timeout=MAX_IDLE_WAIT_S + 10
                    )
                    env = r.get("ok_envelope") or r
                    connect_result["ok"] = env.get("ok")
                    connect_result["envelope"] = env
                except Exception as exc:
                    connect_result["err"] = str(exc)

            mark("mc_connect.dispatch")
            connect_thread = threading.Thread(
                target=_do_connect, daemon=True, name="mc-connect",
            )
            connect_thread.start()

            # Tight-poll for the new MC PID so trace.start fires as early
            # as possible (captures early CLR JIT + WPF BAML loading).
            pid_deadline = time.perf_counter() + PID_POLL_DEADLINE_S
            while time.perf_counter() < pid_deadline:
                mc_pid = find_new_pid(pre_pids)
                if mc_pid:
                    break
                time.sleep(0.05)

            if not mc_pid:
                mark("FATAL.no_pid")
                return 2
            mark("mc.pid", pid=mc_pid)

            # Start dotnet-trace immediately on PID detection. The trace
            # captures cold JIT, WPF XAML/BAML parsing, first layout pass,
            # and first render. Duration is fixed; dotnet-trace self-exits
            # when the timer fires and flushes the .nettrace cleanly.
            trace_capture = DotnetTraceCapture(
                dotnet_trace_path=dotnet_trace,
                process_id=mc_pid,
                output_path=nettrace_path,
                duration_s=TRACE_DURATION_S,
                buffer_mb=512,
            )
            mark("trace.start", duration_s=TRACE_DURATION_S)
            trace_capture.start()
            trace_capture.wait_started(timeout=15.0)
            mark("trace.attached")

            # Wait for mc_connect's background thread — that signals MC has
            # reached idle (= startup complete enough for automation).
            mark("idle.wait")
            connect_thread.join(timeout=MAX_IDLE_WAIT_S)
            if connect_thread.is_alive():
                mark("idle.timeout", warning="mc_connect still running at deadline")
            else:
                err = connect_result.get("err")
                mark(
                    "idle.reached",
                    ok=connect_result.get("ok"),
                    **({"err": err} if err else {}),
                )

            # Settle: let post-startup lazy work (late GC, background SDK
            # init) surface in the trace before it stops.
            mark("settle.start", settle_s=SETTLE_S)
            time.sleep(SETTLE_S)
            mark("settle.done")

            # Wait for dotnet-trace to flush itself at --duration.
            mark("trace.wait_flush")
            trc_rc = trace_capture.stop(wait_timeout=120.0)
            trace_capture = None
            mark("trace.stopped", rc=trc_rc)

            # Cooperative shutdown.
            mark("disconnect")
            try:
                driver.disconnect(close_target=True)
            except Exception as exc:
                mark("disconnect.warning", err=str(exc))

            time.sleep(0.5)
            driver.kill()
            mark("driver.killed")

        except Exception as exc:
            mark("FATAL", err=str(exc))
            rc = 99
            try:
                if trace_capture is not None:
                    trace_capture.stop(wait_timeout=15.0)
            except Exception:
                pass
            try:
                driver.kill()
            except Exception:
                pass

        finally:
            # Always kill the MC-CLI PID we tracked ourselves.
            # NEVER kill by image name — that would destroy any MC instance
            # the user has running. We only kill the PID from find_new_pid.
            try:
                if mc_pid is not None and mc_pid in list_mc_pids():
                    print(f"  [cleanup] taskkill /F /PID {mc_pid}", flush=True)
                    subprocess.run(
                        ["taskkill.exe", "/F", "/PID", str(mc_pid)],
                        capture_output=True, timeout=15,
                    )
            except Exception as exc:
                print(f"  [cleanup] MC shutdown failed: {exc}", flush=True)

            (RESULT_DIR / "timeline.json").write_text(
                json.dumps(timeline, indent=2, default=str), encoding="utf-8",
            )

            nettrace_sz = nettrace_path.stat().st_size if nettrace_path.exists() else 0
            print(f"\n=== scenario-startup artifacts ({RESULT_DIR}) ===", flush=True)
            print(f"  {nettrace_path.name}: {nettrace_sz / 1024 / 1024:.2f} MB", flush=True)

            if rc == 0 and nettrace_sz == 0:
                print("  WARNING: .nettrace is empty — trace may have failed.",
                      file=sys.stderr, flush=True)
                rc = 3

    return rc


if __name__ == "__main__":
    sys.exit(main())
