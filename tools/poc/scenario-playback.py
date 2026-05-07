#!/usr/bin/env python3
"""scenario-playback — 10s steady-state playback dotnet-trace capture.

Captures the WPF render/dispatcher cost during steady-state video playback by:
  1. Pre-staging a real .take fixture into the hive (same import-takes helper
     and fixture env vars as spike-9).
  2. Launching MC-CLI, connecting, and navigating to the Analysis screen via
     the full Explorer dialog flow (student → session → take selection).
  3. Opening the take, waiting for VideoSlider.Maximum > 0 (take loaded).
  4. Holding a PRE_PLAY_ATTACH_S warmup window after the take is loaded so
     the dotnet-trace IPC handshake completes BEFORE playback starts.
  5. Starting dotnet-trace, clicking play, letting playback run SCENARIO_PLAY_S,
     clicking pause, then stopping the trace via terminate() (clean flush).
  6. Disconnecting MC cooperatively then taskkill in the finally block
     (only the PID we tracked via find_new_pid(pre_pids), never by image name).

Required env vars:
  SCENARIO_RESULT_DIR — directory where the .nettrace file lands
  SCENARIO_NAME       — filename stem for the output, e.g. "playback"

Optional env vars (default to spike-9 values verbatim):
  SCENARIO_TAKES_SRC_DIR  — directory containing .take fixture files
                            (default: /f/work/takes)
  SCENARIO_TAKE_FIXTURE   — .take filename to import
                            (default: Breyden Johl - 2018-11-21 - 292.take)
  SCENARIO_INTRACE_SHOTS  — if set to a truthy value, take mid-play screenshots
                            during the trace window (default: OFF for clean
                            profiling — screenshots allocate ~5 MB on the LOH).

Exit 0 on success, non-zero on any failure.
"""
from __future__ import annotations

import ast
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        "Usage: SCENARIO_RESULT_DIR=/tmp/out SCENARIO_NAME=playback "
        "python3 scenario-playback.py",
        file=sys.stderr,
    )
    sys.exit(1)

if not _scenario_name_raw:
    print(
        "ERROR: SCENARIO_NAME is not set.\n"
        "Usage: SCENARIO_RESULT_DIR=/tmp/out SCENARIO_NAME=playback "
        "python3 scenario-playback.py",
        file=sys.stderr,
    )
    sys.exit(1)

RESULT_DIR = Path(_result_dir_raw)
SCENARIO_NAME = _scenario_name_raw

# ---------------------------------------------------------------------------
# Config — take fixture (defaults mirror spike-9 verbatim)
# ---------------------------------------------------------------------------

TAKES_SRC_DIR = Path(
    os.environ.get("SCENARIO_TAKES_SRC_DIR", "/f/work/takes")
)
TAKE_FIXTURE = os.environ.get(
    "SCENARIO_TAKE_FIXTURE", "Breyden Johl - 2018-11-21 - 292.take"
)

# ---------------------------------------------------------------------------
# Config — timing (matches spike-9 constants)
# ---------------------------------------------------------------------------

# How long to wait for mc_connect (startup idle signal) before giving up.
WARMUP_TIMEOUT_S = 90

# How long to wait for take to load (VideoSlider.Maximum > 0) before failing.
TAKE_LOAD_TIMEOUT_S = 60

# Warmup hold after take is loaded, before starting the trace. Lets the
# dotnet-trace IPC handshake complete and the post-open transient settle
# so the trace window covers only steady-state playback.
PRE_PLAY_ATTACH_S = 3.0

# How long to let video play before pausing. Configurable for experimentation.
SCENARIO_PLAY_S = float(os.environ.get("SCENARIO_PLAY_S", "10.0"))

# Post-play idle before stopping the trace — lets trailing dispatcher work
# (deferred render passes, LOH GC flushes) surface in the trace.
POST_PLAY_IDLE_S = 1.5

# Trace flush margin: dotnet-trace needs a few seconds after pause to flush
# its internal ring buffer and write rundown events. Must be larger than the
# typical rundown duration (~2–4 s on this hardware).
TRACE_FLUSH_MARGIN_S = 4.0

