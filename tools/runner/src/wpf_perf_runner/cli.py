"""CLI entry point for wpf-perf-runner.

Usage:
    wpf-perf-runner --scenario <path> --wpf-sha <label> --runs <N> \\
        [--out-dir <path>] [--mc-build <path>] \\
        [--ui-mcp-host-bin <path>] [--perfview-path <path>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import Runner, RunnerConfig, RunnerError
from .scenario import ScenarioValidationError, load_scenario

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_DEFAULT_MC_BUILD = "/c/work/desktop/wpf-test/src/motioncatalyst/BUILD/x64_Debug"
_DEFAULT_OUT_DIR = "traces"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wpf-perf-runner",
        description=(
            "Drive MotionCatalyst through a perf scenario with PerfView ETW capture.\n\n"
            "Outputs one .etl trace file per run plus a .runner.json sidecar "
            "with step timings and metadata."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single run, quick smoke:
  wpf-perf-runner --scenario scenarios/v1.json --wpf-sha if.72

  # 5 measured runs (+ 1 warmup = 6 total):
  wpf-perf-runner --scenario scenarios/v1.json --wpf-sha if.72 --runs 5

  # Full options:
  wpf-perf-runner \\
    --scenario scenarios/v1.json \\
    --wpf-sha if.72 \\
    --runs 5 \\
    --out-dir traces/session-001 \\
    --mc-build /c/work/desktop/wpf-test/src/motioncatalyst/BUILD/x64_Release \\
    --ui-mcp-host-bin /c/work/desktop/wpf-test/Tools/ui-mcp-host/bin/Release/\\
        net10.0-windows10.0.19041.0/UiMcpHost.exe \\
    --perfview-path "C:\\\\PerfView\\\\PerfView.exe"
""",
    )

    p.add_argument(
        "--scenario",
        required=True,
        metavar="PATH",
        help="Path to the scenario JSON file (validated against scenarios/schema.json).",
    )
    p.add_argument(
        "--wpf-sha",
        required=True,
        metavar="LABEL",
        help="Label identifying the WPF build under test (e.g. 'if.72', 'baseline'). "
             "Used in output file names.",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help="Number of measured runs (default: 1). One additional warmup run is always prepended.",
    )
    p.add_argument(
        "--out-dir",
        default=_DEFAULT_OUT_DIR,
        metavar="PATH",
        help=f"Directory for .etl and .runner.json output files (default: {_DEFAULT_OUT_DIR!r}).",
    )
    p.add_argument(
        "--mc-build",
        default=_DEFAULT_MC_BUILD,
        metavar="PATH",
        help=f"MC build output directory containing MotionCatalyst-cli.exe "
             f"(default: {_DEFAULT_MC_BUILD!r}).",
    )
    p.add_argument(
        "--ui-mcp-host-bin",
        default=None,
        metavar="PATH",
        help="Path to UiMcpHost.exe. Auto-discovered if omitted.",
    )
    p.add_argument(
        "--perfview-path",
        default=None,
        metavar="PATH",
        help="Path to PerfView.exe. Auto-discovered if omitted "
             "(checks C:\\PerfView\\PerfView.exe and PATH).",
    )
    p.add_argument(
        "--perf-hive-dir",
        default=None,
        metavar="PATH",
        help="Directory containing perf-hive XML templates (and optional seed.sql). "
             "If not set, an empty hive is used.",
    )
    p.add_argument(
        "--screens-yml",
        default=None,
        metavar="PATH",
        help="Path to screens.yml for mc_navigate_to navigation graph. "
             "Auto-discovered relative to --mc-build if omitted.",
    )
    p.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate the scenario JSON — do not run anything.",
    )

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --- Load & validate scenario ---
    try:
        scenario = load_scenario(args.scenario)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ScenarioValidationError as exc:
        print(f"error: scenario validation failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"[cli] Scenario: {scenario.name!r} "
        f"({len(scenario.warmup_steps)} warmup steps, {len(scenario.steps)} measured steps)",
        flush=True,
    )

    if args.validate_only:
        print("[cli] Scenario is valid (--validate-only).", flush=True)
        return 0

    if args.runs < 1:
        print("error: --runs must be >= 1", file=sys.stderr)
        return 2

    # --- Build config ---
    cfg = RunnerConfig(
        scenario_path=Path(args.scenario).resolve(),
        wpf_sha=args.wpf_sha,
        runs=args.runs,
        out_dir=Path(args.out_dir).resolve(),
        mc_build=Path(args.mc_build).resolve(),
        ui_mcp_host_bin=args.ui_mcp_host_bin,
        perfview_path=args.perfview_path,
        perf_hive_dir=Path(args.perf_hive_dir).resolve() if args.perf_hive_dir else None,
        screens_yml=Path(args.screens_yml).resolve() if args.screens_yml else None,
    )

    # --- Run ---
    runner = Runner(cfg)
    try:
        results = runner.run(scenario)
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[cli] Interrupted.", file=sys.stderr)
        return 130

    # Summary.
    measured = [r for r in results if not r.is_warmup]
    failed = [r for r in measured if not r.scenario_complete]
    print(
        f"\n[cli] Done. {len(measured)} measured run(s), "
        f"{len(measured) - len(failed)} succeeded, {len(failed)} failed.",
        flush=True,
    )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
