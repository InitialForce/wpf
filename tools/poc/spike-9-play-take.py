#!/usr/bin/env python3
"""Spike 9: profile WPF cost during take playback.

Differs from spike-8 (cold startup) by capturing only the *playback* window:
  1. Stage hive + import a .take fixture (gives us actual video to play).
  2. Launch MC, drive UI to Analysis screen with a take loaded.
  3. Wait for the player to be ready.
  4. Start dotnet-trace ONLY at this point — the captured trace is JUST the
     play work, no startup noise.
  5. wpf_click PlayButton, sleep ~10s, wpf_click PlayButton (pause).
  6. Stop trace, heap dump, probe, Asynkron call trees.

Acceptance signal: during 10s of play, `measureSlowEventCount` and
`arrangeSlowEventCount` from LayoutPerfTraceLogger should be ~0. Any
non-zero count means a control is being re-laid-out per frame — a real
per-frame layout bug. Render-frame max/p95 reveals compositor stalls.

Output dir: /c/work/wpf-perf-spike-9/
"""
from __future__ import annotations

import ast
import base64
import json
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
from wpf_perf_runner.gcdump import collect_gcdump, find_gcdump  # noqa: E402
from wpf_perf_runner.asynkron import analyze_to_files as asynkron_analyze  # noqa: E402

UIMCP_BIN = (
    "/c/work/desktop/wpf-test/Tools/ui-mcp-host/bin/Release/"
    "net10.0-windows10.0.19041.0/UiMcpHost.exe"
)
MC_BUILD = "/c/work/desktop/wpf-test/src/motioncatalyst/BUILD/x64_Debug"
PERF_HIVE_SRC = Path("/c/work/wpf-perf/scenarios/perf-hive")
# Real-video takes for playback profiling. The .take fixtures bundled in
# the test repo (Tests/SwingCatalyst.Test.Integration.Windows/Assets/Takes)
# have only 5-byte AVI placeholders — useful for testing import paths but
# NOT for playback because there is no video to render. /f/work/takes is
# Oystein's local stash of real .take files exported from real sessions.
TAKES_SRC_DIR = Path(
    __import__("os").environ.get("SPIKE9_TAKES_SRC_DIR", "/f/work/takes")
)
TAKE_FIXTURE = __import__("os").environ.get(
    "SPIKE9_TAKE_FIXTURE", "Breyden Johl - 2018-11-21 - 292.take"
)

# Each variant gets its own slug so file names + result dir line up.
# Override per-run via SPIKE9_NAME (file prefix) and SPIKE9_RESULT_DIR (out dir).
SPIKE_NAME = __import__("os").environ.get("SPIKE9_NAME", "spike-9")
RESULT_DIR = Path(
    __import__("os").environ.get(
        "SPIKE9_RESULT_DIR", f"/c/work/wpf-perf-{SPIKE_NAME}"
    )
)

# Per-frame slow-render emission threshold (microseconds). Defaults to 1000
# (= 1 ms) so the long tail of fast-but-not-zero render frames is visible.
# Set to 0 to emit every frame, or 5000+ to keep it sparse.
RENDER_FRAME_THRESHOLD_US = __import__("os").environ.get(
    "WPF_RENDER_TRACE_FRAME_US", "1000"
)

PROBE_EXE = "/c/work/wpf-perf/tools/probe/bin/Release/net10.0/nettrace-probe.exe"

# Trace orchestration. dotnet-trace's --duration is wall-clock from when
# `dotnet-trace collect` starts. The diagnostic-IPC handshake to MC takes
# ~1-2 s in practice (it allocates buffers, enables EventPipe, kicks
# rundown), and we have to allow a small flush margin at the end. Total:
#   PRE_PLAY_ATTACH_S (let dotnet-trace attach) + PLAY_DURATION_S
#   + POST_PLAY_IDLE_S + flush margin
# Be generous on PRE_PLAY_ATTACH_S — we'd rather waste a few seconds at
# the start of the trace than miss the first frames of play.
PRE_PLAY_ATTACH_S = 3.0
PLAY_DURATION_S = 10.0
POST_PLAY_IDLE_S = 1.5
TRACE_FLUSH_MARGIN_S = 4.0
TRACE_DURATION_S = int(round(
    PRE_PLAY_ATTACH_S + PLAY_DURATION_S + POST_PLAY_IDLE_S + TRACE_FLUSH_MARGIN_S
))  # = 19s
WARMUP_TIMEOUT_S = 90


def _run_tasklist() -> str:
    # Call tasklist.exe directly with each argv token as its own list
    # element. Going via `cmd.exe /c "tasklist /FI \"IMAGENAME eq ...\""`
    # drops the inner quotes — tasklist then sees `eq` as a positional
    # argument and exits rc=1 with "Invalid argument/option - 'eq'".
    # Direct invocation preserves the value of the /FI filter as a single
    # argv element. Filtered tasklist on this host runs in ~1.5s; the
    # unfiltered variant takes ~60s (1200+ processes).
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


def _envelope(r: dict) -> dict:
    return r.get("ok_envelope") or r


