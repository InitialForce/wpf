#!/usr/bin/env python3
"""Render-pass amplification diagnostic for WPF scenario profiles.

Reads an analysis.json produced by the profiling pipeline and computes:

    amplification_ratio = renderPassCount / max(1, animationRenderRate * wallSeconds)

A healthy WPF app keeps this ratio near 1.0 (one render pass per animation tick).
Ratios above 3 indicate something is repeatedly invalidating the render outside the
animation clock; ratios above 10 are critical.

When animationRenderRate == 0 (no animation active during capture), the tool falls
back to comparing absolute renders/sec against a 60 Hz floor:
  - renders/sec > 120  → WARN  (something is redundantly invalidating)
  - renders/sec > 200  → CRITICAL

Usage
-----
    python3 autoresearch/profile-amplification-check.py
    python3 autoresearch/profile-amplification-check.py --analysis path/to/analysis.json
    python3 autoresearch/profile-amplification-check.py --scenario take-open
    python3 autoresearch/profile-amplification-check.py --json
    python3 autoresearch/profile-amplification-check.py --warn-threshold 3.0 --crit-threshold 10.0

Output
------
Human-readable summary by default; JSON-only when --json is given.  Both shapes
are always computed — the JSON can be consumed by downstream tooling (ralph iters,
CI gates) without re-parsing the human text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ─── Defaults ─────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent.resolve()
_PROFILE_OUTPUT_DIR = _ROOT / "profile-output"

# Default render-pass thresholds for the animation-amplification path.
_DEFAULT_WARN_RATIO = 3.0
_DEFAULT_CRIT_RATIO = 10.0

# Absolute renders/sec thresholds used when animationRenderRate == 0.
_NO_ANIM_WARN_RPS = 120.0
_NO_ANIM_CRIT_RPS = 200.0

# Common causes surfaced in the WARN/CRITICAL human-readable report.
_COMMON_CAUSES = [
    "Forever-animation propagating Freezable changes up the visual tree",
    "Layout-affecting binding triggered per-frame",
    "Adorned element with continuously-changing transform",
    "Redundant per-tick InvalidateVisual / InvalidateMeasure",
]


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _resolve_analysis_path(args: argparse.Namespace) -> Path:
    """Return the path to the analysis.json to read.

    Resolution order:
      1. --analysis <path>  — used verbatim.
      2. --scenario <slug>  — looks at profile-output/<slug>/analysis.json,
         then falls back to the top-level profile-output/analysis.json if the
         per-scenario file is absent.
      3. Default             — profile-output/analysis.json.
    """
    if args.analysis:
        p = Path(args.analysis)
        if not p.is_absolute():
            p = Path.cwd() / p
        return p

    if args.scenario:
        slug = args.scenario
        # Per-scenario path (e.g. runs that use SCENARIO_RESULT_DIR=profile-output/<slug>).
        per_scenario = _PROFILE_OUTPUT_DIR / slug / "analysis.json"
        if per_scenario.exists():
            return per_scenario
        # Fall through to top-level (the main profiling pipeline stores a single
        # analysis.json at profile-output/analysis.json regardless of scenario).

    return _PROFILE_OUTPUT_DIR / "analysis.json"


def _read_analysis(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: analysis.json not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: failed to parse {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _extract_animation_rate(data: dict) -> float:
    """Return animationRenderRate from the most recent presentation snapshot.

    The harness takes snapshots periodically; we use the median across all
    snapshots to avoid transient values (e.g. 0 during a brief idle window).
    Falls back to 0.0 if no snapshots are present.
    """
    snapshots = data.get("wpf", {}).get("presentationSnapshots", [])
    if not snapshots:
        return 0.0
    rates = [float(s.get("animationRenderRate", 0)) for s in snapshots]
    # Median — sorted, take middle element.
    rates.sort()
    n = len(rates)
    if n % 2 == 1:
        return rates[n // 2]
    return (rates[n // 2 - 1] + rates[n // 2]) / 2.0


# ─── Core logic ───────────────────────────────────────────────────────────────


def analyze(
    data: dict,
    scenario_name: str,
    warn_threshold: float = _DEFAULT_WARN_RATIO,
    crit_threshold: float = _DEFAULT_CRIT_RATIO,
) -> dict:
    """Compute the amplification result dict from a parsed analysis.json.

    Returns the JSON output shape described in the module docstring.
    """
    capture_span_ms: float = float(data.get("captureSpanMs", 0.0))
    wall_seconds: float = capture_span_ms / 1000.0

    wpf = data.get("wpf", {})
    render_passes: int = int(wpf.get("renderPassCount", 0))
    animation_rate_hz: float = _extract_animation_rate(data)

    render_per_sec: float = render_passes / wall_seconds if wall_seconds > 0 else 0.0

    # ── Choose comparison mode ─────────────────────────────────────────────────
    no_animation_mode = animation_rate_hz == 0.0

    if no_animation_mode:
        # No animation: compare absolute renders/sec against a 60 Hz floor.
        amplification_ratio = render_per_sec / 60.0
        if render_per_sec >= _NO_ANIM_CRIT_RPS:
            verdict = "CRITICAL"
        elif render_per_sec >= _NO_ANIM_WARN_RPS:
            verdict = "WARN"
        else:
            verdict = "OK"
        diagnostic_note = (
            f"No animation detected; comparing {render_per_sec:.1f} renders/sec "
            f"against 60 Hz floor (WARN>{_NO_ANIM_WARN_RPS:.0f}, "
            f"CRITICAL>{_NO_ANIM_CRIT_RPS:.0f})."
        )
    else:
        # Animation present: compute ratio vs expected animation-driven rate.
        expected_passes = animation_rate_hz * wall_seconds
        amplification_ratio = render_passes / max(1.0, expected_passes)
        if amplification_ratio >= crit_threshold:
            verdict = "CRITICAL"
        elif amplification_ratio >= warn_threshold:
            verdict = "WARN"
        else:
            verdict = "OK"
        diagnostic_note = (
            f"Expected ~{expected_passes:.0f} passes "
            f"({animation_rate_hz:.0f} Hz × {wall_seconds:.2f}s); "
            f"actual {render_passes} passes → {amplification_ratio:.1f}× ratio."
        )

    common_causes = _COMMON_CAUSES if verdict != "OK" else []

    return {
        "scenario": scenario_name,
        "wall_seconds": round(wall_seconds, 2),
        "render_passes": render_passes,
        "render_per_sec": round(render_per_sec, 1),
        "animation_rate_hz": animation_rate_hz,
        "amplification_ratio": round(amplification_ratio, 1),
        "verdict": verdict,
        "diagnostic_note": diagnostic_note,
        "common_causes": common_causes,
    }


def print_human(result: dict) -> None:
    """Print a human-readable summary of the amplification result."""
    verdict = result["verdict"]
    prefix = {
        "OK": "   OK",
        "WARN": " WARN",
        "CRITICAL": "CRIT!",
    }.get(verdict, verdict)

    print(
        f"\n[amplification-check] {prefix} — scenario: {result['scenario']}"
    )
    print(
        f"  wall time        : {result['wall_seconds']:.2f}s"
    )
    print(
        f"  render passes    : {result['render_passes']} "
        f"({result['render_per_sec']:.1f}/sec)"
    )
    print(
        f"  animation rate   : {result['animation_rate_hz']:.0f} Hz"
    )
    print(
        f"  amplification    : {result['amplification_ratio']:.1f}×"
    )
    print(f"  note             : {result['diagnostic_note']}")

    if result["common_causes"]:
        print(f"\n  [{verdict}] Common causes of render amplification:")
        for cause in result["common_causes"]:
            print(f"    • {cause}")
    print()


# ─── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--analysis",
        metavar="PATH",
        default="",
        help=(
            "Path to analysis.json. "
            "Default: autoresearch/profile-output/analysis.json "
            "(or profile-output/<scenario>/analysis.json when --scenario is given "
            "and that file exists)."
        ),
    )
    ap.add_argument(
        "--scenario",
        metavar="NAME",
        default="",
        help=(
            "Scenario slug (e.g. 'take-open'). Used both to resolve the "
            "analysis.json path and to label the output. When omitted, the "
            "scenario name is derived from the analysis.json parent directory."
        ),
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit only the machine-readable JSON result (no human text).",
    )
    ap.add_argument(
        "--warn-threshold",
        type=float,
        default=_DEFAULT_WARN_RATIO,
        metavar="RATIO",
        help=f"Amplification ratio threshold for WARN (default {_DEFAULT_WARN_RATIO}).",
    )
    ap.add_argument(
        "--crit-threshold",
        type=float,
        default=_DEFAULT_CRIT_RATIO,
        metavar="RATIO",
        help=f"Amplification ratio threshold for CRITICAL (default {_DEFAULT_CRIT_RATIO}).",
    )
    return ap


def main() -> int:
    ap = _build_parser()
    args = ap.parse_args()

    analysis_path = _resolve_analysis_path(args)
    data = _read_analysis(analysis_path)

    # Derive scenario name: --scenario > parent dir name > "unknown"
    if args.scenario:
        scenario_name = args.scenario
    elif analysis_path.parent.name not in ("profile-output", "autoresearch", "."):
        scenario_name = analysis_path.parent.name
    else:
        # Top-level analysis.json: try to infer from the etlPath field.
        etl = data.get("etlPath", "")
        scenario_name = Path(etl).stem if etl else "unknown"

    result = analyze(
        data,
        scenario_name=scenario_name,
        warn_threshold=args.warn_threshold,
        crit_threshold=args.crit_threshold,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_human(result)
        print(json.dumps(result, indent=2))

    # Exit code: 0=OK, 1=WARN, 2=CRITICAL
    return {"OK": 0, "WARN": 1, "CRITICAL": 2}.get(result["verdict"], 0)


if __name__ == "__main__":
    sys.exit(main())
