"""
test_smoke_harness_structure.py
================================
Python-side structural validator for test/InitialForce.WpfSmoke/.

Validates without running dotnet:
  - All 22 scenario .cs files exist
  - Each scenario file contains at least one [Test] method
  - The scenario method names match the canonical spec table
  - SmokeBase.cs exists and contains [OneTimeSetUp]
  - The main csproj exists and is valid XML
  - The perf harness csproj exists and references BenchmarkDotNet
  - README.md exists and is >500 chars
  - Goldens/.gitkeep exists

Run with:
  pytest tests/test_smoke_harness_structure.py -v
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT   = Path(__file__).resolve().parent.parent
SMOKE_DIR   = REPO_ROOT / "test" / "InitialForce.WpfSmoke"
SMOKE_CS    = SMOKE_DIR / "Smoke"
PERF_DIR    = SMOKE_DIR / "Perf"
GOLDENS_DIR = SMOKE_DIR / "Goldens"

# ---------------------------------------------------------------------------
# Canonical scenario table (22 scenarios)
# Derived verbatim from exec-docs/40-packaging-and-tests.md §3.2
# ---------------------------------------------------------------------------

SCENARIOS: list[dict] = [
    {"id": "SMOKE-001", "class": "GeometryParserTests",      "method": "RoundTrip10kPaths"},
    {"id": "SMOKE-002", "class": "GeometryParserBench",      "method": "ReadNumberBench"},
    {"id": "SMOKE-003", "class": "ListCollectionViewTests",  "method": "SortOf50kItems"},
    {"id": "SMOKE-004", "class": "ListCollectionViewTests",  "method": "PrepareComparerZeroAllocs"},
    {"id": "SMOKE-005", "class": "FrugalListTests",          "method": "InsertRemoveRoundTrip"},
    {"id": "SMOKE-006", "class": "FrugalListTests",          "method": "GenericIntNoBoxing"},
    {"id": "SMOKE-007", "class": "WeakReferenceListTests",   "method": "EnumeratorNotBoxed"},
    {"id": "SMOKE-008", "class": "VirtualizingPanelTests",   "method": "Only30ContainersRealized"},
    {"id": "SMOKE-009", "class": "PixelDiffTests",           "method": "XamlSceneA"},
    {"id": "SMOKE-010", "class": "PixelDiffTests",           "method": "DataGrid5Rows"},
    {"id": "SMOKE-011", "class": "PixelDiffTests",           "method": "FlowDocument"},
    {"id": "SMOKE-012", "class": "PixelDiffTests",           "method": "RtlText"},
    {"id": "SMOKE-013", "class": "HitTestingTests",          "method": "ThreeRectanglesNinePoints"},
    {"id": "SMOKE-014", "class": "ImageLoadingTests",        "method": "DecodeAllFormats"},
    {"id": "SMOKE-015", "class": "ImageLoadingBench",        "method": "JpegDecode100"},
    {"id": "SMOKE-016", "class": "DataBindingTests",
     "method": "ItemsControlUpdatesOnChange"},
    {"id": "SMOKE-017", "class": "DataBindingTests",
     "method": "MultiBindingConverterChain"},
    {"id": "SMOKE-018", "class": "AnimationTests",
     "method": "DoubleAnimationReachesTarget"},
    {"id": "SMOKE-019", "class": "StyleTests",
     "method": "ResourceDictionaryAllStylesResolve"},
    {"id": "SMOKE-020", "class": "SortTests",                "method": "ArrayListSortGenericPath"},
    {"id": "SMOKE-021", "class": "PresentationSourceTests",  "method": "NoLeakAfter100Windows"},
    {"id": "SMOKE-022", "class": "LifecycleTests",           "method": "AppRunShutdownClean"},
]

assert len(SCENARIOS) == 22, f"Expected 22 scenarios, got {len(SCENARIOS)}"


# ---------------------------------------------------------------------------
# Helper: collect all .cs source text under SMOKE_CS and PERF_DIR
# ---------------------------------------------------------------------------

def _read_cs(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_cs_source() -> str:
    """Returns concatenated text of all .cs files in the harness."""
    parts: list[str] = []
    for cs_file in (SMOKE_CS.glob("*.cs") if SMOKE_CS.exists() else []):
        parts.append(_read_cs(cs_file))
    for cs_file in (PERF_DIR.glob("*.cs") if PERF_DIR.exists() else []):
        parts.append(_read_cs(cs_file))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Gate 1: Main csproj exists and is valid XML
# ---------------------------------------------------------------------------

class TestMainCsproj:
    CSPROJ = SMOKE_DIR / "InitialForce.WpfSmoke.csproj"

    def test_csproj_exists(self) -> None:
        assert self.CSPROJ.exists(), f"Missing: {self.CSPROJ}"

    def test_csproj_is_valid_xml(self) -> None:
        tree = ET.parse(str(self.CSPROJ))
        root = tree.getroot()
        assert root is not None, "csproj XML root is None"

    def test_csproj_targets_net10_windows(self) -> None:
        content = self.CSPROJ.read_text(encoding="utf-8")
        assert "net10.0-windows" in content, \
            "csproj does not target net10.0-windows"

    def test_csproj_references_nunit(self) -> None:
        content = self.CSPROJ.read_text(encoding="utf-8")
        assert "NUnit" in content, "csproj does not reference NUnit"

    def test_csproj_references_benchmarkdotnet(self) -> None:
        content = self.CSPROJ.read_text(encoding="utf-8")
        assert "BenchmarkDotNet" in content, \
            "csproj does not reference BenchmarkDotNet"


# ---------------------------------------------------------------------------
# Gate 2: All 22 scenario methods exist in the Smoke/ C# source
# ---------------------------------------------------------------------------

class TestScenarioMethods:
    """Parametrized test — one assertion per scenario."""

    @pytest.fixture(scope="class")
    def all_source(self) -> str:
        return _all_cs_source()

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
    def test_scenario_class_exists(self, scenario: dict, all_source: str) -> None:
        class_name = scenario["class"]
        # Check for "class ClassName" in source.
        assert re.search(rf"\bclass\s+{re.escape(class_name)}\b", all_source), \
            f"Class '{class_name}' not found in Smoke/*.cs or Perf/*.cs"

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
    def test_scenario_method_exists(self, scenario: dict, all_source: str) -> None:
        method_name = scenario["method"]
        # Check for "void MethodName", "int MethodName", or "[Benchmark...] public * MethodName"
        m = re.escape(method_name)
        pattern = rf"\b(public\s+\w+\s+{m}\s*\(|{m}\s*\(\s*\))"
        assert re.search(pattern, all_source), \
            f"Method '{method_name}' (scenario {scenario['id']}) not found in source files."

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
    def test_scenario_has_test_or_benchmark_attribute(
        self, scenario: dict, all_source: str
    ) -> None:
        method_name = scenario["method"]
        # Look for [Test] or [Benchmark] within a few lines before the method name.
        # We use a multiline search for "[Test]" or "[Benchmark" followed by method name.
        pattern = rf"(\[Test\]|\[Benchmark)[\s\S]{{0,300}}{re.escape(method_name)}\s*\("
        assert re.search(pattern, all_source), \
            (f"Method '{method_name}' (scenario {scenario['id']}) does not appear to be "
             f"preceded by [Test] or [Benchmark] attribute within 300 chars.")


# ---------------------------------------------------------------------------
# Gate 3: SmokeBase.cs exists and has [OneTimeSetUp]
# ---------------------------------------------------------------------------

class TestSmokeBase:
    SMOKE_BASE = SMOKE_CS / "SmokeBase.cs"

    def test_smoke_base_exists(self) -> None:
        assert self.SMOKE_BASE.exists(), f"Missing: {self.SMOKE_BASE}"

    def test_smoke_base_has_onetimesetup(self) -> None:
        content = self.SMOKE_BASE.read_text(encoding="utf-8")
        assert "[OneTimeSetUp]" in content, \
            "SmokeBase.cs does not contain [OneTimeSetUp]"

    def test_smoke_base_has_testfixture(self) -> None:
        content = self.SMOKE_BASE.read_text(encoding="utf-8")
        assert "[TestFixture]" in content, \
            "SmokeBase.cs does not contain [TestFixture]"


# ---------------------------------------------------------------------------
# Gate 4: Perf harness csproj exists and references BenchmarkDotNet
# ---------------------------------------------------------------------------

class TestPerfCsproj:
    PERF_CSPROJ = PERF_DIR / "InitialForce.WpfPerf.csproj"

    def test_perf_csproj_exists(self) -> None:
        assert self.PERF_CSPROJ.exists(), f"Missing: {self.PERF_CSPROJ}"

    def test_perf_csproj_is_valid_xml(self) -> None:
        tree = ET.parse(str(self.PERF_CSPROJ))
        root = tree.getroot()
        assert root is not None

    def test_perf_csproj_references_benchmarkdotnet(self) -> None:
        content = self.PERF_CSPROJ.read_text(encoding="utf-8")
        assert "BenchmarkDotNet" in content, \
            "Perf csproj does not reference BenchmarkDotNet"

    def test_perf_csproj_targets_net10_windows(self) -> None:
        content = self.PERF_CSPROJ.read_text(encoding="utf-8")
        assert "net10.0-windows" in content


# ---------------------------------------------------------------------------
# Gate 5: PerfHarness.cs exists in Perf/
# ---------------------------------------------------------------------------

class TestPerfHarness:
    PERF_HARNESS = PERF_DIR / "PerfHarness.cs"

    def test_perf_harness_exists(self) -> None:
        assert self.PERF_HARNESS.exists(), f"Missing: {self.PERF_HARNESS}"

    def test_perf_harness_has_benchmark_attribute(self) -> None:
        content = self.PERF_HARNESS.read_text(encoding="utf-8")
        assert "[Benchmark" in content, \
            "PerfHarness.cs does not contain any [Benchmark] attributes"

    def test_perf_harness_has_benchmark_config(self) -> None:
        content = self.PERF_HARNESS.read_text(encoding="utf-8")
        assert "BenchmarkConfig" in content, \
            "PerfHarness.cs does not reference BenchmarkConfig"


# ---------------------------------------------------------------------------
# Gate 6: README.md exists and is >500 characters
# ---------------------------------------------------------------------------

class TestReadme:
    README = SMOKE_DIR / "README.md"

    def test_readme_exists(self) -> None:
        assert self.README.exists(), f"Missing: {self.README}"

    def test_readme_length(self) -> None:
        length = len(self.README.read_text(encoding="utf-8"))
        assert length > 500, \
            f"README.md is only {length} chars (must be >500)"

    def test_readme_mentions_dotnet_test(self) -> None:
        content = self.README.read_text(encoding="utf-8")
        assert "dotnet test" in content, \
            "README.md does not mention 'dotnet test'"

    def test_readme_contains_scenario_table(self) -> None:
        content = self.README.read_text(encoding="utf-8")
        # Spot-check a few scenario IDs are present in the README table.
        for smoke_id in ("SMOKE-001", "SMOKE-011", "SMOKE-022"):
            assert smoke_id in content, \
                f"README.md does not mention {smoke_id}"


# ---------------------------------------------------------------------------
# Gate 7: Goldens/.gitkeep placeholder exists
# ---------------------------------------------------------------------------

class TestGoldens:
    GITKEEP = GOLDENS_DIR / ".gitkeep"

    def test_goldens_dir_exists(self) -> None:
        assert GOLDENS_DIR.exists(), f"Goldens directory missing: {GOLDENS_DIR}"

    def test_gitkeep_exists(self) -> None:
        assert self.GITKEEP.exists(), f"Missing: {self.GITKEEP}"


# ---------------------------------------------------------------------------
# Gate 8: Naming convention — each scenario file uses class name from spec
# ---------------------------------------------------------------------------

class TestNamingConventions:
    """Each class from the spec table must appear in the source."""

    @pytest.fixture(scope="class")
    def all_source(self) -> str:
        return _all_cs_source()

    def test_twenty_two_scenarios_total(self) -> None:
        assert len(SCENARIOS) == 22, \
            f"Expected exactly 22 scenarios in spec table, got {len(SCENARIOS)}"

    def test_scenario_ids_unique(self) -> None:
        ids = [s["id"] for s in SCENARIOS]
        assert len(ids) == len(set(ids)), \
            f"Duplicate scenario IDs: {[x for x in ids if ids.count(x) > 1]}"

    def test_scenario_ids_sequential(self) -> None:
        for i, scenario in enumerate(SCENARIOS, start=1):
            expected_id = f"SMOKE-{i:03d}"
            assert scenario["id"] == expected_id, \
                f"Scenario {i} has ID '{scenario['id']}', expected '{expected_id}'"

    def test_all_smoke_cs_files_in_smoke_dir(self) -> None:
        if not SMOKE_CS.exists():
            pytest.skip("Smoke/ directory does not exist")
        cs_files = list(SMOKE_CS.glob("*.cs"))
        assert len(cs_files) >= 1, "No .cs files found in Smoke/"

    def test_pixel_diff_helper_exists(self) -> None:
        assert (SMOKE_CS / "PixelDiffHelper.cs").exists(), \
            "PixelDiffHelper.cs missing from Smoke/"

    def test_svg_path_generator_exists(self) -> None:
        assert (SMOKE_CS / "SvgPathGenerator.cs").exists(), \
            "SvgPathGenerator.cs missing from Smoke/"
