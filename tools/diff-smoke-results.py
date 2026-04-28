#!/usr/bin/env python3
"""
diff-smoke-results.py
=====================
Compare upstream-clean vs fork smoke-harness NUnit/TRX XML outputs and report
any regressions, new scenarios, or missing scenarios.

Usage:
  python tools/diff-smoke-results.py \\
      --upstream <path> \\
      --fork <path> \\
      --output <path>

Exit codes:
  0 — perfect match (verdict: match)
  1 — drift: scenario set changed but no pass->fail regressions (verdict: drift)
  2 — regression: at least one scenario went from pass to fail (verdict: regression)

Output JSON shape:
  {
    "matched_scenarios": <int>,
    "regressions": [
      {
        "scenario": "<name>",
        "upstream_status": "pass|fail|skip",
        "fork_status": "pass|fail|skip",
        "details": "<human-readable explanation>"
      }
    ],
    "new_scenarios": ["<name>", ...],
    "missing_scenarios": ["<name>", ...],
    "verdict": "match|regression|drift"
  }

XML format support:
  NUnit 3 (TestResult/@type="Assembly"):
    <test-case name="..." result="Passed|Failed|Skipped" duration="..." />
    Pixel-diff metadata expected in <properties>:
      <property name="pixel-diff-hash" value="<sha256>" />

  TRX (Visual Studio test result):
    <UnitTestResult testName="..." outcome="Passed|Failed|NotExecuted" duration="..." />
    Pixel-diff metadata expected in <Output><StdOut>:
      pixel-diff-hash: <sha256>
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScenarioResult:
    """Result for a single test scenario parsed from NUnit/TRX XML."""

    name: str
    status: str           # "pass", "fail", "skip"
    duration_s: float     # seconds; 0.0 if unavailable
    pixel_diff_hash: str  # SHA-256 hex string or "" if not present


@dataclass
class DiffOutput:
    """Top-level comparison output."""

    matched_scenarios: int = 0
    regressions: list[dict[str, str]] = field(default_factory=list)
    new_scenarios: list[str] = field(default_factory=list)
    missing_scenarios: list[str] = field(default_factory=list)
    verdict: str = "match"

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_scenarios": self.matched_scenarios,
            "regressions": self.regressions,
            "new_scenarios": self.new_scenarios,
            "missing_scenarios": self.missing_scenarios,
            "verdict": self.verdict,
        }


# ---------------------------------------------------------------------------
# Status normalisation
# ---------------------------------------------------------------------------

_NUNIT_PASS = frozenset({"Passed", "passed", "Success", "success"})
_NUNIT_FAIL = frozenset({"Failed", "failed", "Error", "error"})
_TRX_PASS = frozenset({"Passed", "passed"})
_TRX_FAIL = frozenset({"Failed", "failed"})


def _normalise_nunit_status(raw: str) -> str:
    if raw in _NUNIT_PASS:
        return "pass"
    if raw in _NUNIT_FAIL:
        return "fail"
    return "skip"


def _normalise_trx_status(raw: str) -> str:
    if raw in _TRX_PASS:
        return "pass"
    if raw in _TRX_FAIL:
        return "fail"
    return "skip"


def _parse_duration(raw: str | None) -> float:
    """Parse a duration string to seconds.  Accepts float seconds or H:MM:SS.ffffff."""
    if not raw:
        return 0.0
    raw = raw.strip()
    # H:MM:SS.ffffff  (TRX format)
    if ":" in raw:
        parts = raw.split(":")
        try:
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
        except ValueError:
            return 0.0
    # plain float seconds (NUnit)
    try:
        return float(raw)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# NUnit XML parser
# ---------------------------------------------------------------------------

def _parse_nunit(root: ET.Element) -> dict[str, ScenarioResult]:
    """Parse NUnit 3 XML (TestResult root or test-run root)."""
    results: dict[str, ScenarioResult] = {}

    for tc in root.iter("test-case"):
        name: str = tc.get("name", "") or tc.get("fullname", "")
        if not name:
            continue

        raw_result = tc.get("result", "")
        status = _normalise_nunit_status(raw_result)
        duration = _parse_duration(tc.get("duration"))

        # Pixel-diff hash from <properties><property name="pixel-diff-hash" value="..." />
        pixel_hash = ""
        props = tc.find("properties")
        if props is not None:
            for prop in props.iter("property"):
                if prop.get("name") == "pixel-diff-hash":
                    pixel_hash = prop.get("value", "")
                    break

        results[name] = ScenarioResult(
            name=name,
            status=status,
            duration_s=duration,
            pixel_diff_hash=pixel_hash,
        )

    return results


# ---------------------------------------------------------------------------
# TRX XML parser
# ---------------------------------------------------------------------------

_TRX_NS = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"


def _trx_tag(local: str) -> str:
    return f"{{{_TRX_NS}}}{local}"


def _parse_trx(root: ET.Element) -> dict[str, ScenarioResult]:
    """Parse Visual Studio TRX XML (TestRun root)."""
    results: dict[str, ScenarioResult] = {}

    for ur in root.iter(_trx_tag("UnitTestResult")):
        name = ur.get("testName", "")
        if not name:
            continue

        raw_outcome = ur.get("outcome", "")
        status = _normalise_trx_status(raw_outcome)
        duration = _parse_duration(ur.get("duration"))

        # Pixel-diff hash from <Output><StdOut>pixel-diff-hash: <sha256>
        pixel_hash = ""
        output = ur.find(_trx_tag("Output"))
        if output is not None:
            stdout = output.find(_trx_tag("StdOut"))
            if stdout is not None and stdout.text:
                for line in stdout.text.splitlines():
                    if line.startswith("pixel-diff-hash:"):
                        pixel_hash = line.split(":", 1)[1].strip()
                        break

        results[name] = ScenarioResult(
            name=name,
            status=status,
            duration_s=duration,
            pixel_diff_hash=pixel_hash,
        )

    return results


# ---------------------------------------------------------------------------
# Format detection and dispatch
# ---------------------------------------------------------------------------

def parse_xml(path: Path) -> dict[str, ScenarioResult]:
    """Parse a NUnit or TRX XML file and return a scenario-name keyed dict."""
    text = path.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"XML parse error in {path}: {exc}") from exc

    # Strip namespace prefix for tag comparison (ElementTree stores as {ns}local)
    tag = root.tag
    local_tag = tag.split("}")[-1] if "}" in tag else tag

    if local_tag in {"TestRun"}:
        # TRX
        return _parse_trx(root)

    if local_tag in {"test-results", "test-run", "TestResult"}:
        # NUnit 2/3
        return _parse_nunit(root)

    # Fallback: try NUnit then TRX
    nunit = _parse_nunit(root)
    if nunit:
        return nunit
    return _parse_trx(root)


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------

def diff_results(
    upstream: dict[str, ScenarioResult],
    fork: dict[str, ScenarioResult],
) -> DiffOutput:
    """Compute the diff between upstream and fork scenario results."""
    out = DiffOutput()

    upstream_names = set(upstream)
    fork_names = set(fork)

    out.missing_scenarios = sorted(upstream_names - fork_names)
    out.new_scenarios = sorted(fork_names - upstream_names)

    common = upstream_names & fork_names
    out.matched_scenarios = len(common)

    for name in sorted(common):
        up = upstream[name]
        fk = fork[name]

        details_parts: list[str] = []

        # Status regression: pass -> fail
        status_regressed = up.status == "pass" and fk.status == "fail"

        # Duration delta (always include for regression details)
        if up.duration_s > 0 and fk.duration_s > 0:
            delta_pct = (fk.duration_s - up.duration_s) / up.duration_s * 100.0
            details_parts.append(
                f"duration delta {delta_pct:+.1f}% "
                f"(upstream={up.duration_s:.3f}s fork={fk.duration_s:.3f}s)"
            )

        # Pixel-diff hash mismatch
        if up.pixel_diff_hash and fk.pixel_diff_hash:
            if up.pixel_diff_hash != fk.pixel_diff_hash:
                details_parts.append(
                    f"pixel-diff-hash mismatch "
                    f"(upstream={up.pixel_diff_hash[:12]}... "
                    f"fork={fk.pixel_diff_hash[:12]}...)"
                )
                if not status_regressed:
                    # Hash mismatch alone counts as regression
                    status_regressed = True

        if status_regressed:
            details = "; ".join(details_parts) if details_parts else "status changed pass->fail"
            out.regressions.append(
                {
                    "scenario": name,
                    "upstream_status": up.status,
                    "fork_status": fk.status,
                    "details": details,
                }
            )

    # Determine verdict
    if out.regressions:
        out.verdict = "regression"
    elif out.missing_scenarios or out.new_scenarios:
        out.verdict = "drift"
    else:
        out.verdict = "match"

    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diff upstream-clean vs fork smoke-harness NUnit/TRX XML results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--upstream",
        required=True,
        metavar="PATH",
        help="Path to the upstream NUnit/TRX XML results file.",
    )
    parser.add_argument(
        "--fork",
        required=True,
        metavar="PATH",
        help="Path to the fork NUnit/TRX XML results file.",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Optional path to write the output JSON. Defaults to stdout only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    upstream_path = Path(args.upstream)
    fork_path = Path(args.fork)

    if not upstream_path.exists():
        print(f"ERROR: --upstream file not found: {upstream_path}", file=sys.stderr)
        return 2
    if not fork_path.exists():
        print(f"ERROR: --fork file not found: {fork_path}", file=sys.stderr)
        return 2

    try:
        upstream_results = parse_xml(upstream_path)
    except ValueError as exc:
        print(f"ERROR parsing --upstream file: {exc}", file=sys.stderr)
        return 2

    try:
        fork_results = parse_xml(fork_path)
    except ValueError as exc:
        print(f"ERROR parsing --fork file: {exc}", file=sys.stderr)
        return 2

    diff = diff_results(upstream_results, fork_results)
    output_doc = diff.to_dict()
    output_json = json.dumps(output_doc, indent=2)

    if args.output:
        Path(args.output).write_text(output_json + "\n", encoding="utf-8")

    print(output_json)

    # Human-readable summary to stderr
    print(
        f"\nverdict={diff.verdict} "
        f"matched={diff.matched_scenarios} "
        f"regressions={len(diff.regressions)} "
        f"new={len(diff.new_scenarios)} "
        f"missing={len(diff.missing_scenarios)}",
        file=sys.stderr,
    )

    if diff.verdict == "regression":
        for reg in diff.regressions:
            print(
                f"  REGRESSION  {reg['scenario']}: "
                f"{reg['upstream_status']} -> {reg['fork_status']} | {reg['details']}",
                file=sys.stderr,
            )
        return 2

    if diff.verdict == "drift":
        for name in diff.missing_scenarios:
            print(f"  MISSING  {name}", file=sys.stderr)
        for name in diff.new_scenarios:
            print(f"  NEW      {name}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