# Trace upper bound — dotnet-trace self-exits at this wall-clock duration.
# = PRE_PLAY_ATTACH_S + SCENARIO_PLAY_S + POST_PLAY_IDLE_S + TRACE_FLUSH_MARGIN_S
# We terminate() early once playback is paused, so this is a safety ceiling only.
TRACE_DURATION_S = int(round(
    PRE_PLAY_ATTACH_S
    + SCENARIO_PLAY_S
    + POST_PLAY_IDLE_S
    + TRACE_FLUSH_MARGIN_S
))

# Timeout for the PID poll after mc_connect.
PID_POLL_DEADLINE_S = 30.0

# ---------------------------------------------------------------------------
# tasklist helpers (identical pattern to spike-9 and scenario-take-open)
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
# MCP response helpers (identical pattern to spike-9 and scenario-take-open)
# ---------------------------------------------------------------------------


def _envelope(r: dict) -> dict:
    return r.get("ok_envelope") or r


def _is_error_response(r: dict) -> tuple[bool, str]:
    if isinstance(r, dict) and "error" in r and r["error"]:
        return True, str(r["error"])
    env = _envelope(r)
    if isinstance(env, dict):
        if env.get("ok") is False:
            err = env.get("error") or {}
            return True, f"{err.get('code')}:{err.get('message')}"
        if env.get("success") is False:
            reason = env.get("failureReason")
            return True, f"action_failed:{reason}"
    if isinstance(r, dict) and "_raw" in r:
        return True, str(r["_raw"])[:300]
    if isinstance(r, dict) and r.get("isError") is True:
        return True, str(r.get("content"))[:300]
    return False, ""


def call_strict(
    driver: McpDriver,
    name: str,
    args: dict,
    timeout: float = 10.0,
    *,
    retry_on_busy_for_s: float = 15.0,
) -> dict:
    """call_tool that raises on tool-level errors; auto-retries DISPATCHER_BUSY."""
    deadline = time.monotonic() + max(retry_on_busy_for_s, 0.0)
    while True:
        r = driver.call_tool(name, args, timeout=timeout)
        is_err, msg = _is_error_response(r)
        if not is_err:
            return r
        if "DISPATCHER_BUSY" in msg and time.monotonic() < deadline:
            time.sleep(0.5)
            continue
        raise RuntimeError(f"{name}({args}) failed: {msg}")


def find_node_id(driver: McpDriver, x_name: str, timeout_s: float = 30.0) -> str | None:
    """Resolve an x:Name to a brokered nodeId via visual-tree enumeration."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = driver.call_tool(
                "wpf_find_elements",
                {
                    "name": x_name,
                    "treeType": "visual",
                    "maxResults": 50,
                    "conditions": [
                        {"property": "IsVisible", "operator": "Equals", "value": "True"},
                    ],
                },
                timeout=8.0,
            )
            env = _envelope(r)
            if not isinstance(env, dict):
                time.sleep(0.5)
                continue
            results = (
                env.get("results")
                or env.get("elements")
                or env.get("data", {}).get("results")
                or []
            )
            for entry in results:
                node = entry.get("node") or entry
                name = node.get("name") if isinstance(node, dict) else None
                if name == x_name:
                    node_id = node.get("nodeId") or node.get("id") or entry.get("nodeId")
                    if node_id:
                        return str(node_id)
        except Exception:
            pass
        time.sleep(0.5)
    return None


def find_in_window(
    driver: McpDriver, root_node_id: str, x_name: str, timeout_s: float = 30.0,
) -> str | None:
    """Like find_node_id but scopes the search to the given root nodeId."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = driver.call_tool(
                "wpf_find_elements",
                {
                    "rootNodeId": root_node_id,
                    "name": x_name,
                    "treeType": "visual",
                    "maxResults": 50,
                    "conditions": [
                        {"property": "IsVisible", "operator": "Equals", "value": "True"},
                    ],
                },
                timeout=8.0,
            )
            env = _envelope(r)
            results = (
                env.get("results") or env.get("elements") or []
            ) if isinstance(env, dict) else []
            for entry in results:
                node = entry.get("node") or entry
                if not isinstance(node, dict):
                    continue
                if node.get("name") == x_name:
                    node_id = node.get("nodeId") or node.get("id")
                    if node_id:
                        return str(node_id)
        except Exception:
            pass
        time.sleep(0.5)
    return None


