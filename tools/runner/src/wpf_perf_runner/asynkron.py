"""Asynkron.Profiler (forked) wrapper for deep call-tree analysis.

The forked binary lives at::

    /c/work/asynkron-profiler-fork/src/ProfileTool/bin/Release/net10.0/ProfileTool.exe

Upstream Asynkron.Profiler has a bug where ``--input <file.nettrace>`` is
gated by ``ValidateHotJitRequest`` even when ``--hot`` is not specified
(``HotThresholdSpecified`` is wrongly set when the option's default factory
fires). The fork at ``oysteinkrog/Asynkron.Profiler`` (branch
``fix/input-mode-hot-default``) checks ``ParseResult.IsImplicit`` so default
values no longer trip the gate.

This module wraps ``ProfileTool.exe`` to produce structured call-tree text
that complements the fast totals in ``nettrace-probe``:

- ``--memory``     allocation call tree (alloc bytes per chain)
- ``--cpu``        CPU call tree
- ``--exception``  throw-site call tree (filterable with ``--exception-type``)
- ``--contention`` lock-wait call tree

Call-tree text is captured to a ``.txt`` file alongside the source ``.nettrace``;
nothing is parsed — it's meant for direct human reading and grep.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Default fork build location.
DEFAULT_PROFILE_TOOL = (
    "/c/work/asynkron-profiler-fork/src/ProfileTool/bin/Release/net10.0/ProfileTool.exe"
)

# All modes the fork supports against an existing .nettrace.
# (--heap is intentionally excluded — needs a live process, not --input.)
SUPPORTED_MODES: tuple[str, ...] = ("cpu", "memory", "exception", "contention")


def _to_win(p: str) -> str:
    if p.startswith("/") and len(p) > 2 and p[2] == "/":
        return p[1].upper() + ":" + p[2:].replace("/", "\\")
    return p.replace("/", "\\")


def _win_path(p: str | Path) -> str:
    s = str(p)
    try:
        result = subprocess.run(
            ["cygpath", "-w", s], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return _to_win(s)


def find_profile_tool(hint: str | None = None) -> str:
    """Locate the forked ProfileTool.exe.

    Returns Windows path. Raises FileNotFoundError if not found.
    """
    candidates: list[str] = []
    if hint:
        candidates.append(hint)
    env = os.environ.get("ASYNKRON_PROFILE_TOOL")
    if env:
        candidates.append(env)
    candidates.append(DEFAULT_PROFILE_TOOL)

    for c in candidates:
        if c.startswith("/") and os.path.exists(c):
            return _win_path(c)
        # Try POSIX form of a Windows path.
        if len(c) > 2 and c[1] == ":":
            posix = "/" + c[0].lower() + c[2:].replace("\\", "/")
            if os.path.exists(posix):
                return _win_path(c)

    raise FileNotFoundError(
        "ProfileTool.exe (Asynkron fork) not found. Build it:\n"
        "  cd /c/work/asynkron-profiler-fork && cmd.exe /c "
        "\"dotnet build src/ProfileTool/ProfileTool.csproj -c Release\""
    )


def run_mode(
    nettrace_path: str | Path,
    *,
    mode: str,
    profile_tool_path: str | None = None,
    extra_args: list[str] | None = None,
    timeout_s: int = 600,
    callsite_root: str | None = None,
    calltree_depth: int | None = None,
    calltree_width: int | None = None,
    exception_type: str | None = None,
) -> tuple[int, str, str]:
    """Run one analysis mode.

    Returns ``(returncode, stdout, stderr)``. Stdout contains the formatted
    call tree (Spectre.Console output, includes ANSI escapes — the caller
    typically writes it to a ``.txt`` file).
    """
    if mode not in SUPPORTED_MODES:
        raise ValueError(
            f"mode must be one of {SUPPORTED_MODES!r}, got {mode!r}",
        )

    tool = _win_path(profile_tool_path) if profile_tool_path else find_profile_tool()
    in_w = _win_path(nettrace_path)

    cmd: list[str] = ["cmd.exe", "/c", tool, f"--{mode}", "--input", in_w]
    if callsite_root:
        cmd += ["--root", callsite_root]
    if calltree_depth is not None:
        cmd += ["--calltree-depth", str(calltree_depth)]
    if calltree_width is not None:
        cmd += ["--calltree-width", str(calltree_width)]
    if exception_type:
        cmd += ["--exception-type", exception_type]
    if extra_args:
        cmd += extra_args

    env = dict(os.environ)
    # Asynkron uses Spectre.Console; suppress ANSI so output is clean text.
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")

    proc = subprocess.run(
        cmd, capture_output=True, timeout=timeout_s, env=env,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, out, err


def analyze_to_files(
    nettrace_path: str | Path,
    *,
    out_dir: str | Path,
    modes: tuple[str, ...] = SUPPORTED_MODES,
    profile_tool_path: str | None = None,
    timeout_s: int = 600,
) -> dict[str, Path]:
    """Run each requested mode against the .nettrace and write `<mode>.txt`.

    Returns a mapping of mode → output file path. Modes that fail are
    reported by writing an error stub and the path is still included so the
    caller can detect partial completion.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}

    def _run_one(mode: str) -> tuple[str, Path]:
        out_file = out / f"asynkron-{mode}.txt"
        try:
            rc, stdout, stderr = run_mode(
                nettrace_path, mode=mode,
                profile_tool_path=profile_tool_path, timeout_s=timeout_s,
            )
            body = stdout if rc == 0 else (
                f"ProfileTool exited rc={rc}\n--- stderr ---\n{stderr}\n"
                f"--- stdout ---\n{stdout}\n"
            )
            out_file.write_text(body, encoding="utf-8")
        except subprocess.TimeoutExpired:
            out_file.write_text(
                f"ProfileTool {mode} timed out after {timeout_s}s\n",
                encoding="utf-8",
            )
        except FileNotFoundError as exc:
            out_file.write_text(f"{exc}\n", encoding="utf-8")
        return mode, out_file

    with ThreadPoolExecutor(max_workers=len(modes)) as pool:
        futures = {pool.submit(_run_one, mode): mode for mode in modes}
        for fut in as_completed(futures):
            mode_name, out_file = fut.result()
            results[mode_name] = out_file

    return results
