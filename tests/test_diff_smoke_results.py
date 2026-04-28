"""
Tests for tools/diff-smoke-results.py

Covers:
  - match case (identical pass/fail + identical pixel-diff hashes) -> exit 0, verdict "match"
  - regression case (pass -> fail) -> exit 2, verdict "regression", regressions list non-empty
  - pixel-diff hash mismatch -> exit 2, verdict "regression"
  - drift case (missing/new scenarios) -> exit 1, verdict "drift"
  - missing upstream file -> exit 2 (error)
  - missing fork file -> exit 2 (error)
  - malformed XML -> exit 2 (error)
  - output JSON written to file
  - matched_scenarios count is correct
  - new_scenarios and missing_scenarios lists are populated
  - duration delta appears in regression details
  - TRX format parsed correctly
  - NUnit format with no pixel-diff metadata still works
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "diff-smoke-results.py"
FIXTURES = Path(__file__).parent / "fixtures"

UPSTREAM = str(FIXTURES / "smoke-upstream.xml")
FORK_PASS = str(FIXTURES / "smoke-fork-pass.xml")
FORK_REGRESSION = str(FIXTURES / "smoke-fork-regression.xml")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def run_script(
    upstream: str = UPSTREAM,
    fork: str = FORK_PASS,
    extra_args: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run diff-smoke-results.py and return (exit_code, parsed_output_json)."""
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--upstream", upstream,
        "--fork", fork,
        *(extra_args or []),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError:
        data = {}
    return result.returncode, data


# ---------------------------------------------------------------------------
# Match case
# ---------------------------------------------------------------------------

class TestMatchCase:
    """upstream vs fork-pass: all scenarios identical status + hashes."""

    def test_exit_code_zero(self) -> None:
        code, _ = run_script()
        assert code == 0

    def test_verdict_match(self) -> None:
        _, data = run_script()
        assert data.get("verdict") == "match"

    def test_regressions_empty(self) -> None:
        _, data = run_script()
        assert data.get("regressions") == []

    def test_new_scenarios_empty(self) -> None:
        _, data = run_script()
        assert data.get("new_scenarios") == []

    def test_missing_scenarios_empty(self) -> None:
        _, data = run_script()
        assert data.get("missing_scenarios") == []

    def test_matched_scenarios_count(self) -> None:
        _, data = run_script()
        # All 22 scenarios are present in both files
        assert data.get("matched_scenarios") == 22


# ---------------------------------------------------------------------------
# Regression case
# ---------------------------------------------------------------------------

class TestRegressionCase:
    """upstream vs fork-regression: PrepareComparerZeroAllocs fails + XamlSceneA hash changed."""

    def test_exit_code_two(self) -> None:
        code, _ = run_script(fork=FORK_REGRESSION)
        assert code == 2

    def test_verdict_regression(self) -> None:
        _, data = run_script(fork=FORK_REGRESSION)
        assert data.get("verdict") == "regression"

    def test_regressions_non_empty(self) -> None:
        _, data = run_script(fork=FORK_REGRESSION)
        assert len(data.get("regressions", [])) >= 1

    def test_status_regression_scenario_listed(self) -> None:
        _, data = run_script(fork=FORK_REGRESSION)
        names = [r["scenario"] for r in data["regressions"]]
        assert "ListCollectionViewTests.PrepareComparerZeroAllocs" in names

    def test_regression_upstream_status_pass(self) -> None:
        _, data = run_script(fork=FORK_REGRESSION)
        for reg in data["regressions"]:
            if reg["scenario"] == "ListCollectionViewTests.PrepareComparerZeroAllocs":
                assert reg["upstream_status"] == "pass"

    def test_regression_fork_status_fail(self) -> None:
        _, data = run_script(fork=FORK_REGRESSION)
        for reg in data["regressions"]:
            if reg["scenario"] == "ListCollectionViewTests.PrepareComparerZeroAllocs":
                assert reg["fork_status"] == "fail"

    def test_pixel_diff_hash_mismatch_is_regression(self) -> None:
        _, data = run_script(fork=FORK_REGRESSION)
        names = [r["scenario"] for r in data["regressions"]]
        # XamlSceneA has a different pixel-diff hash in the regression fixture
        assert "PixelDiffTests.XamlSceneA" in names

    def test_no_drift_when_regression(self) -> None:
        _, data = run_script(fork=FORK_REGRESSION)
        # Both files have the same 22 scenarios; no new/missing
        assert data.get("new_scenarios") == []
        assert data.get("missing_scenarios") == []