def _is_error_response(r: dict) -> tuple[bool, str]:
    """Detect tool-level errors — the broker's MCP returns isError=true on
    the JSON-RPC result with the exception text in content[0].text. The
    earlier driver.call_tool wrapper happily returns these as if they
    succeeded, so we have to check ourselves."""
    if isinstance(r, dict) and "error" in r and r["error"]:
        return True, str(r["error"])
    env = _envelope(r)
    if isinstance(env, dict):
        # If wpf_find_elements / wpf_click etc. returns ok=false, that's an error.
        if env.get("ok") is False:
            err = env.get("error") or {}
            return True, f"{err.get('code')}:{err.get('message')}"
        # Action tools (wpf_click, wpf_double_click, wpf_execute_command,
        # wpf_toggle, wpf_set_text_value, …) return a StateDeltaDto with
        # success:false + failureReason on a tool-level failure (e.g.
        # PATTERN_NOT_SUPPORTED on a ToggleButton). The 'ok' field is absent
        # in that envelope shape so the check above doesn't catch it.
        if env.get("success") is False:
            reason = env.get("failureReason")
            return True, f"action_failed:{reason}"
        # Some broker tools wrap raw exception text in '_raw' (when the JSON
        # parse fails). That always means failure.
    if isinstance(r, dict) and "_raw" in r:
        return True, str(r["_raw"])[:300]
    # MCP-level isError sometimes leaks via raw content shape.
    if isinstance(r, dict) and r.get("isError") is True:
        return True, str(r.get("content"))[:300]
    return False, ""


def call_strict(driver: McpDriver, name: str, args: dict, timeout: float = 10.0) -> dict:
    """call_tool that RAISES on tool-level errors (e.g. ArgumentException
    for missing nodeId)."""
    r = driver.call_tool(name, args, timeout=timeout)
    is_err, msg = _is_error_response(r)
    if is_err:
        raise RuntimeError(f"{name}({args}) failed: {msg}")
    return r


def find_node_id(driver: McpDriver, x_name: str, timeout_s: float = 30.0) -> str | None:
    """Resolve a XAML x:Name (== AutomationId in this codebase) to a
    brokered MCP nodeId by visual-tree enumeration + exact-name post-filter.

    The broker's wpf_find_elements signature is
    `(typeName, name, rootNodeId, conditions, treeType, maxResults)` — there
    is NO `automationId` parameter, and `name` is a CASE-INSENSITIVE
    SUBSTRING match on FrameworkElement.Name (see
    BrokeredNavigationTools.MakeExistsFn). So we must:
      1. Pass `name=<x_name>` and `treeType="visual"` (logical tree misses
         transitions / ContentPresenter swaps used by MainMenuView).
      2. Filter to IsVisible=True so collapsed siblings don't match.
      3. Over-fetch (maxResults=50) and post-filter
         results[*].node.name == x_name exactly.

    Earlier versions of this helper passed `automationId` (silently dropped
    by the broker) and trusted maxResults=1; that returned the first random
    visible element regardless of its name, so wait_for_id always returned
    True instantly — masking real navigation failures."""
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


def find_window_by_title(
    driver: McpDriver, title_substr: str, timeout_s: float = 30.0,
    debug_log_path: Path | None = None,
) -> str | None:
    """Poll wpf_get_windowsp until a top-level window with a title or type
    name containing `title_substr` is found, then return its nodeId.

    Brokered MCP's wpf_find_elements is anchored to the **main** window's
    visual tree by default. Child windows (SwingExplorerDialog,
    OnboardingGuideDialog, etc.) are unreachable unless we provide their
    rootNodeId explicitly. wpf_get_windowsp enumerates all top-level
    WPF windows in the process, regardless of which dispatcher owns them.

    debug_log_path: dump first response to disk for shape diagnosis when
    the lookup fails.
    """
    deadline = time.monotonic() + timeout_s
    last_response: dict | None = None
    poll = 0
    while time.monotonic() < deadline:
        poll += 1
        try:
            r = driver.call_tool("wpf_get_windows", {}, timeout=8.0)
            env = _envelope(r)
            windows: list = []
            # wpf_get_windows returns the windows array directly as the
            # top-level JSON value (no wrapping object). _envelope therefore
            # hands back a list. Older fallbacks (dict-with-windows,
            # _raw-as-Python-repr) are also kept for completeness.
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


def find_in_window(
    driver: McpDriver, root_node_id: str, x_name: str, timeout_s: float = 30.0,
) -> str | None:
    """Like find_node_id but scopes the search to the given root nodeId.
    Used to address controls inside child windows (SwingExplorerDialog) —
    the default rootless wpf_find_elements only sees the main window."""
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


def find_clickable_node(driver: McpDriver, x_name: str, timeout_s: float = 30.0) -> str | None:
    """Resolve a XAML x:Name to a brokered nodeId, but if that element is a
    non-invokable wrapper (HighlightFeature, StackPanel, UserControl with no
    InvokePattern), descend to the first Button child and return its nodeId.

    This mirrors FlaUI's pattern at FlaUIActionCatalog.cs:188 — many MC
    XAML buttons are wrapped in `Controls:HighlightFeature` for badge/
    onboarding overlays; the wrapper has the addressable x:Name, but
    only the inner Button has the Command binding that actually fires
    on click. wpf_click on the wrapper is a silent no-op.
    """
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
    # No Button child — fall back to the wrapper itself.
    return wrapper


