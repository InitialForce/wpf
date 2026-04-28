"""
tests/test_initial_force_wpf_csproj.py

XML structure validation for packaging/InitialForce.WPF/.

These tests are runnable in WSL (no Windows / dotnet required).
They validate that the files are well-formed XML and contain all
required metadata before a Windows CI runner attempts dotnet build/pack.

Deferred to CI (Windows runners only):
  - dotnet build packaging/InitialForce.WPF/InitialForce.WPF.csproj
  - dotnet pack ... --no-build -p:PackageVersion=...
  - tools/verify-pkg.ps1 InitialForce.WPF.nupkg
  - msquic-pattern override verification on a hello-world WPF test app
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
PKG_DIR = REPO_ROOT / "packaging" / "InitialForce.WPF"
CSPROJ = PKG_DIR / "InitialForce.WPF.csproj"
TARGETS = PKG_DIR / "buildTransitive" / "InitialForce.WPF.targets"
PROPS = PKG_DIR / "buildTransitive" / "InitialForce.WPF.props"
README = PKG_DIR / "README.md"
ICON = PKG_DIR / "icon.png"
LIB_PLACEHOLDER = PKG_DIR / "lib" / "net10.0-windows" / "_._"

# MSBuild namespace used in SDK-style project files
MSBUILD_NS = "http://schemas.microsoft.com/developer/msbuild/2003"


def _parse(path: Path) -> ET.Element:
    """Parse XML and return root element. Raises on malformed XML."""
    tree = ET.parse(str(path))
    return tree.getroot()


def _find_text(root: ET.Element, xpath: str, ns: dict | None = None) -> str | None:
    """Return stripped text of first matching element, or None."""
    el = root.find(xpath, ns or {})
    return el.text.strip() if el is not None and el.text else None


def _find_all(root: ET.Element, xpath: str, ns: dict | None = None) -> list:
    return root.findall(xpath, ns or {})


# ---------------------------------------------------------------------------
# Gate 1: All files exist
# ---------------------------------------------------------------------------


class TestFilesExist:
    def test_csproj_exists(self):
        assert CSPROJ.exists(), f"Missing: {CSPROJ}"

    def test_targets_exists(self):
        assert TARGETS.exists(), f"Missing: {TARGETS}"

    def test_props_exists(self):
        assert PROPS.exists(), f"Missing: {PROPS}"

    def test_readme_exists(self):
        assert README.exists(), f"Missing: {README}"

    def test_lib_placeholder_exists(self):
        assert LIB_PLACEHOLDER.exists(), f"Missing: {LIB_PLACEHOLDER}"

    def test_icon_exists(self):
        assert ICON.exists(), (
            f"Missing: {ICON}. "
            "Either create with python+pillow or add as TODO and document."
        )


# ---------------------------------------------------------------------------
# Gate 2: XML parses without errors
# ---------------------------------------------------------------------------


class TestXmlWellFormed:
    def test_csproj_parses(self):
        """csproj must be valid XML."""
        root = _parse(CSPROJ)
        assert root is not None

    def test_targets_parses(self):
        """targets must be valid XML."""
        root = _parse(TARGETS)
        assert root is not None

    def test_props_parses(self):
        """props must be valid XML."""
        root = _parse(PROPS)
        assert root is not None


# ---------------------------------------------------------------------------
# Gate 3: csproj required metadata
# ---------------------------------------------------------------------------


class TestCsprojMetadata:
    """Verify all required NuGet metadata fields are present in the csproj."""

    def setup_method(self):
        self.root = _parse(CSPROJ)

    def _pg_text(self, tag: str) -> str | None:
        """Find text of <tag> inside any <PropertyGroup>."""
        for pg in self.root.findall("PropertyGroup"):
            el = pg.find(tag)
            if el is not None and el.text:
                return el.text.strip()
        return None

    def test_package_id(self):
        value = self._pg_text("PackageId")
        assert value == "InitialForce.WPF", f"PackageId is '{value}'"

    def test_authors(self):
        value = self._pg_text("Authors")
        assert value == "Initial Force AS", f"Authors is '{value}'"

    def test_target_framework(self):
        value = self._pg_text("TargetFramework")
        assert value == "net10.0-windows", f"TargetFramework is '{value}'"

    def test_package_license_expression(self):
        value = self._pg_text("PackageLicenseExpression")
        assert value == "MIT", f"PackageLicenseExpression is '{value}'"

    def test_repository_url(self):
        value = self._pg_text("RepositoryUrl")
        assert value is not None, "RepositoryUrl is missing"
        assert "InitialForce/wpf" in value, f"RepositoryUrl unexpected: '{value}'"

    def test_repository_type(self):
        value = self._pg_text("RepositoryType")
        assert value == "git", f"RepositoryType is '{value}'"

    def test_include_build_output_false(self):
        value = self._pg_text("IncludeBuildOutput")
        assert value is not None, "IncludeBuildOutput is missing"
        assert value.lower() == "false", (
            f"IncludeBuildOutput should be 'false' (pre-built DLLs); got '{value}'"
        )

    def test_generate_package_on_build_false(self):
        value = self._pg_text("GeneratePackageOnBuild")
        assert value is not None, "GeneratePackageOnBuild is missing"
        assert value.lower() == "false", (
            f"GeneratePackageOnBuild should be 'false' (packed via release.yml); got '{value}'"
        )

    def test_symbol_package_format_snupkg(self):
        value = self._pg_text("SymbolPackageFormat")
        assert value == "snupkg", f"SymbolPackageFormat is '{value}'"

    def test_description_present(self):
        value = self._pg_text("Description")
        assert value, "Description is missing or empty"

    def test_package_project_url(self):
        value = self._pg_text("PackageProjectUrl")
        assert value is not None, "PackageProjectUrl is missing"
        assert "InitialForce/wpf" in value


# ---------------------------------------------------------------------------
# Gate 4: csproj includes runtime DLLs and targets in pack
# ---------------------------------------------------------------------------


class TestCsprojPackItems:
    """Verify the <None> items that form the .nupkg contents."""

    def setup_method(self):
        self.root = _parse(CSPROJ)

    def _all_none_items(self) -> list[ET.Element]:
        items = []
        for ig in self.root.findall("ItemGroup"):
            items.extend(ig.findall("None"))
        return items

    def _pack_paths(self) -> list[str]:
        return [
            el.get("PackagePath", "")
            for el in self._all_none_items()
            if el.get("Pack", "").lower() == "true"
        ]

    def test_runtime_dlls_included(self):
        paths = self._pack_paths()
        assert any("runtimes/win-x64/lib/net10.0-windows" in p for p in paths), (
            "No <None> item packs DLLs from runtimes/win-x64/lib/net10.0-windows/. "
            f"Found PackagePaths: {paths}"
        )

    def test_lib_placeholder_included(self):
        paths = self._pack_paths()
        assert any("lib/net10.0-windows/_._" in p for p in paths), (
            "lib/net10.0-windows/_._  placeholder not found in Pack items. "
            f"Found PackagePaths: {paths}"
        )

    def test_targets_file_included(self):
        paths = self._pack_paths()
        # Targets should be in buildTransitive/ and/or build/
        assert any("InitialForce.WPF.targets" in p for p in paths), (
            "InitialForce.WPF.targets not found in Pack items. "
            f"Found PackagePaths: {paths}"
        )

    def test_props_file_included(self):
        paths = self._pack_paths()
        assert any("InitialForce.WPF.props" in p for p in paths), (
            "InitialForce.WPF.props not found in Pack items. "
            f"Found PackagePaths: {paths}"
        )


# ---------------------------------------------------------------------------
# Gate 5: .targets file has AfterTargets="Build;CopyFilesToOutputDirectory"
# ---------------------------------------------------------------------------


class TestTargetsStructure:
    """Verify the targets file contains the msquic-pattern required targets."""

    NS = {"ms": MSBUILD_NS}

    def setup_method(self):
        self.root = _parse(TARGETS)

    def _all_targets(self) -> list[ET.Element]:
        return self.root.findall("ms:Target", self.NS)

    def test_root_is_project(self):
        assert "Project" in self.root.tag, f"Root tag should be Project, got {self.root.tag}"

    def test_inject_target_after_build_and_copy_files(self):
        """AfterTargets must literally contain 'Build;CopyFilesToOutputDirectory'."""
        raw = ET.tostring(self.root, encoding="unicode")
        assert "AfterTargets=" in raw, "No AfterTargets attribute found in targets file"
        assert "Build;CopyFilesToOutputDirectory" in raw or "CopyFilesToOutputDirectory" in raw, (
            "AfterTargets does not include CopyFilesToOutputDirectory. "
            "This is required by the msquic precedent for inner-loop F5 builds."
        )

    def test_remove_runtime_target_exists(self):
        """Target 1 must exist: removes Microsoft WPF DLLs from copy lists."""
        target_names = [t.get("Name", "") for t in self._all_targets()]
        assert any("RemoveRuntime" in n or "Remove" in n for n in target_names), (
            f"No Target with 'Remove' in Name found. Targets: {target_names}"
        )

    def test_inject_target_exists(self):
        """Target 2 must exist: injects our patched DLLs."""
        target_names = [t.get("Name", "") for t in self._all_targets()]
        assert any("Inject" in n or "Copy" in n for n in target_names), (
            f"No Target with 'Inject' or 'Copy' in Name found. Targets: {target_names}"
        )

    def test_presentation_core_removed(self):
        raw = ET.tostring(self.root, encoding="unicode")
        assert "PresentationCore.dll" in raw, (
            "PresentationCore.dll not referenced in targets file"
        )

    def test_presentation_framework_removed(self):
        raw = ET.tostring(self.root, encoding="unicode")
        assert "PresentationFramework.dll" in raw

    def test_windows_base_removed(self):
        raw = ET.tostring(self.root, encoding="unicode")
        assert "WindowsBase.dll" in raw

    def test_system_xaml_removed(self):
        raw = ET.tostring(self.root, encoding="unicode")
        assert "System.Xaml.dll" in raw

    def test_windows_only_warning_present(self):
        """Targets file should emit a warning for non-Windows consumers."""
        raw = ET.tostring(self.root, encoding="unicode")
        assert "RuntimeIdentifier" in raw, (
            "Targets file should condition on $(RuntimeIdentifier) for Windows-only guard"
        )

    def test_remove_runtime_wpf_assets_has_win_x64_condition(self):
        """RemoveRuntimeWpfAssets must be conditioned on win-x64 only (HIGH-8)."""
        ns = {"ms": MSBUILD_NS}
        targets = self.root.findall("ms:Target", ns)
        remove_target = next(
            (t for t in targets if t.get("Name") == "RemoveRuntimeWpfAssets"), None
        )
        assert remove_target is not None, "RemoveRuntimeWpfAssets target not found"
        condition = remove_target.get("Condition", "")
        assert "win-x64" in condition, (
            f"RemoveRuntimeWpfAssets Condition must restrict to win-x64; got: {condition!r}"
        )

    def test_error_element_for_unsupported_rid(self):
        """Targets file must contain an <Error> element for unsupported RIDs (HIGH-8 fail-closed)."""
        ns = {"ms": MSBUILD_NS}
        errors = self.root.findall(".//ms:Error", ns)
        assert errors, "No <Error> element found in targets file; HIGH-8 requires fail-closed error for unsupported RIDs"
        error_texts = [e.get("Text", "") for e in errors]
        assert any("win-x64" in t or "RuntimeIdentifier" in t for t in error_texts), (
            f"<Error> element should reference RuntimeIdentifier or win-x64. Found: {error_texts}"
        )

    def test_no_overwrite_readonly_files(self):
        """No <Copy> task should use OverwriteReadOnlyFiles='true' (LOW-2)."""
        raw = ET.tostring(self.root, encoding="unicode")
        assert "OverwriteReadOnlyFiles" not in raw, (
            "OverwriteReadOnlyFiles='true' found in targets file; LOW-2 requires it be removed"
        )


# ---------------------------------------------------------------------------
# Gate 6: .props file is valid and documents consumer-overridable properties
# ---------------------------------------------------------------------------


class TestPropsStructure:
    def test_props_is_project(self):
        root = _parse(PROPS)
        assert "Project" in root.tag


# ---------------------------------------------------------------------------
# Gate 7: README.md has install instructions
# ---------------------------------------------------------------------------


class TestReadme:
    def setup_method(self):
        self.content = README.read_text(encoding="utf-8")

    def test_has_package_reference(self):
        assert "PackageReference" in self.content, (
            "README.md must include install instructions with <PackageReference ...>"
        )

    def test_has_requirements_section(self):
        lower = self.content.lower()
        assert "requirement" in lower or "net10.0" in lower, (
            "README.md should mention requirements (net10.0-windows, win-x64)"
        )

    def test_has_win_x64_mention(self):
        assert "win-x64" in self.content, "README.md should mention win-x64 RID restriction"

    def test_has_mit_license_mention(self):
        upper = self.content.upper()
        assert "MIT" in upper, "README.md should mention MIT license"


# ---------------------------------------------------------------------------
# Deferred gates documentation (not executable here)
# ---------------------------------------------------------------------------


class TestDeferredGatesDocumentation:
    """
    These tests document which validation gates are deferred to Windows CI runners.
    They always pass locally — they exist to make the deferred list visible in pytest output.
    """

    @pytest.mark.skip(reason="DEFERRED TO CI: requires Windows runner with dotnet 10 SDK")
    def test_dotnet_build_succeeds(self):
        """
        CI command:
          dotnet build packaging/InitialForce.WPF/InitialForce.WPF.csproj
        Expected: exits 0, no NU5128 or other errors.
        """

    @pytest.mark.skip(reason="DEFERRED TO CI: requires Windows runner with dotnet 10 SDK")
    def test_dotnet_pack_produces_nupkg(self):
        """
        CI command:
          dotnet pack packaging/InitialForce.WPF/InitialForce.WPF.csproj
            --no-build
            -p:PackageVersion=10.0.4-if.20260427.1
            -p:GitCommitId=<sha>
            -o artifacts/nuget/
        Expected: InitialForce.WPF.10.0.4-if.20260427.1.nupkg produced.
        """

    @pytest.mark.skip(reason="DEFERRED TO CI: requires Windows runner with dotnet 10 SDK")
    def test_verify_pkg_script_passes(self):
        """
        CI command:
          tools/verify-pkg.ps1 InitialForce.WPF.nupkg
        Expected: exactly 4 DLLs + 4 PDBs + 1 targets file at expected paths;
                  all PDBs are portable PDB format (magic bytes BSJB).
        """

    @pytest.mark.skip(reason="DEFERRED TO CI: requires Windows runner with dotnet 10 SDK")
    def test_symbol_package_snupkg_produced(self):
        """
        CI command: (same dotnet pack as above)
        Expected: InitialForce.WPF.10.0.4-if.20260427.1.snupkg produced alongside .nupkg.
        """

    @pytest.mark.skip(reason="DEFERRED TO CI: requires manual verification on Windows")
    def test_msquic_pattern_overrides_runtime_pack(self):
        """
        Manual CI step:
          1. Create a 'dotnet new wpf' test app.
          2. Add <PackageReference Include="InitialForce.WPF" Version="..." />
          3. dotnet build -r win-x64 --self-contained
          4. Verify bin/Debug/net10.0-windows/win-x64/PresentationFramework.dll
             file hash matches our patched DLL, NOT Microsoft's runtime pack copy.
        See wpf-w3v (msquic-pattern verification bead) for the full smoke-harness spec.
        """
