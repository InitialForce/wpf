"""CLI: ``wpf-perf-deep`` — run Asynkron.Profiler (forked) on a captured .nettrace.

Examples::

    wpf-perf-deep /c/work/wpf-perf-spike-7/spike-7.nettrace
    wpf-perf-deep trace.nettrace --modes memory,cpu --out-dir analysis
    wpf-perf-deep trace.nettrace --mode exception --exception-type IOException

Writes ``asynkron-<mode>.txt`` files into ``--out-dir`` (default: same dir as input).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .asynkron import (
    DEFAULT_PROFILE_TOOL,
    SUPPORTED_MODES,
    analyze_to_files,
    find_profile_tool,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wpf-perf-deep",
        description=(
            "Deep call-tree analysis of a captured .nettrace via the forked "
            "Asynkron.Profiler ProfileTool.exe. Produces one .txt per mode."
        ),
    )
    p.add_argument("nettrace", help="Path to a .nettrace file.")
    p.add_argument(
        "--modes",
        default="memory,cpu",
        help=(
            f"Comma-separated modes to run. "
            f"Valid: {','.join(SUPPORTED_MODES)}. "
            f"Default: 'memory,cpu'. Use 'all' for every mode."
        ),
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory (default: same dir as the nettrace).",
    )
    p.add_argument(
        "--profile-tool",
        default=None,
        help=f"Path to ProfileTool.exe (default auto-discover; "
             f"falls back to {DEFAULT_PROFILE_TOOL}).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Per-mode timeout in seconds (default: 600).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    nettrace = Path(args.nettrace)
    if not nettrace.exists():
        print(f"error: {nettrace} not found", file=sys.stderr)
        return 2

    if args.modes.strip().lower() == "all":
        modes: tuple[str, ...] = SUPPORTED_MODES
    else:
        modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
        for m in modes:
            if m not in SUPPORTED_MODES:
                print(
                    f"error: unknown mode {m!r}. Valid: {','.join(SUPPORTED_MODES)}",
                    file=sys.stderr,
                )
                return 2

    out_dir = Path(args.out_dir) if args.out_dir else nettrace.parent

    try:
        tool = find_profile_tool(args.profile_tool)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"[wpf-perf-deep] ProfileTool: {tool}", flush=True)
    print(f"[wpf-perf-deep] Input:       {nettrace}", flush=True)
    print(f"[wpf-perf-deep] Out dir:     {out_dir}", flush=True)
    print(f"[wpf-perf-deep] Modes:       {','.join(modes)}", flush=True)

    out = analyze_to_files(
        nettrace,
        out_dir=out_dir,
        modes=modes,
        profile_tool_path=args.profile_tool,
        timeout_s=args.timeout,
    )

    print(flush=True)
    for mode, path in out.items():
        sz = path.stat().st_size if path.exists() else 0
        print(f"  {mode:11s} -> {path}  ({sz / 1024:.1f} KB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