def wait_for_list_items(
    driver: McpDriver, list_node_id: str, min_count: int = 1, timeout_s: float = 30.0,
) -> int:
    """Block until the given ListBox has at least `min_count` visible items.

    Mirrors FlaUI's `WaitListBoxAchieveItemsAndGetItemByIndex` from
    ExplorerView.cs — selecting a Student/Session populates the next
    ListBox asynchronously (DB query → bound ObservableCollection), so
    we MUST wait for items before calling wpf_select_item_by_index. If
    we don't, the index-0 select either silently no-ops or selects a
    placeholder, and the next ListBox never populates.

    Returns the observed item count, or 0 if the timeout elapses.
    """
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


def capture_screenshot(driver: McpDriver, label: str, out_dir: Path) -> dict:
    """Capture a screenshot of the MC main window and write it as a PNG.
    Used as a sanity check that warmup/play actually navigated MC where we
    think it did. Without this, the broker happily executes wrong-arg tool
    calls and we don't notice MC is stuck on the first screen."""
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


def wait_for_id(driver: McpDriver, automation_id: str, timeout_s: float) -> bool:
    """Returns True if an element with the given automationId becomes visible
    within the timeout. (Distinct from find_node_id which also returns the
    nodeId — wait_for_id is for cases where presence alone is what we want.)"""
    return find_node_id(driver, automation_id, timeout_s=timeout_s) is not None


def wait_for_screen(driver: McpDriver, screen_name: str, timeout_s: float) -> bool:
    """Block until mc_detect_current_screen reports `screen_name`. This is
    the canonical signal — it walks screens.yml's sentinels under
    IsVisible=True with the same logic the broker's mc_navigate_to uses
    internally, so it cannot disagree with itself.

    Polling for a sentinel automationId via wpf_find_elements (which is
    what wait_for_id used to do) is unreliable because:
      (a) wpf_find_elements has no automationId param — `name` is a
          substring match,
      (b) MainMenuView keeps every screen control in the visual tree
          (just collapsed), so an exact name match without IsVisible=True
          will produce false positives."""
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


def wait_for_mc_quiet(driver: McpDriver, quiet_seconds: float, max_wait_s: float) -> tuple[bool, float]:
    """Poll the broker's stderr buffer (which carries MC's stdout via the
    [MC][stdout] prefix) and return when no NEW MC log lines have arrived for
    `quiet_seconds`. This is our heuristic for "background SDK init has
    settled" — FFEncoder ctor, Spinnaker enumeration, Halcon framegrabber
    probe, and WMI queries all log on completion, so a quiet stderr means
    they're done.

    Returns (settled, elapsed_seconds).
    """
    start = time.monotonic()
    deadline = start + max_wait_s
    last_count = len(driver.stderr_tail)
    last_change_at = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.5)
        cur = len(driver.stderr_tail)
        now = time.monotonic()
        if cur != last_count:
            last_count = cur
            last_change_at = now
            continue
        if now - last_change_at >= quiet_seconds:
            return True, now - start
    return False, time.monotonic() - start


def import_takes(hive_dir: Path) -> int:
    """Run `MotionCatalyst-cli.exe import-takes` against a single fixture take.

    Returns process exit code. import-takes uses .take-embedded user info,
    so the imported take comes with its own student record — we just pick
    item index 0 from the user list afterwards.
    """
    take = TAKES_SRC_DIR / TAKE_FIXTURE
    if not take.exists():
        print(f"[import-takes] fixture not found: {take}", flush=True)
        return 1
    # CLI expects a directory of .take files; copy our single fixture
    # into a Windows-visible temp dir (NOT /tmp — that's WSL-only and
    # MC-CLI on the Windows side cannot resolve it).
    win_tmp = Path("/c/tmp")
    win_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="perf-take-fixture-", dir=str(win_tmp)) as td:
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