def find_window_by_title(
    driver: McpDriver, title_substr: str, timeout_s: float = 30.0,
    debug_log_path: Path | None = None,
) -> str | None:
    """Poll wpf_get_windows until a top-level window matching title_substr is found."""
    deadline = time.monotonic() + timeout_s
    last_response: dict | None = None
    while time.monotonic() < deadline:
        try:
            r = driver.call_tool("wpf_get_windows", {}, timeout=8.0)
            env = _envelope(r)
            windows: list = []
            if isinstance(env, list):
                windows = env
                last_response = {"list_count": len(env), "sample": env[:2]}
            elif isinstance(env, dict):
                last_response = env
                windows = (
                    env.get("windows")
                    or env.get("results")
                    or env.get("data", {}).get("windows")
                    or []
                )
                if not windows and "_raw" in env:
                    try:
                        parsed = ast.literal_eval(env["_raw"])
                        if isinstance(parsed, list):
                            windows = parsed
                    except Exception:
                        pass
            for w in windows:
                if not isinstance(w, dict):
                    continue
                title = (w.get("title") or "")
                type_name = (w.get("typeName") or w.get("type") or "")
                if title_substr in title or title_substr in type_name:
                    node_id = w.get("nodeId") or w.get("id")
                    if node_id:
                        return str(node_id)
        except Exception as exc:
            last_response = {"_exc": str(exc)}
        time.sleep(0.5)
    if debug_log_path is not None and last_response is not None:
        try:
            debug_log_path.parent.mkdir(parents=True, exist_ok=True)
            debug_log_path.write_text(
                json.dumps(last_response, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass
    return None


def find_clickable_node(driver: McpDriver, x_name: str, timeout_s: float = 30.0) -> str | None:
    """Resolve an x:Name to a nodeId, descending to a Button child if the
    wrapper doesn't have InvokePattern (e.g. HighlightFeature wrappers)."""
    wrapper = find_node_id(driver, x_name, timeout_s=timeout_s)
    if wrapper is None:
        return None
    try:
        r = driver.call_tool(
            "wpf_find_elements",
            {
                "typeName": "Button",
                "rootNodeId": wrapper,
                "treeType": "visual",
                "maxResults": 1,
                "conditions": [
                    {"property": "IsVisible", "operator": "Equals", "value": "True"},
                ],
            },
            timeout=8.0,
        )
        env = _envelope(r)
        results = (env.get("results") or env.get("elements") or []) if isinstance(env, dict) else []
        for entry in results:
            node = entry.get("node") or entry
            child_id = node.get("nodeId") or node.get("id") if isinstance(node, dict) else None
            if child_id and child_id != wrapper:
                return str(child_id)
    except Exception:
        pass
    return wrapper


def wait_for_list_items(
    driver: McpDriver, list_node_id: str, min_count: int = 1, timeout_s: float = 30.0,
) -> int:
    """Block until the given ListBox has at least min_count visible items."""
    deadline = time.monotonic() + timeout_s
    last_count = 0
    while time.monotonic() < deadline:
        try:
            r = driver.call_tool(
                "wpf_find_elements",
                {
                    "typeName": "ListBoxItem",
                    "rootNodeId": list_node_id,
                    "treeType": "visual",
                    "maxResults": 200,
                },
                timeout=8.0,
            )
            env = _envelope(r)
            results = (
                env.get("results") or env.get("elements") or []
            ) if isinstance(env, dict) else []
            last_count = len(results)
            if last_count >= min_count:
                return last_count
        except Exception:
            pass
        time.sleep(0.5)
    return last_count


def wait_for_screen(driver: McpDriver, screen_name: str, timeout_s: float) -> bool:
    """Block until mc_detect_current_screen reports screen_name."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = driver.call_tool("mc_detect_current_screen", {}, timeout=10.0)
            env = _envelope(r)
            if isinstance(env, dict):
                screen = (env.get("screen") or env.get("screenName")
                          or env.get("data", {}).get("screen")
                          or env.get("currentScreen"))
                if screen == screen_name:
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def capture_screenshot(driver: McpDriver, label: str, out_dir: Path) -> dict:
    """Capture a screenshot of the MC main window and write it as a PNG."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        raw = driver._call_raw("wpf_capture_screenshot", {}, timeout=30)
        content = raw.get("content") or []
        if not content:
            return {"ok": False, "error": "no content"}
        env = json.loads(content[0]["text"])
        blob_ref = env.get("blobRef") or env.get("data", {}).get("blobRef")
        if not blob_ref:
            return {"ok": False, "error": "no blobRef", "env_keys": list(env.keys())}
        raw2 = driver._call_raw("wpf_fetch_blob", {"key": blob_ref}, timeout=30)
        png = None
        for b in raw2.get("content", []):
            if b.get("type") == "image" and b.get("data"):
                png = base64.b64decode(b["data"])
                break
        if png is None:
            return {"ok": False, "error": "no image block in fetch_blob"}
        out = out_dir / f"{label}.png"
        out.write_bytes(png)
        return {"ok": True, "path": str(out), "bytes": len(png),
                "width": env.get("width"), "height": env.get("height")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# take import (identical logic to spike-9, using SCENARIO_* env vars)
# ---------------------------------------------------------------------------


def import_takes(hive_dir: Path) -> int:
    """Run MotionCatalyst-cli.exe import-takes against the fixture take."""
    take = TAKES_SRC_DIR / TAKE_FIXTURE
    if not take.exists():
        print(
            f"[import-takes] fixture not found: {take}\n"
            f"  Set SCENARIO_TAKES_SRC_DIR and SCENARIO_TAKE_FIXTURE env vars.",
            flush=True,
        )
        return 1
    win_tmp = Path("/c/tmp")
    win_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="perf-playback-fixture-", dir=str(win_tmp)) as td:
        scratch = Path(td)
        shutil.copy2(take, scratch / take.name)
        cmd = [
            "cmd.exe", "/c",
            _win_path(str(Path(MC_BUILD) / "MotionCatalyst-cli.exe")),
            "import-takes",
            "--dir", _win_path(str(scratch)),
            "--hiveDir", _win_path(str(hive_dir)),
        ]
        print(f"[import-takes] {' '.join(cmd[2:])}", flush=True)
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        out = r.stdout.decode("utf-8", errors="replace")
        err = r.stderr.decode("utf-8", errors="replace")
        for line in (out + err).splitlines():
            print(f"[import-takes][out] {line}", flush=True)
        return r.returncode


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

    # SCENARIO_INTRACE_SHOTS: default OFF (clean profiling). Set to any truthy
    # value to take mid-play screenshots inside the trace window — useful for
    # verifying that video frames advanced, but each PNG allocates ~5 MB on
    # the LOH which shows up as AllocationTick events in the trace.
    intrace_shots = bool(os.environ.get("SCENARIO_INTRACE_SHOTS", ""))

    win_tmp_root = Path("/c/tmp")
    win_tmp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="perf-scenario-playback-",
        dir=str(win_tmp_root),
        ignore_cleanup_errors=True,
    ) as tmp:
        hive = Path(tmp) / "hive"
        settings = hive / "ProgramData" / "settings"
        settings.mkdir(parents=True)
        for xml in PERF_HIVE_SRC.glob("*.xml"):
            shutil.copy2(xml, settings / xml.name)

        t0 = time.perf_counter()

        # Pre-stage takes BEFORE launching the profiling MC process. import-takes
        # spawns its own short-lived MC-CLI; doing it here keeps the long-running
        # MC's hive clean and avoids racing with the DB.
        mark("import-takes.start")
        import_rc = import_takes(hive)
        mark("import-takes.done", rc=import_rc)
        if import_rc != 0:
            mark("FATAL.import_failed")
            return 3

        # Verify the take landed in the DB before proceeding.
        db_path = hive / "ProgramData" / "SwingCatalystDB.s3db"
        take_id: int | None = None
        try:
            r = subprocess.run(
                ["sqlite3", str(db_path), "SELECT Id FROM Take ORDER BY Id LIMIT 1;"],
                capture_output=True, timeout=10,
            )
            out = r.stdout.decode("utf-8", errors="replace").strip()
            if r.returncode == 0 and out:
                take_id = int(out.splitlines()[0])
        except Exception as exc:
            pass
        mark("db.take_id", takeId=take_id)
        if take_id is None:
            mark("FATAL.no_take_id_in_db")
            return 13

        driver = McpDriver(
            ui_mcp_host_bin=UIMCP_BIN,
            mc_build=Path(MC_BUILD),
            hive_dir=hive,
            extra_env={
                "MC_PERF_MODE": "1",
                "MC_HEADLESS": "1",
            },
        )

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
                "idleTimeoutMs": WARMUP_TIMEOUT_S * 1000,
            }
            mark("mc_connect.start")
            r = driver.call_tool("mc_connect", launch_args, timeout=WARMUP_TIMEOUT_S + 30)
            env = r.get("ok_envelope") or r
            mark("mc_connect.done", ok=env.get("ok"))

            for _ in range(10):
                mc_pid = find_new_pid(pre_pids)
                if mc_pid:
                    break
                time.sleep(0.5)
            if not mc_pid:
                mark("FATAL.no_pid")
                return 2
            mark("mc.pid", pid=mc_pid)

            # --- Warmup: navigate to Analysis screen and open the take ---

            mark("warmup.wait_for_landing_screen")
            landing = None
            landing_deadline = time.monotonic() + 180.0
            last_detect_err: str | None = None
            while time.monotonic() < landing_deadline:
                try:
                    r = driver.call_tool("mc_detect_current_screen", {}, timeout=10.0)
                    env = _envelope(r)
                    if isinstance(env, dict) and env.get("ok") is not False:
                        screen = (env.get("screen") or env.get("screenName")
                                  or env.get("data", {}).get("screen")
                                  or env.get("currentScreen"))
                        if screen:
                            landing = str(screen)
                            break
                        last_detect_err = f"detect ok but no screen field: {list(env.keys())}"
                    else:
                        err_d = (env.get("error") or {}) if isinstance(env, dict) else {}
                        last_detect_err = f"{err_d.get('code')}:{err_d.get('message')}"
                except Exception as exc:
                    last_detect_err = str(exc)
                time.sleep(1.5)
            if landing is None:
                mark("FATAL.no_landing_screen", last_err=last_detect_err)
                return 8
            mark("warmup.landing_screen_detected", screen=landing)

            if landing != "UserSelection":
                mark("warmup.navigate_to_user_selection", from_screen=landing)
                call_strict(
                    driver, "mc_navigate_to",
                    {"screenName": "UserSelection"}, timeout=30.0,
                )

            mark("warmup.user_selection_settling")
            if not wait_for_screen(driver, "UserSelection", 30.0):
                mark("FATAL.no_user_selection_screen")
                return 9
            list_node = find_node_id(driver, "listViewStudentsAndGroups", 15.0)
            if not list_node:
                mark("FATAL.no_user_list")
                return 4

            mark("warmup.select_first_student", nodeId=list_node)
            call_strict(
                driver, "wpf_select_item_by_index",
                {"nodeId": list_node, "index": 0}, timeout=10.0,
            )
            time.sleep(1.5)

            mark("warmup.click_next")
            next_node = find_node_id(driver, "NextViewButton", 10.0)
            if not next_node:
                mark("FATAL.no_next_button")
                return 7
            call_strict(
                driver, "wpf_click",
                {"nodeId": next_node}, timeout=10.0,
            )

            # Now on Analysis / Quick Start panel. Open the Explorer dialog.
            mark("warmup.click_open_swing_explorer")
            open_explorer = find_clickable_node(driver, "OpenSwingExplorer", 30.0)
            if open_explorer is None:
                mark("FATAL.no_open_swing_explorer")
                return 10
            call_strict(
                driver, "wpf_click",
                {"nodeId": open_explorer}, timeout=10.0,
            )

            mark("warmup.wait_for_explorer_window")
            dialog_root = find_window_by_title(
                driver, "SwingExplorerDialog", 30.0,
                debug_log_path=RESULT_DIR / "windowsp-debug.json",
            )
            if dialog_root is None:
                mark("FATAL.no_explorer_window")
                return 10
            mark("warmup.explorer_window_ready", nodeId=dialog_root)

            mark("warmup.wait_for_student_list")
            student_list = find_in_window(driver, dialog_root, "StudentListBox", 30.0)
            if student_list is None:
                mark("FATAL.no_student_list")
                return 11
            students = wait_for_list_items(driver, student_list, min_count=1, timeout_s=30.0)
            mark("warmup.student_list_ready", count=students)
            if students < 1:
                mark("FATAL.empty_student_list")
                return 11
            call_strict(
                driver, "wpf_select_item_by_index",
                {"nodeId": student_list, "index": 0}, timeout=10.0,
            )

            mark("warmup.wait_for_session_list")
            session_list = find_in_window(driver, dialog_root, "SessionListBox", 30.0)
            if session_list is None:
                mark("FATAL.no_session_list")
                return 12
            sessions = wait_for_list_items(driver, session_list, min_count=1, timeout_s=30.0)
            mark("warmup.session_list_ready", count=sessions)
            if sessions < 1:
                mark("FATAL.empty_session_list")
                return 12
            call_strict(
                driver, "wpf_select_item_by_index",
                {"nodeId": session_list, "index": 0}, timeout=10.0,
            )

            mark("warmup.wait_for_take_list")
            take_list = find_in_window(driver, dialog_root, "TakeListBox", 30.0)
            if take_list is None:
                mark("FATAL.no_take_list")
                return 13
            takes = wait_for_list_items(driver, take_list, min_count=1, timeout_s=30.0)
            mark("warmup.take_list_ready", count=takes)
            if takes < 1:
                mark("FATAL.empty_take_list")
                return 13

            # Get the realized first take ListBoxItem for double-click.
            # wpf_select_item_by_index sets ListBox.SelectedItem (= FocusedTake)
            # but bypasses ExplorerMultiSelectBehavior's SelectItemCommand. We
            # then double-click the realized item; DoubleClickItemCommand routes
            # through SessionVM.HandleLibraryTakeDoubleClick → OpenFocusedTake.
            take_items_r = call_strict(
                driver, "wpf_get_list_items",
                {"nodeId": take_list}, timeout=10.0,
            )
            take_items_env = _envelope(take_items_r)
            take_items: list = []
            if isinstance(take_items_env, list):
                take_items = take_items_env
            elif isinstance(take_items_env, dict):
                take_items = (
                    take_items_env.get("items")
                    or take_items_env.get("listItems")
                    or take_items_env.get("results")
                    or []
                )
                if not take_items and "_raw" in take_items_env:
                    try:
                        parsed = ast.literal_eval(take_items_env["_raw"])
                        if isinstance(parsed, list):
                            take_items = parsed
                    except Exception:
                        pass
            mark("warmup.take_items_count", count=len(take_items))
            if not take_items:
                mark("FATAL.no_take_items_realized")
                return 15
            first_take = take_items[0]
            first_take_id = (
                first_take.get("nodeId")
                or first_take.get("id")
                or (first_take.get("node", {}) or {}).get("nodeId")
            )
            if not first_take_id:
                mark("FATAL.no_first_take_id", item=str(first_take)[:200])
                return 15

            # Set selection (fires SelectItemCommand via SelectionChanged) so
            # FocusedTake is populated before the double-click.
            mark("warmup.select_take_item", nodeId=take_list, index=0)
            call_strict(
                driver, "wpf_select_item_by_index",
                {"nodeId": take_list, "index": 0}, timeout=10.0,
            )
            time.sleep(0.5)

            # Double-click to open the take.
            mark("warmup.double_click_take", nodeId=first_take_id)
            dclick_r = call_strict(
                driver, "wpf_double_click",
                {"nodeId": first_take_id}, timeout=10.0,
            )
            (RESULT_DIR / "diag-wpf_double_click-response.json").write_text(
                json.dumps(dclick_r, indent=2, default=str), encoding="utf-8")
            mark("warmup.double_click_take.done")

            # Wait for take to be loaded. VideoSlider.Maximum > 0 is set only
            # after the video clip has been decoded enough to know its frame
            # count — the canonical take-loaded signal (same as spike-9).
            mark("warmup.wait_for_take_loaded")
            take_load_deadline = time.monotonic() + TAKE_LOAD_TIMEOUT_S
            take_loaded = False
            slider_max: float | None = None
            while time.monotonic() < take_load_deadline:
                slider_id = find_node_id(driver, "VideoSlider", timeout_s=1.0)
                if slider_id is None:
                    time.sleep(0.5)
                    continue
                try:
                    r = driver.call_tool(
                        "wpf_get_properties",
                        {"nodeId": slider_id, "names": ["Maximum"]},
                        timeout=5.0,
                    )
                    env = _envelope(r)
                    items = (env.get("items") if isinstance(env, dict) else None) or []
                    for it in items:
                        if it.get("name") == "Maximum":
                            try:
                                slider_max = float(it.get("value") or 0)
                            except (TypeError, ValueError):
                                slider_max = None
                            break
                    if slider_max is not None and slider_max > 0:
                        take_loaded = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            mark("warmup.take_loaded", ok=take_loaded, slider_max=slider_max)

            if not take_loaded:
                mark("FATAL.take_not_loaded")
                return 16

            # Resolve play button BEFORE entering the trace window — resolving
            # it inside the trace allocates via the Snoop-tree walk and pollutes
            # the AllocationTick signal.
            mark("warmup.wait_for_play_button")
            play_node = find_node_id(driver, "PlayButton", 60.0)
            if not play_node:
                mark("FATAL.no_play_button")
                return 6
            mark("warmup.play_button_resolved", nodeId=play_node)

            # VideoSlider for value polling — also resolved outside the trace.
            video_slider = find_node_id(driver, "VideoSlider", 15.0)
            if video_slider is None:
                mark("FATAL.no_video_slider")
                return 14
            mark("warmup.video_slider_ready", nodeId=video_slider)

            # --- PRE_PLAY_ATTACH_S warmup window ---
            # Hold this long AFTER take is loaded and controls are resolved.
            # Lets the dotnet-trace IPC handshake (buffer allocation, EventPipe
            # enable, rundown) complete before playback starts, so the captured
            # window is truly steady-state playback with no open-transient noise.
            mark("pre_play_attach.start", hold_s=PRE_PLAY_ATTACH_S)
            time.sleep(PRE_PLAY_ATTACH_S)
            mark("pre_play_attach.done")

            # --- CAPTURE WINDOW BEGINS HERE ---
            # Trace covers: IPC attach settle already done + SCENARIO_PLAY_S
            # of playback + POST_PLAY_IDLE_S + flush margin.
            # We use terminate() (stdin newline) to stop early after pause so
            # dotnet-trace flushes cleanly without waiting out the full ceiling.
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

            # Property reader helpers (closed over driver/slider/play refs).
            def _read_prop(node_id: str, prop: str) -> str | None:
                try:
                    r = driver.call_tool(
                        "wpf_get_properties",
                        {"nodeId": node_id, "names": [prop]},
                        timeout=5.0,
                    )
                    env = _envelope(r)
                    items = (env.get("items") if isinstance(env, dict) else None) or []
                    for it in items:
                        if it.get("name") == prop:
                            return it.get("value")
                except Exception:
                    return None
                return None

            def _is_play_checked() -> bool:
                v = _read_prop(play_node, "IsChecked")
                return str(v).lower() == "true"

            # PlayButton is a ToggleButton bound to TogglePlayCommand.
            # wpf_execute_command fires ButtonBase.CommandProperty.Execute()
            # — the correct path for Command-bound ToggleButtons (wpf_click
            # returns PATTERN_NOT_SUPPORTED; wpf_toggle bypasses OnClick).
            mark("play.click")
            play_r = call_strict(
                driver, "wpf_execute_command",
                {"nodeId": play_node}, timeout=10.0,
                retry_on_busy_for_s=30.0,
            )
            (RESULT_DIR / "diag-play-execute_command-response.json").write_text(
                json.dumps(play_r, indent=2, default=str), encoding="utf-8")

            # Verify play actually started. Retry up to 3 times if IsChecked
            # is still False (dispatcher race or WPF render-thread stall).
            def _sample_is_checked(samples: int = 5, period_s: float = 0.4) -> bool:
                for _ in range(samples):
                    if _is_play_checked():
                        return True
                    time.sleep(period_s)
                return False

            play_started = False
            for attempt in range(3):
                time.sleep(1.0)
                checked = _sample_is_checked()
                mark("play.verify", attempt=attempt + 1, isChecked=checked)
                if checked:
                    play_started = True
                    break
                if attempt < 2:
                    mark("play.click.retry", attempt=attempt + 1)
                    try:
                        call_strict(
                            driver, "wpf_execute_command",
                            {"nodeId": play_node}, timeout=10.0,
                            retry_on_busy_for_s=15.0,
                        )
                    except Exception as exc:
                        mark("play.click.retry.error", err=str(exc))

            if not play_started:
                mark("FATAL.play_did_not_start")
                trc_rc = trace_capture.terminate(flush_timeout=30.0)
                trace_capture = None
                mark("trace.stopped_on_failure", rc=trc_rc)
                return 17

            # Run for SCENARIO_PLAY_S — either with or without mid-play screenshots.
            # Screenshots are LOH-allocating (~5 MB each) and perturb AllocationTick
            # attribution; keep them OFF by default for clean profiling.
            if intrace_shots:
                mid_a_s = min(3.0, SCENARIO_PLAY_S * 0.3)
                time.sleep(mid_a_s)
                shot_a = capture_screenshot(driver, "02-mid-play-a", RESULT_DIR / "shots")
                mark("play.mid.shot.a", ok=shot_a.get("ok"),
                     path=shot_a.get("path"), err=shot_a.get("error"))
                mid_b_s = min(7.0, SCENARIO_PLAY_S * 0.7)
                time.sleep(mid_b_s - mid_a_s)
                shot_b = capture_screenshot(driver, "02-mid-play-b", RESULT_DIR / "shots")
                mark("play.mid.shot.b", ok=shot_b.get("ok"),
                     path=shot_b.get("path"), err=shot_b.get("error"))
                time.sleep(SCENARIO_PLAY_S - mid_b_s)
            else:
                time.sleep(SCENARIO_PLAY_S)
            mark("play.elapsed", play_s=SCENARIO_PLAY_S)

            # Pause: re-use the cached play_node — calling find_node_id inside
            # the trace window costs a Snoop-tree walk (~0.5–2 MB LOH per call).
            mark("pause.click", nodeId=play_node, cached=True)
            try:
                call_strict(
                    driver, "wpf_execute_command",
                    {"nodeId": play_node}, timeout=10.0,
                    retry_on_busy_for_s=15.0,
                )
            except Exception as exc:
                mark("pause.error", err=str(exc))
            time.sleep(POST_PLAY_IDLE_S)

            # Stop the trace early (stdin newline → clean flush with rundown).
            # terminate() is preferred over stop() here: we set a large
            # TRACE_DURATION_S ceiling; terminate() ends the trace now without
            # waiting out the ceiling timer.
            mark("trace.terminate")
            trc_rc = trace_capture.terminate(flush_timeout=60.0)
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
                    trace_capture.terminate(flush_timeout=15.0)
            except Exception:
                pass
            try:
                driver.kill()
            except Exception:
                pass

        finally:
            # Always kill the MC-CLI PID we tracked ourselves.
            # NEVER kill by image name — that would destroy any MC instance the
            # user has running. We only kill the PID from find_new_pid(pre_pids).
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
            print(f"\n=== scenario-playback artifacts ({RESULT_DIR}) ===", flush=True)
            print(f"  {nettrace_path.name}: {nettrace_sz / 1024 / 1024:.2f} MB", flush=True)

            if rc == 0 and nettrace_sz == 0:
                print("  WARNING: .nettrace is empty — trace may have failed.",
                      file=sys.stderr, flush=True)
                rc = 3

    return rc


if __name__ == "__main__":
    sys.exit(main())
