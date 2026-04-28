"""Tests for tools/check-denylist.py.

Each test exercises check_files() or the full CLI (via subprocess) to ensure
correct glob matching, exit codes, and JSON output shapes.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Helpers to load the module under test directly
# ---------------------------------------------------------------------------

TOOLS_DIR = Path(__file__).parent.parent / "tools"
CHECK_DENYLIST = TOOLS_DIR / "check-denylist.py"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_CONFIG = FIXTURES_DIR / "sample-denylist-config.yaml"


def _load_patterns() -> list[str]:
    """Return the file_denylist patterns from the sample fixture."""
    with SAMPLE_CONFIG.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return list(data["file_denylist"])


def _run_cli(
    *extra_args: str,
    changed_files_content: str | None = None,
    config_path: Path | None = None,
) -> tuple[int, dict]:  # type: ignore[type-arg]
    """Run check-denylist.py as a subprocess, returning (exit_code, parsed_json)."""
    if config_path is None:
        config_path = SAMPLE_CONFIG

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp_files:
        if changed_files_content is not None:
            tmp_files.write(changed_files_content)
        changed_path = Path(tmp_files.name)

    try:
        cmd = [
            sys.executable,
            str(CHECK_DENYLIST),
            "--config",
            str(config_path),
            "--changed-files",
            str(changed_path),
            *extra_args,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        return result.returncode, payload
    finally:
        changed_path.unlink(missing_ok=True)


# ===========================================================================
# Unit-level tests using check_files() directly (imported via importlib)
# ===========================================================================


def _import_module():  # type: ignore[return]
    spec = importlib.util.spec_from_file_location("check_denylist", str(CHECK_DENYLIST))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


MODULE = _import_module()
check_files = MODULE.check_files
_matches_pattern = MODULE._matches_pattern
load_config = MODULE.load_config


# ---------------------------------------------------------------------------
# Test 1: clean file list produces no violations
# ---------------------------------------------------------------------------


def test_clean_files_no_violations() -> None:
    patterns = _load_patterns()
    files = [
        "src/Microsoft.DotNet.Wpf/src/PresentationCore/System/Windows/Media/Brush.cs",
        "src/Microsoft.DotNet.Wpf/src/WindowsBase/System/ComponentModel/DependencyPropertyDescriptor.cs",
        "tests/SomeTest.cs",
    ]
    violations = check_files(files, patterns)
    assert violations == []


# ---------------------------------------------------------------------------
# Test 2: exact supply-chain file triggers violation
# ---------------------------------------------------------------------------


def test_exact_match_nuget_config() -> None:
    patterns = _load_patterns()
    violations = check_files(["NuGet.config"], patterns)
    assert len(violations) == 1
    assert violations[0]["file"] == "NuGet.config"
    assert violations[0]["matched_pattern"] == "NuGet.config"


# ---------------------------------------------------------------------------
# Test 3: eng/ glob matches deeply nested paths
# ---------------------------------------------------------------------------


def test_eng_glob_matches_nested() -> None:
    """'eng/Signing.props' exact pattern should match that exact file."""
    patterns = _load_patterns()
    violations = check_files(["eng/Signing.props"], patterns)
    assert len(violations) == 1
    assert violations[0]["matched_pattern"] == "eng/Signing.props"


# ---------------------------------------------------------------------------
# Test 4: .github/** glob matches any path under .github/
# ---------------------------------------------------------------------------


def test_github_glob_matches_workflow() -> None:
    patterns = _load_patterns()
    violations = check_files([".github/workflows/pr-ingestion.yml"], patterns)
    assert len(violations) == 1
    assert violations[0]["matched_pattern"] == ".github/**"


def test_github_glob_matches_codeowners() -> None:
    patterns = _load_patterns()
    violations = check_files([".github/CODEOWNERS"], patterns)
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# Test 5: src/.../WpfGfx/** matches files deep in WpfGfx
# ---------------------------------------------------------------------------


def test_wpfgfx_glob_deep() -> None:
    patterns = _load_patterns()
    file = "src/Microsoft.DotNet.Wpf/src/WpfGfx/core/hw/d3dutils.cpp"
    violations = check_files([file], patterns)
    assert len(violations) == 1
    assert "WpfGfx" in violations[0]["matched_pattern"]


# ---------------------------------------------------------------------------
# Test 6: src/.../Primitives/Popup.cs exact match
# ---------------------------------------------------------------------------


def test_primitives_popup_exact() -> None:
    patterns = _load_patterns()
    file = (
        "src/Microsoft.DotNet.Wpf/src/PresentationFramework/"
        "System/Windows/Controls/Primitives/Popup.cs"
    )
    violations = check_files([file], patterns)
    assert len(violations) == 1
    assert "Popup.cs" in violations[0]["matched_pattern"]


# ---------------------------------------------------------------------------
# Test 7: .if-fork/prompts/** glob matches nested prompt file
# ---------------------------------------------------------------------------


def test_if_fork_prompts_glob() -> None:
    patterns = _load_patterns()
    violations = check_files([".if-fork/prompts/cherry-pick.md"], patterns)
    assert len(violations) == 1
    assert violations[0]["matched_pattern"] == ".if-fork/prompts/**"


# ---------------------------------------------------------------------------
# Test 8: Imaging/** glob matches deep imaging path
# ---------------------------------------------------------------------------


def test_imaging_glob() -> None:
    patterns = _load_patterns()
    file = (
        "src/Microsoft.DotNet.Wpf/src/PresentationCore/"
        "System/Windows/Media/Imaging/BitmapImage.cs"
    )
    violations = check_files([file], patterns)
    assert len(violations) == 1
    assert "Imaging" in violations[0]["matched_pattern"]


# ---------------------------------------------------------------------------
# Test 9: mixed list — clean files alongside a denied file
# ---------------------------------------------------------------------------


def test_mixed_list_reports_only_violations() -> None:
    patterns = _load_patterns()
    files = [
        "src/Microsoft.DotNet.Wpf/src/PresentationCore/System/Windows/Media/Brush.cs",  # clean
        "global.json",  # denied
        "tests/SomeTest.cs",  # clean
    ]
    violations = check_files(files, patterns)
    assert len(violations) == 1
    assert violations[0]["file"] == "global.json"


# ---------------------------------------------------------------------------
# Test 10: PenImc/** glob
# ---------------------------------------------------------------------------


def test_penimc_glob() -> None:
    patterns = _load_patterns()
    file = "src/Microsoft.DotNet.Wpf/src/PenImc/dll/PenImcRc.rc"
    violations = check_files([file], patterns)
    assert len(violations) == 1
    assert "PenImc" in violations[0]["matched_pattern"]


# ===========================================================================
# CLI integration tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Test 11: CLI exit 0 + verdict ok for clean files
# ---------------------------------------------------------------------------


def test_cli_clean_exit_0() -> None:
    code, payload = _run_cli(
        changed_files_content="src/Microsoft.DotNet.Wpf/src/PresentationCore/Brush.cs\n"
    )
    assert code == 0
    assert payload["verdict"] == "ok"
    assert payload["checked_count"] == 1


# ---------------------------------------------------------------------------
# Test 12: CLI exit 2 + verdict refused for denied file
# ---------------------------------------------------------------------------


def test_cli_denied_exit_2() -> None:
    code, payload = _run_cli(changed_files_content="NuGet.config\n")
    assert code == 2
    assert payload["verdict"] == "refused"
    assert len(payload["violations"]) >= 1
    assert payload["violations"][0]["file"] == "NuGet.config"


# ---------------------------------------------------------------------------
# Test 13: CLI --output writes JSON file
# ---------------------------------------------------------------------------


def test_cli_output_flag() -> None:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp_files:
        tmp_files.write("global.json\n")
        files_path = Path(tmp_files.name)

    try:
        cmd = [
            sys.executable,
            str(CHECK_DENYLIST),
            "--config",
            str(SAMPLE_CONFIG),
            "--changed-files",
            str(files_path),
            "--output",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 2
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["verdict"] == "refused"
    finally:
        out_path.unlink(missing_ok=True)
        files_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 14: empty changed-files list exits 0 with checked_count=0
# ---------------------------------------------------------------------------


def test_cli_empty_changed_files() -> None:
    code, payload = _run_cli(changed_files_content="")
    assert code == 0
    assert payload["verdict"] == "ok"
    assert payload["checked_count"] == 0


# ---------------------------------------------------------------------------
# Test 15: missing config exits 1 (error)
# ---------------------------------------------------------------------------


def test_cli_missing_config_exits_1() -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp_files:
        tmp_files.write("some-file.cs\n")
        files_path = Path(tmp_files.name)

    try:
        cmd = [
            sys.executable,
            str(CHECK_DENYLIST),
            "--config",
            "/nonexistent/path/config.yaml",
            "--changed-files",
            str(files_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 1
    finally:
        files_path.unlink(missing_ok=True)