# ---------------------------------------------------------------------------
# Drift case
# ---------------------------------------------------------------------------

class TestDriftCase:
    """Fork has a different scenario set: one missing, one new."""

    def _make_drift_xml(self, tmp_path: Path) -> Path:
        """Copy fork-pass XML but remove one scenario and add a new one."""
        tree = ET.parse(FORK_PASS)
        root = tree.getroot()

        # Remove LifecycleTests.AppRunShutdownClean
        for suite in root.iter("test-suite"):
            for tc in list(suite):
                if tc.get("name") == "LifecycleTests.AppRunShutdownClean":
                    suite.remove(tc)

        # Add a brand-new scenario
        parent = root.find("test-suite")
        parent_elem = parent if parent is not None else root
        new_suite = ET.SubElement(parent_elem, "test-suite")
        new_suite.set("type", "TestFixture")
        new_suite.set("name", "NewFeatureTests")
        new_tc = ET.SubElement(new_suite, "test-case")
        new_tc.set("name", "NewFeatureTests.BrandNewScenario")
        new_tc.set("result", "Passed")
        new_tc.set("duration", "0.100")

        drift_path = tmp_path / "smoke-fork-drift.xml"
        tree.write(str(drift_path), encoding="unicode", xml_declaration=True)
        return drift_path

    def test_exit_code_one(self, tmp_path: Path) -> None:
        drift = self._make_drift_xml(tmp_path)
        code, _ = run_script(fork=str(drift))
        assert code == 1

    def test_verdict_drift(self, tmp_path: Path) -> None:
        drift = self._make_drift_xml(tmp_path)
        _, data = run_script(fork=str(drift))
        assert data.get("verdict") == "drift"

    def test_missing_scenario_listed(self, tmp_path: Path) -> None:
        drift = self._make_drift_xml(tmp_path)
        _, data = run_script(fork=str(drift))
        assert "LifecycleTests.AppRunShutdownClean" in data.get("missing_scenarios", [])

    def test_new_scenario_listed(self, tmp_path: Path) -> None:
        drift = self._make_drift_xml(tmp_path)
        _, data = run_script(fork=str(drift))
        assert "NewFeatureTests.BrandNewScenario" in data.get("new_scenarios", [])


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    def test_missing_upstream_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.xml"
        code, _ = run_script(upstream=str(missing))
        assert code == 2

    def test_missing_fork_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.xml"
        code, _ = run_script(fork=str(missing))
        assert code == 2

    def test_malformed_upstream_xml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.xml"
        bad.write_text("<this is not valid xml &&&", encoding="utf-8")
        code, _ = run_script(upstream=str(bad))
        assert code == 2

    def test_malformed_fork_xml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.xml"
        bad.write_text("<this is not valid xml &&&", encoding="utf-8")
        code, _ = run_script(fork=str(bad))
        assert code == 2


# ---------------------------------------------------------------------------
# Output file
# ---------------------------------------------------------------------------

class TestOutputFile:
    def test_output_file_written(self, tmp_path: Path) -> None:
        out = tmp_path / "result.json"
        run_script(extra_args=["--output", str(out)])
        assert out.exists()

    def test_output_file_has_required_keys(self, tmp_path: Path) -> None:
        out = tmp_path / "result.json"
        run_script(extra_args=["--output", str(out)])
        data = json.loads(out.read_text(encoding="utf-8"))
        required_keys = (
            "matched_scenarios", "regressions", "new_scenarios", "missing_scenarios", "verdict"
        )
        for key in required_keys:
            assert key in data, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# TRX format
