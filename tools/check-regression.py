#!/usr/bin/env python3
"""
check-regression.py
===================
Reads BenchmarkDotNet JSON results for the current run and a baseline run,
then checks each scenario for performance regressions.

Thresholds (applied to both Mean latency and Allocated bytes):
  > warn_threshold  (default 5%)  -> status: warning, exit 0
  > fail_threshold  (default 15%) -> status: fail,    exit 2
  otherwise                       -> status: pass,     exit 0

Usage:
  python tools/check-regression.py \\
      --current  perf/current.json \\
      --baseline perf/baseline.json \\
      --current-sha <git-sha> \\
      [--output /tmp/result.json] \\
      [--threshold-warn 5] \\
      [--threshold-fail 15] \\
      [--strict-new-scenarios] \\
      [--allow-new-scenarios name1,name2]

Exit codes:
  0 — pass or warning
  2 — one or more scenarios exceed the fail threshold, or empty comparison

Output JSON shape:
  {
    "current_sha": "<sha>",
    "status": "pass|warning|fail",
    "scenarios": [
      {
        "name": "<FullName>",
        "metric": "Mean|Allocated",
        "baseline": <float>,
        "current": <float>,
        "delta_pct": <float>,
        "verdict": "pass|warning|fail|warning(no_baseline)"
      }
    ]
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class ScenarioResult:
    """Result for a single scenario/metric comparison."""

    def __init__(
        self,
        name: str,
        metric: str,
        baseline: float,
        current: float,
        delta_pct: float,
        verdict: str,
        reason: str = "",
    ) -> None:
        self.name = name
        self.metric = metric
        self.baseline = baseline
        self.current = current
        self.delta_pct = delta_pct
        self.verdict = verdict
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "metric": self.metric,
            "baseline": self.baseline,
            "current": self.current,
            "delta_pct": round(self.delta_pct, 4),
            "verdict": self.verdict,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def load_bdn_json(path: str) -> dict[str, Any]:
    """Load and parse a BenchmarkDotNet JSON export file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    raw = p.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def extract_benchmarks(data: dict[str, Any]) -> dict[str, dict[str, float]]:
    """
    Extract per-benchmark Mean and Allocated metrics from a BDN JSON.

    Returns a dict mapping FullName -> {"Mean": <ns>, "Allocated": <bytes>}.
    """
    result: dict[str, dict[str, float]] = {}
    benchmarks = data.get("Benchmarks", [])
    if not isinstance(benchmarks, list):
        raise ValueError("'Benchmarks' field is not a list")

    for bench in benchmarks:
        if not isinstance(bench, dict):
            continue
        full_name = bench.get("FullName", "")
        if not isinstance(full_name, str) or not full_name:
            continue

        # Mean latency (nanoseconds) from Statistics.Mean
        stats = bench.get("Statistics", {})
        if not isinstance(stats, dict):
            continue
        mean_val = stats.get("Mean")
        if not isinstance(mean_val, (int, float)):
            continue

        # Allocation from Memory.BytesAllocatedPerOperation
        memory = bench.get("Memory", {})
        allocated: float = 0.0
        if isinstance(memory, dict):
            alloc_val = memory.get("BytesAllocatedPerOperation")
            if isinstance(alloc_val, (int, float)):
                allocated = float(alloc_val)

        result[full_name] = {
            "Mean": float(mean_val),
            "Allocated": allocated,
        }

    return result


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def compute_delta_pct(baseline: float, current: float) -> float:
    """Compute percent change from baseline to current.

    When baseline is zero and current is also zero, no change occurred: return 0.0.
    When baseline is zero but current is positive, the regression is unbounded:
    return float('inf') so that threshold checks always produce a 'fail' verdict.
    """
    if baseline == 0.0:
        if current == 0.0:
            return 0.0
        return float("inf")
    return (current - baseline) / baseline * 100.0


def verdict_for_delta(
    delta_pct: float,
    threshold_warn: float,
    threshold_fail: float,
) -> str:
    """Return 'pass', 'warning', or 'fail' based on delta percentage."""
    if delta_pct > threshold_fail:
        return "fail"
    if delta_pct > threshold_warn:
        return "warning"
    return "pass"


def compare_benchmarks(
    baseline_data: dict[str, dict[str, float]],
    current_data: dict[str, dict[str, float]],
    threshold_warn: float,
    threshold_fail: float,
    strict_new_scenarios: bool = False,
    allow_new_scenarios: set[str] | None = None,
) -> list[ScenarioResult]:
    """
    Compare current benchmarks against baseline.

    Only scenarios present in the current run are evaluated.
    Scenarios in current but missing from baseline are no longer silently skipped;
    instead each such scenario produces a 'warning' result with reason='no_baseline'
    (or 'fail' when strict_new_scenarios=True, or 'pass' when the scenario name is
    in allow_new_scenarios).
    """
    if allow_new_scenarios is None:
        allow_new_scenarios = set()

    results: list[ScenarioResult] = []

    for full_name, current_metrics in current_data.items():
        if full_name not in baseline_data:
            # Determine the verdict for this new scenario.
            if full_name in allow_new_scenarios:
                new_verdict = "pass"
            elif strict_new_scenarios:
                new_verdict = "fail"
            else:
                new_verdict = "warning"

            print(
                f"  NEW  {full_name}: not found in baseline (verdict={new_verdict})",
                file=sys.stderr,
            )
            # Emit one result per expected metric so callers can see the scenario.
            for metric in ("Mean", "Allocated"):
                current_val = current_metrics.get(metric, 0.0)
                if metric == "Allocated" and current_val == 0.0:
                    continue
                results.append(
                    ScenarioResult(
                        name=full_name,
                        metric=metric,
                        baseline=0.0,
                        current=current_val,
                        delta_pct=float("inf") if current_val > 0.0 else 0.0,
                        verdict=new_verdict,
                        reason="no_baseline",
                    )
                )
            continue

        baseline_metrics = baseline_data[full_name]

        for metric in ("Mean", "Allocated"):
            baseline_val = baseline_metrics.get(metric, 0.0)
            current_val = current_metrics.get(metric, 0.0)

            # Skip allocation checks when both sides are zero
            if metric == "Allocated" and baseline_val == 0.0 and current_val == 0.0:
                continue

            delta = compute_delta_pct(baseline_val, current_val)
            verdict = verdict_for_delta(delta, threshold_warn, threshold_fail)

            results.append(
                ScenarioResult(
                    name=full_name,
                    metric=metric,
                    baseline=baseline_val,
                    current=current_val,
                    delta_pct=delta,
                    verdict=verdict,
                )
            )

    return results


