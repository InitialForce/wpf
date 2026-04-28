"""
Tests for tools/check-regression.py

Covers:
  - pass (all deltas within threshold)
  - warning (>5%, <=15%)
  - fail (>15%)
  - missing scenario in baseline (skipped with warning, does not error)
  - malformed JSON current file
  - malformed JSON baseline file
  - allocation regression triggers warning/fail
  - custom thresholds
  - output JSON contains current_sha
  - exit-code mapping (0=pass, 0=warning, 2=fail)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Paths relative to repo root
REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "check-regression.py"
FIXTURES = Path(__file__).parent / "fixtures"

BASELINE = str(FIXTURES / "perf-baseline.json")
CURRENT_PASS = str(FIXTURES / "perf-current-pass.json")
CURRENT_WARN = str(FIXTURES / "perf-current-warn.json")
CURRENT_FAIL = str(FIXTURES / "perf-current-fail.json")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def run_script(
    *extra_args: str,
    current: str = CURRENT_PASS,
    baseline: str = BASELINE,
    current_sha: str = "testsha1",
) -> tuple[int, dict[str, Any]]:
    """Run check-regression.py and return (exit_code, parsed_output_json)."""
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--current", current,
        "--baseline", baseline,
        "--current-sha", current_sha,
        *extra_args,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError:
        data = {}
    return result.returncode, data


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestPassScenario:
    def test_exit_code_zero(self) -> None:
        code, _ = run_script(current=CURRENT_PASS)
        assert code == 0

    def test_status_is_pass(self) -> None:
        _, data = run_script(current=CURRENT_PASS)
        assert data.get("status") == "pass"

    def test_current_sha_in_output(self) -> None:
        _, data = run_script(current=CURRENT_PASS, current_sha="abc123")
        assert data.get("current_sha") == "abc123"

    def test_scenarios_present(self) -> None:
        _, data = run_script(current=CURRENT_PASS)
        assert isinstance(data.get("scenarios"), list)
        assert len(data["scenarios"]) > 0

    def test_all_scenario_verdicts_pass(self) -> None:
        _, data = run_script(current=CURRENT_PASS)
        verdicts = {s["verdict"] for s in data["scenarios"]}
        assert verdicts == {"pass"}, f"Unexpected verdicts: {verdicts}"


class TestWarningScenario:
    """perf-current-warn.json has GeometryParserBench at ~8% regression (warn only)."""

    def test_exit_code_zero(self) -> None:
        code, _ = run_script(current=CURRENT_WARN)
        assert code == 0

    def test_status_is_warning(self) -> None:
        _, data = run_script(current=CURRENT_WARN)
        assert data.get("status") == "warning"

    def test_current_sha_propagated(self) -> None:
        _, data = run_script(current=CURRENT_WARN, current_sha="deadbeef")
        assert data.get("current_sha") == "deadbeef"

    def test_warning_scenario_present(self) -> None:
        _, data = run_script(current=CURRENT_WARN)
        warn_scenarios = [s for s in data["scenarios"] if s["verdict"] == "warning"]
        assert len(warn_scenarios) >= 1, "Expected at least one warning scenario"
        # Verify the delta is in the warning range
        for s in warn_scenarios:
            assert s["delta_pct"] > 5.0
            assert s["delta_pct"] <= 15.0


class TestFailScenario:
    """perf-current-fail.json has GeometryParserBench at 20% regression (fail)."""

    def test_exit_code_two(self) -> None:
        code, _ = run_script(current=CURRENT_FAIL)
        assert code == 2

    def test_status_is_fail(self) -> None:
        _, data = run_script(current=CURRENT_FAIL)
        assert data.get("status") == "fail"

    def test_fail_scenario_delta_above_threshold(self) -> None:
        _, data = run_script(current=CURRENT_FAIL)
        fail_scenarios = [s for s in data["scenarios"] if s["verdict"] == "fail"]
        assert len(fail_scenarios) >= 1
        for s in fail_scenarios:
            assert s["delta_pct"] > 15.0


class TestMissingBaselineScenario:
    """A current JSON with a scenario not in the baseline is skipped, not an error."""

    def test_extra_scenario_does_not_error(self, tmp_path: Path) -> None:
        baseline = json.loads(Path(BASELINE).read_text())
        current = json.loads(Path(CURRENT_PASS).read_text())

        # Add a brand-new scenario to current only
        new_bench: dict[str, Any] = {
            "FullName": "InitialForce.WpfSmoke.NewBench.NewMethod",
            "Namespace": "InitialForce.WpfSmoke",
            "Type": "NewBench",
            "Method": "NewMethod",
            "Parameters": "",
            "Statistics": {"Mean": 10000.0},
            "Memory": {"BytesAllocatedPerOperation": 0},
        }
        current["Benchmarks"].append(new_bench)

        cur_path = tmp_path / "current_extra.json"
        bas_path = tmp_path / "baseline.json"
        cur_path.write_text(json.dumps(current))
        bas_path.write_text(json.dumps(baseline))

        code, data = run_script(current=str(cur_path), baseline=str(bas_path))
        # Should still succeed (original scenarios pass)
        assert code == 0
        assert data.get("status") == "pass"
        # The extra scenario must NOT appear in output (it was skipped)
        names = [s["name"] for s in data["scenarios"]]
        assert "InitialForce.WpfSmoke.NewBench.NewMethod" not in names


class TestMalformedJson:
    def test_malformed_current_exits_nonzero(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{ this is not valid json }")
        code, _ = run_script(current=str(bad), baseline=BASELINE)
        assert code != 0

    def test_malformed_baseline_exits_nonzero(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{ this is not valid json }")
        code, _ = run_script(current=CURRENT_PASS, baseline=str(bad))
        assert code != 0

    def test_missing_current_file_exits_nonzero(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.json"
        code, _ = run_script(current=str(missing), baseline=BASELINE)
        assert code != 0

    def test_missing_baseline_file_exits_nonzero(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.json"
        code, _ = run_script(current=CURRENT_PASS, baseline=str(missing))
        assert code != 0


class TestAllocationRegression:
    """Allocation delta uses the same 5%/15% thresholds."""

    def test_allocation_warning(self, tmp_path: Path) -> None:
        baseline = json.loads(Path(BASELINE).read_text())
        current = json.loads(Path(CURRENT_PASS).read_text())

        # Set baseline allocation for JpegDecode100 to 8000 bytes
        # Set current allocation to 8640 bytes → +8% regression (warning)
        for bench in baseline["Benchmarks"]:
            if bench["FullName"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100":
                bench["Memory"]["BytesAllocatedPerOperation"] = 8000
        for bench in current["Benchmarks"]:
            if bench["FullName"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100":
                bench["Memory"]["BytesAllocatedPerOperation"] = 8640

        cur_path = tmp_path / "current_alloc_warn.json"
        bas_path = tmp_path / "baseline_alloc_warn.json"
        cur_path.write_text(json.dumps(current))
        bas_path.write_text(json.dumps(baseline))

        code, data = run_script(current=str(cur_path), baseline=str(bas_path))
        assert code == 0
        alloc_scenarios = [
            s for s in data["scenarios"]
            if s["metric"] == "Allocated" and s["verdict"] == "warning"
        ]
        assert len(alloc_scenarios) >= 1
        for s in alloc_scenarios:
            assert s["delta_pct"] > 5.0
            assert s["delta_pct"] <= 15.0

    def test_allocation_fail(self, tmp_path: Path) -> None:
        baseline = json.loads(Path(BASELINE).read_text())
        current = json.loads(Path(CURRENT_PASS).read_text())

        # Set allocation delta to >15% (fail threshold)
        for bench in baseline["Benchmarks"]:
            if bench["FullName"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100":
                bench["Memory"]["BytesAllocatedPerOperation"] = 8000
        for bench in current["Benchmarks"]:
            if bench["FullName"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100":
                bench["Memory"]["BytesAllocatedPerOperation"] = 9600  # +20%

        cur_path = tmp_path / "current_alloc_fail.json"
        bas_path = tmp_path / "baseline_alloc_fail.json"
        cur_path.write_text(json.dumps(current))
        bas_path.write_text(json.dumps(baseline))

        code, data = run_script(current=str(cur_path), baseline=str(bas_path))
        assert code == 2
        assert data.get("status") == "fail"


class TestCustomThresholds:
    def test_custom_warn_threshold_triggers_earlier(self) -> None:
        # With threshold-warn=1, even the pass fixture should warn
        # (GeometryParserBench pass delta is ~2%)
        code, data = run_script(
            "--threshold-warn", "1",
            "--threshold-fail", "15",
            current=CURRENT_PASS,
        )
        assert code == 0
        assert data.get("status") == "warning"

    def test_custom_fail_threshold_lower(self) -> None:
        # With threshold-fail=3, the warn fixture (8% delta) should fail
        code, data = run_script(
            "--threshold-warn", "1",
            "--threshold-fail", "3",
            current=CURRENT_WARN,
        )
        assert code == 2
        assert data.get("status") == "fail"


class TestOutputFile:
    def test_output_file_written(self, tmp_path: Path) -> None:
        out = tmp_path / "result.json"
        run_script("--output", str(out), current=CURRENT_PASS)
        assert out.exists()
        data = json.loads(out.read_text())
        assert "current_sha" in data
        assert "status" in data
        assert "scenarios" in data

    def test_output_file_has_correct_sha(self, tmp_path: Path) -> None:
        out = tmp_path / "result.json"
        run_script("--output", str(out), current=CURRENT_PASS, current_sha="mysha456")
        data = json.loads(out.read_text())
        assert data["current_sha"] == "mysha456"
