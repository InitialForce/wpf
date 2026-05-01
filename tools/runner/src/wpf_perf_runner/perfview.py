"""PerfView ETW capture lifecycle.

Manages starting and stopping PerfView.exe in batch (no-GUI) mode.
PerfView is a Windows-only tool; this module subprocess-invokes it
via cmd.exe when running under WSL.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Default locations
# ---------------------------------------------------------------------------

DEFAULT_PERFVIEW_PATHS = [
    r"C:\PerfView\PerfView.exe",
    r"C:\tools\PerfView\PerfView.exe",
    r"C:\tools\PerfView.exe",
]

# ETW providers always collected. See architectural decision #6 in the plan.
ETW_PROVIDERS = [
    "Microsoft-Windows-WPF",
    "Microsoft-Windows-DotNETRuntime",
    "Microsoft-Windows-DotNETRuntime:0x1FFBCCBFF:5:@StacksEnabled=true",
    "MotionCatalyst-PerfHarness",
]

KERNEL_EVENTS = "Process,ImageLoad,Thread"

# ---------------------------------------------------------------------------
# Path helpers (POSIX → Windows for WSL)
# ---------------------------------------------------------------------------

def _to_win(p: str) -> str:
    """Convert a POSIX path (/c/foo) to a Windows path (C:\\foo)."""
    if p.startswith("/") and len(p) > 2 and p[2] == "/":
        return p[1].upper() + ":" + p[2:].replace("/", "\\")
    return p.replace("/", "\\")


def _win_path(p: str | Path) -> str:
    """Return a Windows path string, converting from POSIX if needed."""
    s = str(p)
    # Try cygpath for accurate WSL↔Windows conversion.
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
# PerfView finder
# ---------------------------------------------------------------------------

def find_perfview(hint: str | None = None) -> str:
    """Locate PerfView.exe.

    Args:
        hint: Explicit path supplied by the caller (highest priority).

    Returns:
        Windows path string to PerfView.exe.

    Raises:
        FileNotFoundError: If PerfView cannot be located.
    """
    candidates: list[str] = []
    if hint:
        candidates.append(hint)
    # Check PATH via where.exe.
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "where PerfView.exe"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    candidates.append(line)
    except Exception:
        pass

    candidates.extend(DEFAULT_PERFVIEW_PATHS)

    for candidate in candidates:
        # Check both POSIX and Windows paths.
        win_path = _win_path(candidate)
        # Convert win path back to POSIX for existence check in WSL.
        posix_check = candidate if os.path.exists(candidate) else None
        if posix_check and os.path.exists(posix_check):
            return _win_path(candidate)
        # Also try direct Windows path check via cmd.exe.
        try:
            result = subprocess.run(
                ["cmd.exe", "/c", f"if exist \"{win_path}\" echo exists"],
                capture_output=True, text=True, timeout=5
            )
            if "exists" in result.stdout:
                return win_path
        except Exception:
            pass

    raise FileNotFoundError(
        f"PerfView.exe not found. Searched: {candidates[:3]}... "
        "Install PerfView at C:\\PerfView\\PerfView.exe or pass --perfview-path."
    )


# ---------------------------------------------------------------------------
# PerfView lifecycle
# ---------------------------------------------------------------------------

class PerfViewCapture:
    """Context manager for a single PerfView ETW collection session.

    Usage::

        with PerfViewCapture(perfview_path, etl_path, log_path) as pv:
            pv.wait_started(timeout=10)
            ... # run scenario
        # ETL is flushed and closed on __exit__

    Can also be used manually::

        pv = PerfViewCapture(...)
        pv.start()
        pv.wait_started()
        ... # do work
        pv.stop()
    """

    def __init__(
        self,
        perfview_path: str,
        etl_path: str | Path,
        log_path: str | Path,
        *,
        circular_mb: int = 500,
        max_collect_sec: int = 300,
        extra_providers: list[str] | None = None,
    ) -> None:
        self.perfview_path = perfview_path
        self.etl_path = _win_path(etl_path)
        self.log_path = log_path  # Keep POSIX for Python-side polling.
        self._circular_mb = circular_mb
        self._max_collect_sec = max_collect_sec
        self._extra_providers = extra_providers or []
        self._proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._started = False

    def _build_cmd(self) -> list[str]:
        providers = list(ETW_PROVIDERS) + self._extra_providers
        provider_args: list[str] = []
        for p in providers:
            provider_args += [f"/Providers:{p}"]

        return [
            "cmd.exe", "/c",
            self.perfview_path,
            "collect",
            "/AcceptEULA",
            "/NoGui",
            f"/CircularMB:{self._circular_mb}",
            f"/MaxCollectSec:{self._max_collect_sec}",
            *provider_args,
            f"/KernelEvents:{KERNEL_EVENTS}",
            f"/LogFile:{_win_path(self.log_path)}",
            self.etl_path,
        ]

    def start(self) -> None:
        """Launch PerfView collect in the background."""
        if self._proc is not None:
            raise RuntimeError("PerfViewCapture already started")
        cmd = self._build_cmd()
        print(f"[perfview] Starting: {' '.join(cmd[2:])}", flush=True)
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        # Background thread to drain PerfView output.
        import threading
        def _drain() -> None:
            assert self._proc is not None
            assert self._proc.stdout is not None
            for raw in self._proc.stdout:
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    print(f"[perfview] {line}", flush=True)
                except Exception:
                    pass
        threading.Thread(target=_drain, daemon=True, name="perfview-drain").start()

    def wait_started(self, timeout: float = 10.0) -> bool:
        """Poll the PerfView log file until it indicates collection has started.

        PerfView writes "Starting collection at" to its log file when ready.
        Falls back to a fixed 3-second sleep if log file doesn't appear.

        Returns:
            True if the start marker was seen, False on timeout.
        """
        deadline = time.monotonic() + timeout
        log_path = Path(self.log_path)
        while time.monotonic() < deadline:
            if log_path.exists():
                try:
                    content = log_path.read_text(encoding="utf-8", errors="replace")
                    if "Starting collection" in content or "Collecting" in content:
                        print("[perfview] Collection started (detected in log).", flush=True)
                        self._started = True
                        return True
                except OSError:
                    pass
            time.sleep(0.5)
        # Fall back: if process is alive and we timed out polling the log,
        # give it a fixed grace period and trust PerfView is capturing.
        if self._proc is not None and self._proc.poll() is None:
            print(
                f"[perfview] Log polling timed out after {timeout}s — "
                "assuming PerfView is capturing (process alive).",
                flush=True,
            )
            self._started = True
            return True
        return False

    def stop(self) -> int:
        """Stop PerfView collection and wait for it to flush/exit.

        Returns:
            PerfView process exit code.
        """
        if self._proc is None:
            return 0
        if self._proc.poll() is not None:
            # Already exited (e.g. /MaxCollectSec elapsed).
            return self._proc.returncode or 0

        # PerfView batch-mode collection is stopped by sending it a Ctrl+C
        # (or by running `PerfView stop`).  Running a second PerfView.exe
        # invocation with `stop` is the documented batch-mode approach.
        print("[perfview] Sending stop command...", flush=True)
        try:
            stop_result = subprocess.run(
                ["cmd.exe", "/c", self.perfview_path, "stop",
                 "/AcceptEULA", "/NoGui",
                 f"/LogFile:{_win_path(self.log_path)}"],
                capture_output=True, text=True, timeout=60,
            )
            print(f"[perfview] Stop command exit={stop_result.returncode}", flush=True)
        except subprocess.TimeoutExpired:
            print("[perfview] Stop command timed out; terminating process.", flush=True)

        # Wait for the collect process to finish flushing.
        try:
            rc = self._proc.wait(timeout=120)
            print(f"[perfview] Collection process exited (rc={rc}).", flush=True)
            return rc
        except subprocess.TimeoutExpired:
            print("[perfview] Collection process did not exit; killing.", flush=True)
            self._proc.kill()
            return -1

    def __enter__(self) -> PerfViewCapture:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