def aggregate_status(results: list[ScenarioResult]) -> str:
    """Return 'pass', 'warning', or 'fail' from a list of scenario results.

    An empty results list means no scenarios were compared at all, which is
    treated as 'fail' (empty_comparison) to avoid silently passing on broken
    runs where no data was produced.
    """
    if not results:
        return "fail"
    has_warning = False
    for r in results:
        if r.verdict == "fail":
            return "fail"
        if r.verdict == "warning":
            has_warning = True
    return "warning" if has_warning else "pass"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check BenchmarkDotNet results for performance regressions.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--current",
        required=True,
        metavar="PATH",
        help="Path to the current BenchmarkDotNet JSON results file.",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        metavar="PATH",
        help="Path to the baseline BenchmarkDotNet JSON results file.",
    )
    parser.add_argument(
        "--current-sha",
        required=True,
        metavar="SHA",
        help="Git SHA of the current build (included in output JSON for correlation).",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Optional path to write the output JSON. Defaults to stdout only.",
    )
    parser.add_argument(
        "--threshold-warn",
        type=float,
        default=5.0,
        metavar="PCT",
        help="Regression percentage that triggers a warning verdict (default: 5).",
    )
    parser.add_argument(
        "--threshold-fail",
        type=float,
        default=15.0,
        metavar="PCT",
        help="Regression percentage that triggers a fail verdict (default: 15).",
    )
    parser.add_argument(
        "--strict-new-scenarios",
        action="store_true",
        default=False,
        help=(
            "Escalate new scenarios (present in current but absent from baseline) "
            "from 'warning' to 'fail'."
        ),
    )
    parser.add_argument(
        "--allow-new-scenarios",
        metavar="NAMES",
        default="",
        help=(
            "Comma-separated list of scenario names that are explicitly allowed as "
            "new (verdict=pass instead of warning)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    threshold_warn: float = args.threshold_warn
    threshold_fail: float = args.threshold_fail
    current_sha: str = args.current_sha
    strict_new_scenarios: bool = args.strict_new_scenarios
    allow_new_scenarios: set[str] = (
        {n.strip() for n in args.allow_new_scenarios.split(",") if n.strip()}
        if args.allow_new_scenarios
        else set()
    )

    if threshold_warn >= threshold_fail:
        print(
            "ERROR: --threshold-warn must be less than --threshold-fail",
            file=sys.stderr,
        )
        return 1

    # Load files
    try:
        current_raw = load_bdn_json(args.current)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR loading --current file: {exc}", file=sys.stderr)
        return 1

    try:
        baseline_raw = load_bdn_json(args.baseline)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR loading --baseline file: {exc}", file=sys.stderr)
        return 1

    # Extract metrics
    try:
        current_metrics = extract_benchmarks(current_raw)
        baseline_metrics = extract_benchmarks(baseline_raw)
    except ValueError as exc:
        print(f"ERROR parsing benchmark data: {exc}", file=sys.stderr)
        return 1

    # Compare
    results = compare_benchmarks(
        baseline_metrics,
        current_metrics,
        threshold_warn,
        threshold_fail,
        strict_new_scenarios=strict_new_scenarios,
        allow_new_scenarios=allow_new_scenarios,
    )

    status = aggregate_status(results)

    output_doc: dict[str, Any] = {
        "current_sha": current_sha,
        "status": status,
        "scenarios": [r.to_dict() for r in results],
    }
    if status == "fail" and not results:
        output_doc["reason"] = "empty_comparison"

    output_json = json.dumps(output_doc, indent=2)

    # Write to file if requested
    if args.output:
        Path(args.output).write_text(output_json + "\n", encoding="utf-8")

    # Always print to stdout
    print(output_json)

    # Print human-readable summary to stderr
    fail_count = sum(1 for r in results if r.verdict == "fail")
    warn_count = sum(1 for r in results if r.verdict == "warning")
    print(
        f"\nsha={current_sha} status={status} "
        f"scenarios={len(results)} warnings={warn_count} failures={fail_count}",
        file=sys.stderr,
    )

    if status == "fail":
        for r in results:
            if r.verdict == "fail":
                print(
                    f"  FAIL  {r.name} [{r.metric}]: "
                    f"+{r.delta_pct:.1f}% "
                    f"(baseline={r.baseline:.0f} current={r.current:.0f})",
                    file=sys.stderr,
                )
        return 2

    if status == "warning":
        for r in results:
            if r.verdict == "warning":
                print(
                    f"  WARN  {r.name} [{r.metric}]: "
                    f"+{r.delta_pct:.1f}% "
                    f"(baseline={r.baseline:.0f} current={r.current:.0f})",
                    file=sys.stderr,
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