# ---------------------------------------------------------------------------

class TestTrxFormat:
    """Verify TRX XML (Visual Studio test results) is parsed correctly."""

    def _make_trx(self, tmp_path: Path, name: str, outcome: str, pixel_hash: str = "") -> Path:
        ns = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"
        xml = textwrap.dedent(f"""\
            <?xml version="1.0" encoding="utf-8"?>
            <TestRun xmlns="{ns}">
              <Results>
                <UnitTestResult testName="{name}" outcome="{outcome}" duration="00:00:00.1234567">
                  <Output>
                    <StdOut>pixel-diff-hash: {pixel_hash}</StdOut>
                  </Output>
                </UnitTestResult>
              </Results>
            </TestRun>
        """)
        p = tmp_path / f"{name.replace('.', '_')}.trx"
        p.write_text(xml, encoding="utf-8")
        return p

    def test_trx_match(self, tmp_path: Path) -> None:
        up = self._make_trx(tmp_path, "MyTest.Scenario1", "Passed", "abc123")
        fk_path = tmp_path / "fork.trx"
        fk_path.write_text(up.read_text(), encoding="utf-8")
        code, data = run_script(upstream=str(up), fork=str(fk_path))
        assert code == 0
        assert data.get("verdict") == "match"

    def test_trx_pass_to_fail_regression(self, tmp_path: Path) -> None:
        ns = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"
        up_path = tmp_path / "up.trx"
        fk_path = tmp_path / "fk.trx"
        up_path.write_text(
            self._trx_xml(ns, "MyTest.Scenario1", "Passed", "abc123"),
            encoding="utf-8",
        )
        fk_path.write_text(
            self._trx_xml(ns, "MyTest.Scenario1", "Failed", "abc123"),
            encoding="utf-8",
        )
        code, data = run_script(upstream=str(up_path), fork=str(fk_path))
        assert code == 2
        assert data.get("verdict") == "regression"

    @staticmethod
    def _trx_xml(ns: str, name: str, outcome: str, pixel_hash: str) -> str:
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="utf-8"?>
            <TestRun xmlns="{ns}">
              <Results>
                <UnitTestResult testName="{name}" outcome="{outcome}" duration="00:00:00.1234567">
                  <Output>
                    <StdOut>pixel-diff-hash: {pixel_hash}</StdOut>
                  </Output>
                </UnitTestResult>
              </Results>
            </TestRun>
        """)


# ---------------------------------------------------------------------------
# Duration delta in regression details
# ---------------------------------------------------------------------------

class TestDurationDelta:
    """Duration delta should appear in regression details when durations differ."""

    def test_duration_delta_in_details(self, tmp_path: Path) -> None:
        # Build a minimal NUnit upstream + fork where one scenario goes pass->fail
        # and has differing durations
        up_xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <test-run>
              <test-suite type="TestFixture" name="Foo">
                <test-case name="Foo.Bar" result="Passed" duration="1.000" />
              </test-suite>
            </test-run>
        """)
        fk_xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <test-run>
              <test-suite type="TestFixture" name="Foo">
                <test-case name="Foo.Bar" result="Failed" duration="2.500" />
              </test-suite>
            </test-run>
        """)
        up_path = tmp_path / "up.xml"
        fk_path = tmp_path / "fk.xml"
        up_path.write_text(up_xml, encoding="utf-8")
        fk_path.write_text(fk_xml, encoding="utf-8")

        code, data = run_script(upstream=str(up_path), fork=str(fk_path))
        assert code == 2
        regressions = data.get("regressions", [])
        assert len(regressions) == 1
        details = regressions[0]["details"]
        # Duration delta should be mentioned
        assert "duration" in details.lower()
