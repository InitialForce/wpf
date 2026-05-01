"""Stdout watcher for MC sentinel lines.

Reads lines from a subprocess stdout stream in a background thread,
looking for MC_READY and SCENARIO_COMPLETE sentinel strings.
Lines are also tee'd to the runner's stdout for visibility.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from typing import IO

SENTINEL_MC_READY = "MC_READY"
SENTINEL_SCENARIO_COMPLETE_PREFIX = "SCENARIO_COMPLETE"


@dataclass
class SentinelWatcher:
    """Background thread that reads MC stdout and detects sentinel lines.

    Usage::

        watcher = SentinelWatcher(proc.stdout)
        watcher.start()
        if watcher.wait_ready(timeout=60.0):
            ...  # MC is ready
        watcher.stop()
    """

    stream: IO[bytes]
    _ready_event: threading.Event = field(default_factory=threading.Event, init=False)
    _complete_event: threading.Event = field(default_factory=threading.Event, init=False)
    _complete_payload: str | None = field(default=None, init=False)
    _lines: list[str] = field(default_factory=list, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def start(self) -> None:
        """Start the background reader thread."""
        self._thread = threading.Thread(target=self._pump, daemon=True, name="sentinel-watcher")
        self._thread.start()

    def stop(self) -> None:
        """Signal the watcher to stop. Does not block."""
        self._stop.set()

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """Block until MC_READY is seen or timeout expires.

        Returns:
            True if MC_READY was seen, False on timeout.
        """
        return self._ready_event.wait(timeout=timeout)

    def wait_complete(self, timeout: float = 30.0) -> bool:
        """Block until SCENARIO_COMPLETE is seen or timeout expires.

        Returns:
            True if SCENARIO_COMPLETE was seen, False on timeout.
        """
        return self._complete_event.wait(timeout=timeout)

    @property
    def complete_payload(self) -> str | None:
        """The SCENARIO_COMPLETE line payload (everything after the sentinel prefix)."""
        return self._complete_payload

    @property
    def lines(self) -> list[str]:
        """All lines seen so far (copy)."""
        return list(self._lines)

    def _pump(self) -> None:
        try:
            for raw in self.stream:
                if self._stop.is_set():
                    break
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    continue
                self._lines.append(line)
                # Tee to runner stdout with prefix for visibility.
                print(f"[MC] {line}", flush=True)

                if SENTINEL_MC_READY in line:
                    self._ready_event.set()

                if line.startswith(SENTINEL_SCENARIO_COMPLETE_PREFIX):
                    self._complete_payload = line
                    self._complete_event.set()
        except Exception:
            pass


def make_watcher(proc: subprocess.Popen) -> SentinelWatcher:  # type: ignore[type-arg]
    """Create and start a SentinelWatcher attached to proc.stdout."""
    if proc.stdout is None:
        raise RuntimeError("Process stdout is not a pipe — pass stdout=subprocess.PIPE")
    watcher = SentinelWatcher(stream=proc.stdout)
    watcher.start()
    return watcher
