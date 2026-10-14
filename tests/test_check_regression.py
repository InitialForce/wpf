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

import pytest

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


NEW_BENCH_NAME = "InitialForce.WpfSmoke.NewBench.NewMethod"


def _make_current_with_new_scenario(tmp_path: Path) -> tuple[str, str]:
    """Return (cur_path, bas_path) where current has one extra new scenario."""
    baseline = json.loads(Path(BASELINE).read_text())
    current = json.loads(Path(CURRENT_PASS).read_text())

    new_bench: dict[str, Any] = {
        "FullName": NEW_BENCH_NAME,
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
    return str(cur_path), str(bas_path)


class TestMissingBaselineScenario:
    """New scenarios (in current but absent from baseline) emit a warning by default."""

    def test_new_scenario_does_not_error(self, tmp_path: Path) -> None:
        cur_path, bas_path = _make_current_with_new_scenario(tmp_path)
        code, data = run_script(current=cur_path, baseline=bas_path)
        # Exit 0 (warning-level, not fail)
        assert code == 0

    def test_new_scenario_produces_warning_status(self, tmp_path: Path) -> None:
        cur_path, bas_path = _make_current_with_new_scenario(tmp_path)
        _, data = run_script(current=cur_path, baseline=bas_path)
        assert data.get("status") == "warning"

    def test_new_scenario_appears_in_output_with_warning_verdict(
        self, tmp_path: Path
    ) -> None:
        cur_path, bas_path = _make_current_with_new_scenario(tmp_path)
        _, data = run_script(current=cur_path, baseline=bas_path)
        new_scenarios = [s for s in data["scenarios"] if s["name"] == NEW_BENCH_NAME]
        assert len(new_scenarios) >= 1, "New scenario must appear in output"
        for s in new_scenarios:
            assert s["verdict"] == "warning"
            assert s.get("reason") == "no_baseline"

    def test_strict_new_scenarios_escalates_to_fail(self, tmp_path: Path) -> None:
        cur_path, bas_path = _make_current_with_new_scenario(tmp_path)
        code, data = run_script(
            "--strict-new-scenarios", current=cur_path, baseline=bas_path
        )
        assert code == 2
        assert data.get("status") == "fail"
        new_scenarios = [s for s in data["scenarios"] if s["name"] == NEW_BENCH_NAME]
        assert all(s["verdict"] == "fail" for s in new_scenarios)

    def test_allow_new_scenarios_produces_pass(self, tmp_path: Path) -> None:
        cur_path, bas_path = _make_current_with_new_scenario(tmp_path)
        code, data = run_script(
            "--allow-new-scenarios", NEW_BENCH_NAME,
            current=cur_path, baseline=bas_path,
        )
        # All original scenarios pass, new one is allowed — overall pass
        assert code == 0
        assert data.get("status") == "pass"
        new_scenarios = [s for s in data["scenarios"] if s["name"] == NEW_BENCH_NAME]
        assert all(s["verdict"] == "pass" for s in new_scenarios)


class TestZeroBaselineRegression:
    """When baseline is 0 and current > 0, the result must be 'fail', not 'pass'."""

    def test_zero_baseline_positive_current_fails(self, tmp_path: Path) -> None:
        baseline = json.loads(Path(BASELINE).read_text())
        current = json.loads(Path(CURRENT_PASS).read_text())

        # Force baseline allocation to 0, current to a positive value
        for bench in baseline["Benchmarks"]:
            if bench["FullName"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100":
                bench["Memory"]["BytesAllocatedPerOperation"] = 0
        for bench in current["Benchmarks"]:
            if bench["FullName"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100":
                bench["Memory"]["BytesAllocatedPerOperation"] = 1024

        cur_path = tmp_path / "current_zero_base.json"
        bas_path = tmp_path / "baseline_zero_base.json"
        cur_path.write_text(json.dumps(current))
        bas_path.write_text(json.dumps(baseline))

        code, data = run_script(current=str(cur_path), baseline=str(bas_path))
        assert code == 2, "Zero-baseline with positive current must exit 2"
        assert data.get("status") == "fail"
        fail_scenarios = [
            s for s in data["scenarios"]
            if s["metric"] == "Allocated" and s["verdict"] == "fail"
            and s["name"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100"
        ]
        assert len(fail_scenarios) >= 1

    def test_zero_baseline_zero_current_passes(self, tmp_path: Path) -> None:
        """Both baseline and current zero means no change — no fail triggered."""
        baseline = json.loads(Path(BASELINE).read_text())
        current = json.loads(Path(CURRENT_PASS).read_text())

        # Keep both at zero for allocation; mean is unchanged
        for bench in baseline["Benchmarks"]:
            if bench["FullName"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100":
                bench["Memory"]["BytesAllocatedPerOperation"] = 0
        for bench in current["Benchmarks"]:
            if bench["FullName"] == "InitialForce.WpfSmoke.ImageLoadingBench.JpegDecode100":
                bench["Memory"]["BytesAllocatedPerOperation"] = 0

        cur_path = tmp_path / "current_zero_both.json"
        bas_path = tmp_path / "baseline_zero_both.json"
        cur_path.write_text(json.dumps(current))
        bas_path.write_text(json.dumps(baseline))

        code, data = run_script(current=str(cur_path), baseline=str(bas_path))
        assert code == 0


class TestEmptyComparison:
    """When no scenarios can be compared at all, status must be 'fail' with exit 2."""

    def test_empty_current_produces_fail(self, tmp_path: Path) -> None:
        baseline = json.loads(Path(BASELINE).read_text())

        # Current with no benchmarks at all
        current_empty: dict[str, Any] = {"Benchmarks": []}

        cur_path = tmp_path / "current_empty.json"
        bas_path = tmp_path / "baseline_empty_test.json"
        cur_path.write_text(json.dumps(current_empty))
        bas_path.write_text(json.dumps(baseline))

        code, data = run_script(current=str(cur_path), baseline=str(bas_path))
        assert code == 2, "Empty comparison must exit 2"
        assert data.get("status") == "fail"
        assert data.get("reason") == "empty_comparison"

    def test_empty_comparison_scenarios_list_empty(self, tmp_path: Path) -> None:
        baseline = json.loads(Path(BASELINE).read_text())
        current_empty: dict[str, Any] = {"Benchmarks": []}

        cur_path = tmp_path / "current_empty2.json"
        bas_path = tmp_path / "baseline_empty_test2.json"
        cur_path.write_text(json.dumps(current_empty))
        bas_path.write_text(json.dumps(baseline))

        _, data = run_script(current=str(cur_path), baseline=str(bas_path))
        assert data.get("scenarios") == []


class TestMixedBaselineAndNew:
    """Mix of existing (pass) and new scenarios: status should reflect worst verdict."""

    def test_mixed_existing_pass_new_warning_gives_warning(
        self, tmp_path: Path
    ) -> None:
        cur_path, bas_path = _make_current_with_new_scenario(tmp_path)
        code, data = run_script(current=cur_path, baseline=bas_path)
        # Original scenarios pass; new scenario is warning; aggregate = warning
        assert code == 0
        assert data.get("status") == "warning"
        verdicts = {s["verdict"] for s in data["scenarios"]}
        assert "warning" in verdicts
        assert "fail" not in verdicts

    def test_mixed_existing_pass_new_strict_gives_fail(
        self, tmp_path: Path
    ) -> None:
        cur_path, bas_path = _make_current_with_new_scenario(tmp_path)
        code, data = run_script(
            "--strict-new-scenarios", current=cur_path, baseline=bas_path
        )
        assert code == 2
        assert data.get("status") == "fail"


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


# ---------------------------------------------------------------------------
# Several reports (one per benchmark class), missing and failed scenarios
# ---------------------------------------------------------------------------

def _split_current_by_class(tmp_path: Path, source: str = CURRENT_PASS) -> list[str]:
    """Split a fixture into one report per benchmark class, like BenchmarkDotNet."""
    current = json.loads(Path(source).read_text())
    paths = []
    for i, bench in enumerate(current["Benchmarks"]):
        report = {k: v for k, v in current.items() if k != "Benchmarks"}
        report["Benchmarks"] = [bench]
        path = tmp_path / f"report-{i}.json"
        path.write_text(json.dumps(report))
        paths.append(str(path))
    return paths


def _run_multi(*extra: str, current: list[str], baseline: str | None = BASELINE) -> tuple[
    int, dict[str, Any]
]:
    cmd = [sys.executable, str(SCRIPT), "--current", *current, "--current-sha", "sha1"]
    if baseline:
        cmd += ["--baseline", baseline]
    result = subprocess.run([*cmd, *extra], capture_output=True, text=True)
    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError:
        data = {}
    return result.returncode, data


class TestMultipleReports:
    def test_all_reports_are_merged(self, tmp_path: Path) -> None:
        paths = _split_current_by_class(tmp_path)
        assert len(paths) == 3
        code, data = _run_multi(current=paths)
        assert code == 0
        assert data["status"] == "pass"
        names = {s["name"] for s in data["scenarios"]}
        assert len(names) == 3

    def test_regression_in_second_report_is_caught(self, tmp_path: Path) -> None:
        """Only the first report used to be checked; a regression in another
        class's report went unnoticed."""
        paths = _split_current_by_class(tmp_path, CURRENT_FAIL)
        code, data = _run_multi(current=paths)
        assert code == 2
        assert data["status"] == "fail"

    def test_duplicate_benchmark_across_reports_is_an_error(self, tmp_path: Path) -> None:
        code, _ = _run_multi(current=[CURRENT_PASS, CURRENT_PASS])
        assert code == 1


class TestMissingAndFailedScenarios:
    def test_missing_baseline_scenario_fails(self, tmp_path: Path) -> None:
        """A run with only one class's report must not pass the whole gate."""
        paths = _split_current_by_class(tmp_path)
        code, data = _run_multi(current=paths[:1])
        assert code == 2
        assert data["status"] == "fail"
        missing = [s for s in data["scenarios"] if s.get("reason") == "missing_in_current"]
        assert len(missing) == 2

    def test_failed_benchmark_without_statistics_fails(self, tmp_path: Path) -> None:
        current = json.loads(Path(CURRENT_PASS).read_text())
        current["Benchmarks"][1]["Statistics"] = None
        failed_name = current["Benchmarks"][1]["FullName"]
        cur = tmp_path / "current.json"
        cur.write_text(json.dumps(current))
        code, data = _run_multi(current=[str(cur)])
        assert code == 2
        failed = [s for s in data["scenarios"] if s["name"] == failed_name]
        assert failed and failed[0]["reason"] == "no_measurement"
        assert failed[0]["verdict"] == "fail"

    def test_failed_new_benchmark_fails(self, tmp_path: Path) -> None:
        current = json.loads(Path(CURRENT_PASS).read_text())
        current["Benchmarks"].append({"FullName": "X.NewBench.Broken", "Statistics": None})
        cur = tmp_path / "current.json"
        cur.write_text(json.dumps(current))
        code, data = _run_multi(current=[str(cur)])
        assert code == 2
        assert any(s.get("reason") == "no_measurement" for s in data["scenarios"])


class TestMergeOnly:
    def test_merge_only_writes_all_benchmarks(self, tmp_path: Path) -> None:
        paths = _split_current_by_class(tmp_path)
        out = tmp_path / "baseline.json"
        code, _ = _run_multi(
            "--merge-only", "--merged-output", str(out), current=paths, baseline=None
        )
        assert code == 0
        merged = json.loads(out.read_text())
        assert len(merged["Benchmarks"]) == 3
        assert "HostEnvironmentInfo" in merged

    def test_merge_only_fails_on_unmeasured(self, tmp_path: Path) -> None:
        current = json.loads(Path(CURRENT_PASS).read_text())
        current["Benchmarks"][0]["Statistics"] = {}
        cur = tmp_path / "current.json"
        cur.write_text(json.dumps(current))
        code, _ = _run_multi(
            "--merge-only", "--merged-output", str(tmp_path / "b.json"),
            current=[str(cur)], baseline=None,
        )
        assert code == 2

    def test_merge_only_fails_on_empty(self, tmp_path: Path) -> None:
        cur = tmp_path / "current.json"
        cur.write_text(json.dumps({"Benchmarks": []}))
        code, _ = _run_multi(
            "--merge-only", "--merged-output", str(tmp_path / "b.json"),
            current=[str(cur)], baseline=None,
        )
        assert code == 2

    def test_baseline_required_without_merge_only(self) -> None:
        code, _ = _run_multi(current=[CURRENT_PASS], baseline=None)
        assert code == 1


class TestAllocationMeasurementRequired:
    """Regression: a missing Memory.BytesAllocatedPerOperation was read as 0,
    so the hard Allocated gate reported a 100% improvement (or skipped the
    check against a zero baseline) and passed."""

    @staticmethod
    def _write(tmp_path: Path, name: str, doc: dict[str, Any]) -> str:
        path = tmp_path / name
        path.write_text(json.dumps(doc))
        return str(path)

    def _current_with(self, tmp_path: Path, memory: Any, drop: bool = False) -> tuple[str, str]:
        current = json.loads(Path(CURRENT_PASS).read_text())
        bench = current["Benchmarks"][0]
        if drop:
            bench.pop("Memory", None)
        elif memory is not None or "Memory" in bench:
            bench["Memory"] = memory
        return self._write(tmp_path, "current.json", current), bench["FullName"]

    @pytest.mark.parametrize(
        "memory",
        [
            None,
            {},
            {"BytesAllocatedPerOperation": None},
            {"BytesAllocatedPerOperation": "100"},
            {"BytesAllocatedPerOperation": True},
            {"BytesAllocatedPerOperation": -1},
        ],
        ids=["null-memory", "empty-memory", "null", "string", "bool", "negative"],
    )
    def test_invalid_allocation_fails(self, tmp_path: Path, memory: Any) -> None:
        cur, name = self._current_with(tmp_path, memory)
        code, data = _run_multi(current=[cur])
        assert code == 2
        failed = [s for s in data["scenarios"] if s["name"] == name]
        assert failed and failed[0]["reason"] == "no_measurement"

    def test_missing_memory_block_fails(self, tmp_path: Path) -> None:
        cur, name = self._current_with(tmp_path, None, drop=True)
        code, data = _run_multi(current=[cur])
        assert code == 2
        assert any(s["name"] == name and s["verdict"] == "fail" for s in data["scenarios"])

    @pytest.mark.parametrize("value", ["NaN", "Infinity"])
    def test_non_finite_values_fail(self, tmp_path: Path, value: str) -> None:
        current = json.loads(Path(CURRENT_PASS).read_text())
        current["Benchmarks"][0]["Memory"] = {"BytesAllocatedPerOperation": 0}
        text = json.dumps(current).replace(
            '"BytesAllocatedPerOperation": 0', f'"BytesAllocatedPerOperation": {value}', 1
        )
        cur = tmp_path / "current.json"
        cur.write_text(text)
        code, _ = _run_multi(current=[str(cur)])
        assert code == 2
        current["Benchmarks"][0]["Memory"] = {"BytesAllocatedPerOperation": 0}
        current["Benchmarks"][0]["Statistics"]["Mean"] = 1.0
        text = json.dumps(current).replace('"Mean": 1.0', f'"Mean": {value}', 1)
        cur.write_text(text)
        code, _ = _run_multi(current=[str(cur)])
        assert code == 2

    def test_measured_zero_allocation_is_fine(self, tmp_path: Path) -> None:
        current = json.loads(Path(CURRENT_PASS).read_text())
        baseline = json.loads(Path(BASELINE).read_text())
        for doc in (current, baseline):
            doc["Benchmarks"][0]["Memory"] = {"BytesAllocatedPerOperation": 0}
        cur = self._write(tmp_path, "current.json", current)
        base = self._write(tmp_path, "baseline.json", baseline)
        code, data = _run_multi(current=[cur], baseline=base)
        assert code == 0, data

    def test_baseline_without_allocation_is_an_error(self, tmp_path: Path) -> None:
        baseline = json.loads(Path(BASELINE).read_text())
        baseline["Benchmarks"][0]["Memory"] = {"BytesAllocatedPerOperation": None}
        base = self._write(tmp_path, "baseline.json", baseline)
        code, _ = _run_multi(current=[CURRENT_PASS], baseline=base)
        assert code == 2

    def test_regeneration_refuses_missing_allocation(self, tmp_path: Path) -> None:
        cur, _ = self._current_with(tmp_path, {"BytesAllocatedPerOperation": None})
        out = tmp_path / "baseline.json"
        code, _ = _run_multi(
            "--merge-only", "--merged-output", str(out), current=[cur], baseline=None
        )
        assert code == 2

    def test_checked_in_baseline_is_valid(self) -> None:
        code, _ = _run_multi(current=[str(REPO_ROOT / "perf" / "baseline.json")],
                             baseline=str(REPO_ROOT / "perf" / "baseline.json"))
        assert code == 0