def main() -> int:
    RESULT_DIR.mkdir(exist_ok=True)
    timeline: list[dict[str, object]] = []
    rc = 0

    def mark(lbl: str, **extra: object) -> None:
        e = time.perf_counter() - t0
        entry = {"t": round(e, 3), "label": lbl, **extra}
        timeline.append(entry)
        print(f"[t+{e:7.3f}s] {lbl} {extra if extra else ''}", flush=True)

    dotnet_trace = find_dotnet_trace()
    gcdump_exe = find_gcdump()
    pre_pids = list_mc_pids()
    nettrace_path = RESULT_DIR / f"{SPIKE_NAME}.nettrace"
    gcdump_path = RESULT_DIR / f"{SPIKE_NAME}.gcdump"
    analysis_path = RESULT_DIR / "analysis.json"
    for p in (nettrace_path, gcdump_path, analysis_path):
        if p.exists():
            p.unlink()

    # Use a Windows-visible temp root so MC-CLI invocations (import-takes)
    # can resolve hive/take paths via cmd.exe. /tmp/* on WSL is invisible
    # to Windows processes.
    win_tmp_root = Path("/c/tmp")
    win_tmp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="perf-spike-9-",
        dir=str(win_tmp_root),
        ignore_cleanup_errors=True,
    ) as tmp:
        hive = Path(tmp) / "hive"
        # Settings path inside the hive is <hive>/ProgramData/settings/ —
        # the same path used by AppLauncher.SetupHive() in the QA test
        # harness. Earlier spikes wrote to <hive>/settings/ which MC
        # silently ignored, so ShowOnboardingGuide=false was never picked
        # up — the OnboardingGuide dialog popped up after Eric Hogge was
        # selected, which is why all spike-9* runs ended up obscuring
        # the Analysis screen / Explorer dialog under the tutorial overlay.
        settings = hive / "ProgramData" / "settings"
        settings.mkdir(parents=True)
        for xml in PERF_HIVE_SRC.glob("*.xml"):
            shutil.copy2(xml, settings / xml.name)

        t0 = time.perf_counter()

        # Pre-stage takes BEFORE launching the perf MC process. import-takes
        # spawns its own short-lived MC-CLI; do it here so the long-running
        # MC sees a hive with takes already in place.
        mark("import-takes.start")
        import_rc = import_takes(hive)
        mark("import-takes.done", rc=import_rc)
        if import_rc != 0:
            mark("FATAL.import_failed")
            return 3

        # Diagnostic: query the DB for the imported take's ID. We don't
        # pass it to MC — the user wants the canonical Explorer dialog
        # navigation to be exercised end-to-end. Logging the ID just
        # confirms import-takes succeeded before we fire up MC. Schema
        # rename TakeID→Id is from a recent EF migration.
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
            mark("db.query_take_id.error", err=str(exc))
        mark("db.take_id", takeId=take_id)
        if take_id is None:
            mark("FATAL.no_take_id_in_db")
            return 13

        driver = McpDriver(
            ui_mcp_host_bin=UIMCP_BIN,
            mc_build=Path(MC_BUILD),
            hive_dir=hive,
            # WPF_RENDER_TRACE_FRAME_US is read once by the WPF fork's
            # LayoutPerfTraceLogger static initializer in MC's process; we
            # set it here so it's already in MC's env before the EventSource
            # is touched.
            extra_env={
                "MC_PERF_MODE": "1",
                "MC_HEADLESS": "1",
                "WPF_RENDER_TRACE_FRAME_US": RENDER_FRAME_THRESHOLD_US,
            },
        )

        trace_capture: DotnetTraceCapture | None = None
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

            mc_pid: int | None = None
            for _ in range(10):
                mc_pid = find_new_pid(pre_pids)
                if mc_pid:
                    break
                time.sleep(0.5)
            if not mc_pid:
                mark("FATAL.no_pid")
                return 2
            mark("mc.pid", pid=mc_pid)

            # Diagnostic: screenshot immediately after mc_connect to confirm
            # MC has a UI. Log how the broker is detecting screens.
            shot_after_connect = capture_screenshot(
                driver, "00-after-mc-connect", RESULT_DIR / "shots",
            )
            mark("diag.shot_after_connect",
                 ok=shot_after_connect.get("ok"),
                 width=shot_after_connect.get("width"),
                 height=shot_after_connect.get("height"),
                 err=shot_after_connect.get("error"))

            # Warmup: drive UI to Analysis with the imported take loaded.
            #
            # MC needs to land on a SCREEN registered in screens.yml before
            # mc_navigate_to can route. Use the broker's own
            # mc_detect_current_screen which tells us exactly which screen
            # (if any) the sentinel-detector matches.
            mark("warmup.wait_for_landing_screen")
            landing = None
            landing_deadline = time.monotonic() + 180.0
            last_detect_err: str | None = None
            while time.monotonic() < landing_deadline:
                try:
                    r = driver.call_tool("mc_detect_current_screen", {}, timeout=10.0)
                    env = _envelope(r)
                    if isinstance(env, dict) and env.get("ok") is not False:
                        # Different broker versions wrap differently; try common keys.
                        screen = (env.get("screen") or env.get("screenName")
                                  or env.get("data", {}).get("screen")
                                  or env.get("currentScreen"))
                        if screen:
                            landing = str(screen)
                            break
                        last_detect_err = f"detect ok but no screen field: {list(env.keys())}"
                    else:
                        err = (env.get("error") or {}) if isinstance(env, dict) else {}
                        last_detect_err = f"{err.get('code')}:{err.get('message')}"
                except Exception as exc:
                    last_detect_err = str(exc)
                time.sleep(1.5)
            if landing is None:
                mark("FATAL.no_landing_screen", last_err=last_detect_err)
                # capture a final diagnostic shot before bailing
                shot_fail = capture_screenshot(
                    driver, "99-no-landing-screen", RESULT_DIR / "shots",
                )
                mark("diag.shot_failure",
                     ok=shot_fail.get("ok"),
                     path=shot_fail.get("path"),
                     err=shot_fail.get("error"))
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
                shot_fail = capture_screenshot(
                    driver, "99-no-user-selection", RESULT_DIR / "shots",
                )
                mark("diag.shot_failure", **shot_fail)
                return 9
            list_node = find_node_id(driver, "listViewStudentsAndGroups", 15.0)
            if not list_node:
                mark("FATAL.no_user_list")
                return 4

            # Pick the first student (whatever import-takes named them).
            # NB: brokered MCP requires nodeId, NOT automationId — passing
            # automationId throws ArgumentException server-side and the
            # generic call_tool wrapper used to swallow that silently.
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

            # NextViewButton lands on Analysis with no take loaded — Quick
            # Start panel is shown. The proper flow to open a take (matches
            # SwingExplorerUserControl.xaml + ExplorerTests.cs):
            #   1. Click OpenSwingExplorer (HighlightFeature wrapper around
            #      a Button bound to OpenSwingExplorerCommmand). FlaUI's
            #      `.Click("OpenSwingExplorer")` works directly, but the
            #      brokered MCP doesn't auto-descend through the wrapper —
            #      so we use find_clickable_node to address the inner
            #      Button child explicitly.
            #   2. Diagnostic screenshot — verify dialog actually opened.
            #   3. StudentListBox populates → select index 0 (binds to a
            #      LibrarySessionItemTemplate which contains a per-student
            #      SessionListBox).
            #   4. SessionListBox populates → select index 0 (the session
            #      template contains a TakeListBox).
            #   5. TakeListBox populates → select index 0. NOTE: ListBox is
            #      SelectionMode="Extended" with DoubleClickItemCommand; a
            #      single select-by-index only adds to the selection set,
            #      it does NOT load the take.
            #   6. Click OpenVideoButton (line 1190 of the dialog xaml,
            #      Command="{Binding OpenCommand}") — this is what fires
            #      OpenCommand to load the take and dismiss the dialog.
            #   7. Diagnostic screenshot — verify Quick Start panel hid and
            #      the video viewport rendered the first frame.
            mark("warmup.click_open_swing_explorer")
            open_explorer = find_clickable_node(driver, "OpenSwingExplorer", 30.0)
            if open_explorer is None:
                mark("FATAL.no_open_swing_explorer")
                shot_fail = capture_screenshot(
                    driver, "99-no-open-swing-explorer", RESULT_DIR / "shots",
                )
                mark("diag.shot_failure", **shot_fail)
                return 10
            call_strict(
                driver, "wpf_click",
                {"nodeId": open_explorer}, timeout=10.0,
            )

            # Wait for the SwingExplorerDialog top-level window to appear.
            # WindowManager.CreateChildWindow + Show() runs on
            # Dispatcher.InvokeAsync; takes ~7 s in practice from the click
            # (auto-selected student lazy-load + dialog template inflation).
            # MC's log line "Showing dialog of type [SwingExplorerDialog]"
            # confirms the timing — we poll wpf_get_windowsp until that
            # window appears, then use its nodeId as the rootNodeId for all
            # subsequent finds. This is critical: wpf_find_elements without
            # a rootNodeId is anchored to the main window's visual tree, so
            # finds for StudentListBox/TakeListBox would either match
            # nothing or match a wrong element from a different dialog
            # template (ImportVideoDialog has its own StudentListBox).
            mark("warmup.wait_for_explorer_window")
            dialog_root = find_window_by_title(
                driver, "SwingExplorerDialog", 30.0,
                debug_log_path=RESULT_DIR / "windowsp-debug.json",
            )
            if dialog_root is None:
                mark("FATAL.no_explorer_window")
                shot_fail = capture_screenshot(
                    driver, "99-no-explorer-window", RESULT_DIR / "shots",
                )
                mark("diag.shot_failure", **shot_fail)
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
            # Get the realized take ListBoxItem for double-click. The
            # OpenVideoButton path is gated on `_selectedTakes.Count > 0`
            # (SwingExplorerVM.OpenTakeAsync L1466) and `_selectedTakes` is
            # populated by SelectTakeCommand fired by ExplorerMultiSelectBehavior
            # on real mouse-click events — `wpf_select_item_by_index` only
            # sets ListBox.SelectedItem (= FocusedTake) and bypasses that
            # behavior, so the button click is a silent no-op even with
            # an item selected.
            #
            # Cleaner path: double-click the take item. The behavior's
            # DoubleClickItemCommand binding (xaml line 162) is wired to
            # SessionVM.HandleLibraryTakeDoubleClick → OpenFocusedTake,
            # which uses FocusedTake (= the SelectedItem we just set) and
            # bypasses _selectedTakes entirely. wpf_double_click raises
            # routed Control.MouseDoubleClickEvent which the behavior
            # catches.
            # Diagnostic: dump take_list properties + visible-tree to see
            # what's actually under it, and the ancestor chain so we can
            # verify the take ListBoxItem really sits under TakeListBox.
            mark("warmup.diag_take_list_props")
            try:
                props = driver.call_tool(
                    "wpf_get_properties", {"nodeId": take_list}, timeout=10.0,
                )
                dump = (RESULT_DIR / "diag-take_list-props.json")
                dump.write_text(
                    json.dumps(props, indent=2, default=str), encoding="utf-8")
            except Exception as exc:
                mark("warmup.diag_take_list_props.err", err=str(exc))
            mark("warmup.diag_take_list_tree")
            try:
                tree = driver.call_tool(
                    "wpf_get_visual_tree",
                    {"rootNodeId": take_list, "maxDepth": 4},
                    timeout=10.0,
                )
                (RESULT_DIR / "diag-take_list-tree.json").write_text(
                    json.dumps(tree, indent=2, default=str), encoding="utf-8")
            except Exception as exc:
                mark("warmup.diag_take_list_tree.err", err=str(exc))

            mark("warmup.get_take_items")
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
                shot_fail = capture_screenshot(
                    driver, "99-no-take-items", RESULT_DIR / "shots",
                )
                mark("diag.shot_failure", **shot_fail)
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

            # Diagnostic: check the first_take's parent chain and properties.
            # If it is NOT a descendant of take_list in the visual tree, then
            # routed events fired on first_take won't bubble to take_list,
            # which is where the multi-select behavior is attached. That
            # would explain why broker-raised events never reach the
            # behavior's handlers.
            try:
                anc = driver.call_tool(
                    "wpf_get_ancestors",
                    {"nodeId": first_take_id, "maxLevels": 20},
                    timeout=10.0,
                )
                (RESULT_DIR / "diag-first_take-ancestors.json").write_text(
                    json.dumps(anc, indent=2, default=str), encoding="utf-8")
            except Exception as exc:
                mark("warmup.diag_ancestors.err", err=str(exc))
            try:
                props = driver.call_tool(
                    "wpf_get_properties",
                    {"nodeId": first_take_id}, timeout=10.0,
                )
                (RESULT_DIR / "diag-first_take-props.json").write_text(
                    json.dumps(props, indent=2, default=str), encoding="utf-8")
            except Exception as exc:
                mark("warmup.diag_first_take_props.err", err=str(exc))

            # The multi-select behavior (ExplorerMultiSelectBehavior) binds
            # SelectItemCommand and DoubleClickItemCommand to *real* mouse
            # events. wpf_select_item_by_index only sets the WPF
            # SelectedItem property and bypasses the behavior, so it
            # neither populates _selectedTakes (used by OpenCommand) nor
            # routes through the SessionVM's double-click handler.
            #
            # To exercise the user-facing flow we need:
            #   1. wpf_select_item_by_index on TakeListBox → sets
            #      ListBox.SelectedItem, which raises SelectionChanged.
            #      ExplorerMultiSelectBehavior's OnSelectionChanged
            #      pushback fires SelectItemCommand on the new selection
            #      → populates _selectedTakes and FocusedTake.
            #      (We deliberately do NOT use wpf_click on the
            #      ListBoxItem here: ListBoxItem only exposes
            #      SelectionItemPattern, not IInvokeProvider, so wpf_click
            #      returns PATTERN_NOT_SUPPORTED — confirmed in
            #      diag-wpf_click-response.json from earlier runs.)
            #   2. wpf_double_click on the realized ListBoxItem → fires
            #      Control.MouseDoubleClickEvent which the per-container
            #      hook in ExplorerMultiSelectBehavior catches and routes
            #      to DoubleClickItemCommand → SessionVM.HandleLibraryTakeDoubleClick
            #      → OpenFocusedTake.
            mark("warmup.select_take_item", nodeId=take_list, index=0)
            sel_r = call_strict(
                driver, "wpf_select_item_by_index",
                {"nodeId": take_list, "index": 0}, timeout=10.0,
            )
            (RESULT_DIR / "diag-select_take_item-response.json").write_text(
                json.dumps(sel_r, indent=2, default=str), encoding="utf-8")
            mark("warmup.select_take_item.response_saved")
            time.sleep(0.5)
            mark("warmup.double_click_take", nodeId=first_take_id)
            dclick_r = call_strict(
                driver, "wpf_double_click",
                {"nodeId": first_take_id}, timeout=10.0,
            )
            (RESULT_DIR / "diag-wpf_double_click-response.json").write_text(
                json.dumps(dclick_r, indent=2, default=str), encoding="utf-8")
            mark("warmup.double_click_take.response_saved")

            # Wait for take to be loaded. Reliable signal: VideoSlider's
            # Maximum property becomes > 0 after the video clip has been
            # decoded enough to know its frame count. (VideoSlider exists
            # in the tree even with no take — Frame 0/0 state — so its
            # presence alone is not a load signal.) The earlier
            # QuickStartPanel-visibility heuristic was unreliable: the
            # panel only collapses when Mode != Neutral, which doesn't
            # always flip immediately on take open.
            mark("warmup.wait_for_take_loaded")
            take_loaded_deadline = time.monotonic() + 60.0
            take_loaded = False
            slider_max: float | None = None
            while time.monotonic() < take_loaded_deadline:
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
            shot_after_open = capture_screenshot(
                driver, "01b-take-loaded", RESULT_DIR / "shots",
            )
            mark("diag.shot_take_loaded",
                 ok=shot_after_open.get("ok"),
                 path=shot_after_open.get("path"))
            if not take_loaded:
                mark("FATAL.take_not_loaded")
                return 16

            # VideoSlider is the bottom playback slider. It exists in the
            # tree even when no take is loaded (Frame: 0/0 state), so its
            # *presence* is NOT a load signal — but with QuickStart hidden
            # we know the take is loaded, so resolve the nodeId now for
            # diagnostic logging and to pre-warm any side-effects of the
            # name lookup.
            mark("warmup.wait_for_video_slider")
            video_slider = find_node_id(driver, "VideoSlider", 60.0)
            if video_slider is None:
                mark("FATAL.no_video_slider", takeId=take_id)
                shot_fail = capture_screenshot(
                    driver, "99-no-video-slider", RESULT_DIR / "shots",
                )
                mark("diag.shot_failure", **shot_fail)
                return 14
            mark("warmup.video_slider_ready", nodeId=video_slider)

            mark("warmup.wait_for_play_button")
            play_node = find_node_id(driver, "PlayButton", 60.0)
            if not play_node:
                mark("FATAL.no_play_button")
                shot_fail = capture_screenshot(
                    driver, "99-no-play-button", RESULT_DIR / "shots",
                )
                mark("diag.shot_failure", **shot_fail)
                return 6
            mark("warmup.play_button_resolved", nodeId=play_node)

            # Wait for background SDK init (FFmpeg encoder, Spinnaker, Halcon
            # framegrabber, WMI queries) to settle. The first spike-9 run had
            # FFEncoderCodecContextVideo..ctor (3.2s) and WMI Next_ (2.1s)
            # still active during the trace window; that adds non-WPF noise to
            # the steady-state-play measurement.
            mark("warmup.wait_for_mc_quiet")
            settled, settle_elapsed = wait_for_mc_quiet(
                driver, quiet_seconds=3.0, max_wait_s=30.0,
            )
            mark("warmup.ready", settled=settled, elapsed=round(settle_elapsed, 2))

            shots_dir = RESULT_DIR / "shots"
            shot_warmup = capture_screenshot(driver, "01-warmup-ready", shots_dir)
            mark("warmup.shot", ok=shot_warmup.get("ok"),
                 path=shot_warmup.get("path"), err=shot_warmup.get("error"))

            # ====================================================================
            # CAPTURE WINDOW BEGINS HERE — covers attach + play + flush margin.
            #
            # IMPORTANT: dotnet-trace's --duration is wall-clock from collect
            # start, NOT from attach. And dotnet-trace prints no "Recording"
            # marker we can poll for, so we cannot reliably wait_started; the
            # earlier version did, fell through its 15s timeout, and ended up
            # clicking play AFTER the 12s trace had already finished collecting.
            #
            # Replacement strategy: start trace, sleep PRE_PLAY_ATTACH_S so the
            # diagnostic-IPC handshake completes, click play, sleep play
            # duration, click pause. The --duration is sized to cover the full
            # sequence with flush margin.
            # ====================================================================
            trace_capture = DotnetTraceCapture(
                dotnet_trace_path=dotnet_trace,
                process_id=mc_pid,
                output_path=nettrace_path,
                duration_s=TRACE_DURATION_S,
                buffer_mb=512,
            )
            mark("trace.start", duration_s=TRACE_DURATION_S)
            trace_capture.start()

            # Static attach delay. dotnet-trace's diagnostic-IPC handshake
            # plus EventPipe enable + initial rundown is ~1-2s in practice;
            # we wait a bit longer to give it room.
            time.sleep(PRE_PLAY_ATTACH_S)
            mark("trace.attach_settled", waited_s=PRE_PLAY_ATTACH_S)

            # Click play. PlayButton is a ToggleButton bound to
            # Playback.TogglePlayCommand. wpf_click goes through the UIA
            # InvokePattern (L1) which ToggleButton does NOT expose
            # (ToggleButtonAutomationPeer only advertises TogglePattern), so
            # the call returns success=false / PATTERN_NOT_SUPPORTED and
            # nothing happens. wpf_toggle would only flip IsChecked via
            # IToggleProvider.Toggle → ToggleButton.OnToggle, which DOES
            # NOT call OnClick and therefore does NOT execute the bound
            # Command. wpf_execute_command (L0) goes straight through the
            # WPF command system: ButtonBase.CommandProperty.Execute() with
            # CanExecute checked first — exactly what a real mouse click
            # would do, no SendInput. Broker docs explicitly recommend
            # this for any Command-bound ButtonBase including ToggleButton.
            mark("play.click")
            play_r = call_strict(
                driver, "wpf_execute_command",
                {"nodeId": play_node}, timeout=10.0,
            )
            (RESULT_DIR / "diag-play-execute_command-response.json").write_text(
                json.dumps(play_r, indent=2, default=str), encoding="utf-8")
            # In-trace mid-play screenshots — VERIFICATION mode. Each
            # 1080p PNG capture allocates ~5 MB on the LOH; we accept that
            # cost so we can compare the captured frames pixel-wise to
            # PROVE the video frames advanced (i.e., MC actually played).
            # For clean profiling, set SPIKE9_INTRACE_SHOTS=0.
            intrace_shots = __import__("os").environ.get(
                "SPIKE9_INTRACE_SHOTS", "1") != "0"
            if intrace_shots:
                time.sleep(3.0)
                shot_play_a = capture_screenshot(
                    driver, "02-mid-play-3s", RESULT_DIR / "shots")
                mark("play.mid.shot.3s", ok=shot_play_a.get("ok"),
                     path=shot_play_a.get("path"), err=shot_play_a.get("error"))
                time.sleep(4.0)
                shot_play_b = capture_screenshot(
                    driver, "02-mid-play-7s", RESULT_DIR / "shots")
                mark("play.mid.shot.7s", ok=shot_play_b.get("ok"),
                     path=shot_play_b.get("path"), err=shot_play_b.get("error"))
                time.sleep(PLAY_DURATION_S - 7.0)
            else:
                time.sleep(PLAY_DURATION_S)
            mark("play.elapsed", seconds=PLAY_DURATION_S)

            # Click again to pause; cleaner shutdown signal. Re-use the
            # cached play_node — calling find_node_id inside the trace
            # window costs a Snoop-tree walk (allocates 0.5–2 MB per call,
            # contributed to the 601 ms gen2 in spike-9d). If the cached
            # nodeId is stale because PlayButton got re-templated mid-play,
            # the click will fail and we'll log it but not refetch — the
            # 10 s play already happened, the trace is what we wanted.
            mark("pause.click", nodeId=play_node, cached=True)
            try:
                call_strict(
                    driver, "wpf_execute_command",
                    {"nodeId": play_node}, timeout=10.0,
                )
            except Exception as exc:
                mark("pause.error", err=str(exc))
            time.sleep(POST_PLAY_IDLE_S)

            # Wait for trace to flush itself (it's --duration based).
            # Screenshots happen AFTER trace.stopped so the Byte[] PNG
            # buffers don't show up as in-trace LOH allocation.
            mark("trace.wait_flush")
            trc_rc = trace_capture.stop(wait_timeout=120.0)
            trace_capture = None
            mark("trace.stopped", rc=trc_rc)

            shot_paused = capture_screenshot(driver, "03-paused", RESULT_DIR / "shots")
            mark("paused.shot", ok=shot_paused.get("ok"),
                 path=shot_paused.get("path"), err=shot_paused.get("error"))

            mark("gcdump.start")
            gcd_rc = collect_gcdump(
                mc_pid, gcdump_path, timeout_s=60, gcdump_path=gcdump_exe,
            )
            mark(
                "gcdump.done", rc=gcd_rc,
                bytes=gcdump_path.stat().st_size if gcdump_path.exists() else 0,
            )

            mark("probe.start")
            try:
                in_w = _win_path(str(nettrace_path))
                out_w = _win_path(str(analysis_path))
                exe_w = _win_path(PROBE_EXE)
                pr = subprocess.run(
                    ["cmd.exe", "/c", exe_w, in_w, "--top", "30", "--json", out_w],
                    capture_output=True, timeout=180,
                )
                mark(
                    "probe.done", rc=pr.returncode,
                    analysis_bytes=(
                        analysis_path.stat().st_size
                        if analysis_path.exists() else 0
                    ),
                )
            except Exception as exc:
                mark("probe.error", err=str(exc))

            mark("asynkron.start")
            try:
                outs = asynkron_analyze(
                    nettrace_path,
                    out_dir=RESULT_DIR,
                    modes=("memory", "cpu", "exception", "contention"),
                    timeout_s=600,
                )
                sizes = {m: p.stat().st_size for m, p in outs.items() if p.exists()}
                mark("asynkron.done", sizes=sizes)
            except Exception as exc:
                mark("asynkron.error", err=str(exc))

            mark("disconnect")
            try:
                driver.disconnect(close_target=True)
            except Exception:
                pass

            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and driver.is_alive():
                time.sleep(0.5)
            if driver.is_alive():
                driver.kill()
                mark("driver.killed")

        except Exception as exc:
            mark("FATAL", err=str(exc))
            try:
                if trace_capture is not None:
                    trace_capture.stop(wait_timeout=15.0)
                driver.kill()
            except Exception:
                pass
            rc = 99
        finally:
            (RESULT_DIR / "timeline.json").write_text(
                json.dumps(timeline, indent=2, default=str), encoding="utf-8")
            # Copy MC's hive log out before TemporaryDirectory cleanup destroys
            # it. Lets us grep for [diag-ms] tracer output that proves whether
            # bubbling routes / per-container hooks fire under automation.
            # MC may still hold the log file open (Serilog), so copy each file
            # individually (best-effort; skip locked files rather than abort).
            try:
                hive_logs_dir = hive / "ProgramData" / "logs"
                if hive_logs_dir.exists():
                    dst = RESULT_DIR / "mc-logs"
                    dst.mkdir(parents=True, exist_ok=True)
                    for log_file in hive_logs_dir.rglob("*"):
                        if not log_file.is_file():
                            continue
                        rel = log_file.relative_to(hive_logs_dir)
                        target = dst / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.copy2(log_file, target)
                        except (PermissionError, OSError) as exc:
                            print(f"  [warn] skip locked log {log_file.name}: {exc}")
            except Exception as exc:
                print(f"  [warn] failed to copy MC logs: {exc}")

            # Always shut down the MC-CLI we launched via mc_connect. Letting
            # the process linger means the next spike run hits ALREADY_RUNNING
            # / HIVE_IN_USE, AND the broker holds the hive log open so the
            # TemporaryDirectory cleanup that follows the with-block can't
            # remove it. We only ever kill PIDs we tracked ourselves
            # (mc_pid from find_new_pid against pre_pids) — never any MC
            # the user might have running independently.
            #
            # Cooperative shutdown is supposed to happen earlier in the try
            # block via driver.disconnect(close_target=True). If MC is still
            # alive at this point the broker has already lost cooperation,
            # so we go straight to taskkill rather than trying mc_disconnect
            # over a dead/dying driver pipe (that call hangs indefinitely
            # waiting for a response from a process that will never reply).
            try:
                if mc_pid is not None and mc_pid in list_mc_pids():
                    print(f"  [cleanup] taskkill /F /PID {mc_pid}")
                    subprocess.run(
                        ["taskkill.exe", "/F", "/PID", str(mc_pid)],
                        capture_output=True, timeout=15,
                    )
            except Exception as exc:
                print(f"  [cleanup] MC shutdown failed: {exc}")

            print(f"\n=== Spike 9 artifacts ({RESULT_DIR}) ===")
            for p in (nettrace_path, gcdump_path, analysis_path):
                sz = p.stat().st_size if p.exists() else 0
                print(f"  {p.name}: {sz / 1024 / 1024:.2f} MB")

    return rc


if __name__ == "__main__":
    sys.exit(main())
