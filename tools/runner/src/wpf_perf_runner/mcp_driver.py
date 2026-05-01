"""UiMcpHost MCP driver — extends driver.py patterns.

Wraps the cmd.jsonl / out.jsonl file-based MCP driver approach from
Tools/ui-mcp-host/driver.py, adapted for structured scenario execution.

Architecture:
  - Spawns UiMcpHost.exe as a subprocess.
  - Performs MCP initialize / notifications/initialized handshake.
  - Invokes mc_connect (Mode 1) to launch MotionCatalyst.
  - Exposes call_tool() for scenario step execution.
  - Provides disconnect() for clean shutdown.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path helpers (duplicated from driver.py to avoid import coupling)
# ---------------------------------------------------------------------------

def _to_win(p: str) -> str:
    """Convert a POSIX path (/c/foo) to a Windows path (C:\\foo)."""
    if p.startswith("/") and len(p) > 2 and p[2] == "/":
        return p[1].upper() + ":" + p[2:].replace("/", "\\")
    return p.replace("/", "\\")


def _win_path(p: str | Path) -> str:
    """Return a Windows path string, converting from POSIX if needed."""
    s = str(p)
    try:
        result = subprocess.run(
            ["cygpath", "-w", s],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return _to_win(s)


# ---------------------------------------------------------------------------
# Default UiMcpHost binary locations
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent


def _find_ui_mcp_host(hint: str | None = None) -> str:
    """Locate UiMcpHost.exe.

    Search order:
    1. Explicit hint (--ui-mcp-host-bin CLI flag).
    2. Default Release build location relative to the MC repo.
    3. StressTest build (used by driver.py for dev iterations).

    Returns absolute POSIX path (for subprocess under WSL).
    """
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint))

    # Walk up to find the MC repo root (contains Tools/ui-mcp-host/).
    for parent in _SCRIPT_DIR.parents:
        release = parent / "Tools" / "ui-mcp-host" / "bin" / "Release" / \
            "net10.0-windows10.0.19041.0" / "UiMcpHost.exe"
        stress = parent / "Tools" / "ui-mcp-host" / "bin" / "StressTest" / "UiMcpHost.exe"
        if release.exists():
            candidates.append(release)
        if stress.exists():
            candidates.append(stress)

    # Common absolute path for this project layout.
    candidates += [
        Path("/c/work/desktop/wpf-test/Tools/ui-mcp-host/bin/Release/"
             "net10.0-windows10.0.19041.0/UiMcpHost.exe"),
        Path("/c/work/desktop/wpf-test/Tools/ui-mcp-host/bin/StressTest/UiMcpHost.exe"),
    ]

    for c in candidates:
        if c.exists():
            return str(c)

    checked = [str(c) for c in candidates[:4]]
    raise FileNotFoundError(
        f"UiMcpHost.exe not found. Checked: {checked}. "
        "Build with: dotnet build Tools/ui-mcp-host/UiMcpHost.csproj "
        "-p:SnoopEnabled=true -c Release"
    )


# ---------------------------------------------------------------------------
# MCP wire protocol helpers (mirrors driver.py)
# ---------------------------------------------------------------------------

class McpDriver:
    """Manages a UiMcpHost.exe subprocess and the JSON-RPC MCP session.

    This class wraps the driver.py patterns into a structured interface
    for scenario step execution.
    """

    def __init__(
        self,
        ui_mcp_host_bin: str,
        mc_build: str | Path,
        *,
        hive_dir: str | Path | None = None,
        extra_env: dict[str, str] | None = None,
        screens_yml: str | Path | None = None,
    ) -> None:
        self.ui_mcp_host_bin = ui_mcp_host_bin
        self.mc_build = str(mc_build)
        self.hive_dir = str(hive_dir) if hive_dir else None
        self.extra_env = extra_env or {}
        self.screens_yml = str(screens_yml) if screens_yml else None

        self._proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._nid = [1]
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=500)
        self._connected = False

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Spawn UiMcpHost.exe and complete MCP initialize handshake."""
        env = os.environ.copy()
        env.update(self.extra_env)

        # Set SCREENS_YML for navigation tools.
        if self.screens_yml:
            env["SCREENS_YML"] = self.screens_yml
        else:
            # Auto-discover screens.yml relative to MC build.
            mc_root = Path(self.mc_build).parent
            for _ in range(6):
                candidate = mc_root / "Tests" / "MotionCatalyst.Test.UI" / "screens.yml"
                if candidate.exists():
                    env["SCREENS_YML"] = str(candidate)
                    break
                mc_root = mc_root.parent

        self._proc = subprocess.Popen(
            [self.ui_mcp_host_bin],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        threading.Thread(
            target=self._stderr_pump,
            daemon=True,
            name="mcp-host-stderr",
        ).start()

        # MCP initialize handshake.
        self._send({
            "jsonrpc": "2.0", "id": self._nid[0], "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "wpf-perf-runner", "version": "0.1.0"},
            },
        })
        r = self._read_response(self._nid[0], timeout=15.0)
        self._nid[0] += 1
        if r is None:
            self.kill()
            raise RuntimeError("MCP initialize handshake failed — no response from UiMcpHost.")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def connect_mc(
        self,
        *,
        wait_for_idle: bool = True,
        idle_timeout_ms: int = 30_000,
        wait_for_element: str = "HomeViewControl",
        element_timeout_ms: int = 60_000,
    ) -> dict[str, Any]:
        """Launch MotionCatalyst via mc_connect (Mode 1).

        Args:
            wait_for_idle: Pass waitForIdle to mc_connect.
            idle_timeout_ms: Idle wait budget.
            wait_for_element: AutomationId sentinel to wait for.
            element_timeout_ms: Element wait budget.

        Returns:
            The ok_envelope dict from mc_connect.

        Raises:
            RuntimeError: If mc_connect returns ok=false.
        """
        launch_args: dict[str, Any] = {
            "buildDir": _win_path(self.mc_build),
            "waitForIdle": wait_for_idle,
            "idleTimeoutMs": idle_timeout_ms,
            "waitForElement": wait_for_element,
            "elementTimeoutMs": element_timeout_ms,
        }
        if self.hive_dir:
            launch_args["hiveDir"] = _win_path(self.hive_dir)

        result = self.call_tool("mc_connect", launch_args, timeout=180.0)
        envelope = result.get("ok_envelope") or {}

        # Fallback for older hosts that lack mc_connect.
        if not envelope.get("ok", True) and \
                (envelope.get("error") or {}).get("code") == "UNKNOWN_TOOL":
            result = self.call_tool("mc_launch", launch_args, timeout=180.0)
            envelope = result.get("ok_envelope") or {}

        if not envelope.get("ok", False):
            err = envelope.get("error", {})
            raise RuntimeError(
                f"mc_connect failed: code={err.get('code')} message={err.get('message')}"
            )

        self._connected = True
        return envelope

    def call_tool(
        self, name: str, args: dict[str, Any], *, timeout: float = 60.0
    ) -> dict[str, Any]:
        """Invoke an MCP tool and return the parsed result.

        Returns a dict with key 'ok_envelope' if the response text was valid
        JSON (the standard tool envelope), or '_raw' for plain text.
        """
        raw = self._call_raw(name, args, timeout=timeout)
        content = raw.get("content") or []
        if content and content[0].get("type") == "text":
            try:
                return {"ok_envelope": json.loads(content[0]["text"])}
            except Exception:
                return {"_raw": content[0]["text"]}
        return raw

    def disconnect(self, *, close_target: bool = True) -> None:
        """Disconnect the MCP session, optionally closing MC."""
        if self._connected:
            try:
                self.call_tool("mc_disconnect", {"closeTarget": close_target}, timeout=30.0)
            except Exception as exc:
                print(f"[mcp] mc_disconnect warning: {exc}", flush=True)
            self._connected = False

    def kill(self) -> None:
        """Forcibly terminate UiMcpHost (used on error paths)."""
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    def is_alive(self) -> bool:
        """Return True if the UiMcpHost process is still running."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def return_code(self) -> int | None:
        """UiMcpHost exit code, or None if still running."""
        if self._proc is None:
            return None
        return self._proc.poll()

    @property
    def stderr_tail(self) -> list[str]:
        """Last ≤500 stderr lines from UiMcpHost."""
        return list(self._stderr_lines)

    # -- Internal JSON-RPC ---------------------------------------------------

    def _send(self, msg: dict[str, Any]) -> None:
        assert self._proc is not None
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _read_response(self, expect_id: int, timeout: float = 60.0) -> dict[str, Any] | None:
        assert self._proc is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self._proc.stdout is not None
            line = self._proc.stdout.readline()
            if not line:
                if self._proc.poll() is not None:
                    return None
                time.sleep(0.02)
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                m = json.loads(text)
            except json.JSONDecodeError:
                continue
            if m.get("id") == expect_id:
                return m  # type: ignore[return-value]
        return None

    def _call_raw(
        self, name: str, args: dict[str, Any], *, timeout: float = 60.0
    ) -> dict[str, Any]:
        msg_id = self._nid[0]
        self._nid[0] += 1
        self._send({
            "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        r = self._read_response(msg_id, timeout=timeout)
        if r is None:
            return {"error": "no response", "content": []}
        if "error" in r:
            return {"error": r["error"], "content": []}
        return r.get("result", {})  # type: ignore[return-value]

    def _stderr_pump(self) -> None:
        assert self._proc is not None
        assert self._proc.stderr is not None
        for raw in self._proc.stderr:
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
                self._stderr_lines.append(line)
                print(f"[host-stderr] {line}", file=sys.stderr, flush=True)
            except Exception:
                break
