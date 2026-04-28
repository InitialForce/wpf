"""
test_runtime_override_csproj.py
================================
Validation gates for packaging/InitialForce.WPF.RuntimeOverride/.

Tests verify XML well-formedness, required metadata, and MSBuild logic in the
targets/props files. dotnet pack and runtime override behavior are deferred to CI.
"""
import pathlib
import xml.etree.ElementTree as ET

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
PKG_DIR = REPO_ROOT / "packaging" / "InitialForce.WPF.RuntimeOverride"
CSPROJ = PKG_DIR / "InitialForce.WPF.RuntimeOverride.csproj"
TARGETS = PKG_DIR / "buildTransitive" / "InitialForce.WPF.RuntimeOverride.targets"
PROPS = PKG_DIR / "buildTransitive" / "InitialForce.WPF.RuntimeOverride.props"
README = PKG_DIR / "README.md"

MSBUILD_NS = "http://schemas.microsoft.com/developer/msbuild/2003"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_xml(path: pathlib.Path) -> ET.Element:
    """Parse an XML file and return the root element."""
    tree = ET.parse(path)
    return tree.getroot()


def csproj_value(root: ET.Element, tag: str) -> str:
    """Return the text of the first matching tag in a csproj PropertyGroup."""
    for elem in root.iter(tag):
        return (elem.text or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Gate 1: Files exist
# ---------------------------------------------------------------------------

class TestFilesExist:
    def test_csproj_exists(self):
        assert CSPROJ.exists(), f"csproj not found: {CSPROJ}"

    def test_targets_exists(self):
        assert TARGETS.exists(), f"targets not found: {TARGETS}"

    def test_props_exists(self):
        assert PROPS.exists(), f"props not found: {PROPS}"

    def test_readme_exists(self):
        assert README.exists(), f"README.md not found: {README}"

    def test_placeholder_lib_exists(self):
        placeholder = PKG_DIR / "lib" / "net10.0-windows" / "_._"
        assert placeholder.exists(), f"lib placeholder not found: {placeholder}"


# ---------------------------------------------------------------------------
# Gate 2: csproj — correct PackageId, Authors, LicenseExpression
# ---------------------------------------------------------------------------

class TestCsprojMetadata:
    @pytest.fixture(scope="class")
    def root(self):
        return parse_xml(CSPROJ)

    def test_xml_parses(self):
        """csproj must be well-formed XML."""
        parse_xml(CSPROJ)  # raises on malformed XML

    def test_package_id(self, root):
        assert csproj_value(root, "PackageId") == "InitialForce.WPF.RuntimeOverride"

    def test_authors(self, root):
        assert csproj_value(root, "Authors") == "Initial Force AS"

    def test_license_expression(self, root):
        assert csproj_value(root, "PackageLicenseExpression") == "MIT"

    def test_target_framework(self, root):
        tf = csproj_value(root, "TargetFramework")
        assert tf == "net10.0-windows", f"Expected net10.0-windows, got {tf!r}"

    def test_include_build_output_false(self, root):
        val = csproj_value(root, "IncludeBuildOutput")
        assert val.lower() == "false"

    def test_no_build(self, root):
        val = csproj_value(root, "NoBuild")
        assert val.lower() == "true"

    def test_buildtransitive_targets_packed(self, root):
        """Verify the .targets file is included with PackagePath buildTransitive/."""
        content = CSPROJ.read_text(encoding="utf-8")
        assert "buildTransitive/InitialForce.WPF.RuntimeOverride.targets" in content

    def test_buildtransitive_props_packed(self, root):
        """Verify the .props file is included with PackagePath buildTransitive/."""
        content = CSPROJ.read_text(encoding="utf-8")
        assert "buildTransitive/InitialForce.WPF.RuntimeOverride.props" in content


# ---------------------------------------------------------------------------
# Gate 3: .targets — IF_OverrideAssemblies property + conditional AfterTargets
# ---------------------------------------------------------------------------

class TestTargetsFile:
    @pytest.fixture(scope="class")
    def root(self):
        return parse_xml(TARGETS)

    def test_xml_parses(self):
        """targets must be well-formed XML."""
        parse_xml(TARGETS)

    def test_if_override_assemblies_referenced(self):
        """IF_OverrideAssemblies must be used in Condition attributes on targets."""
        content = TARGETS.read_text(encoding="utf-8")
        assert "IF_OverrideAssemblies" in content, (
            "IF_OverrideAssemblies is not referenced in targets file"
        )

    def test_after_targets_build_copy(self):
        """Injection targets must use AfterTargets containing Build and Copy."""
        content = TARGETS.read_text(encoding="utf-8")
        assert "AfterTargets" in content
        assert "Build" in content
        assert "CopyFilesToOutputDirectory" in content

    def test_win_rid_condition(self):
        """Targets must be conditioned on win-x64 RuntimeIdentifier
        (HIGH-8: exact RID, not win-*)."""
        content = TARGETS.read_text(encoding="utf-8")
        assert "win-x64" in content, "No win-x64 RuntimeIdentifier condition found in targets"
        # Must NOT use the broad win-* / StartsWith('win-') pattern for Remove/Inject targets
        assert "StartsWith('win-')" not in content, (
            "Targets must condition on 'win-x64' exactly, not StartsWith('win-') — HIGH-8 fix"
        )

    def test_presentation_core_target_present(self):
        content = TARGETS.read_text(encoding="utf-8")
        assert "PresentationCore" in content

    def test_presentation_framework_target_present(self):
        content = TARGETS.read_text(encoding="utf-8")
        assert "PresentationFramework" in content

    def test_windows_base_target_present(self):
        content = TARGETS.read_text(encoding="utf-8")
        assert "WindowsBase" in content

    def test_system_xaml_target_present(self):
        content = TARGETS.read_text(encoding="utf-8")
        assert "System.Xaml" in content

    def test_remove_runtime_pack_asset(self):
        """Must strip RuntimePackAsset for listed assemblies."""
        content = TARGETS.read_text(encoding="utf-8")
        assert "RuntimePackAsset" in content

    def test_copy_task_present(self):
        """Injection target must use <Copy> task to place DLLs in OutDir."""
        content = TARGETS.read_text(encoding="utf-8")
        assert "<Copy " in content or "<Copy\n" in content

    def test_resolved_file_to_publish_present(self):
        """Must add entries to ResolvedFileToPublish for publish scenarios."""
        content = TARGETS.read_text(encoding="utf-8")
        assert "ResolvedFileToPublish" in content

    def test_remove_and_inject_targets_count(self, root):
        """There should be at least 12 targets
        (3 per assembly: Remove + Warn + Inject = 12 minimum)."""
        ns = {"ms": MSBUILD_NS}
        targets = root.findall(".//ms:Target", ns)
        # Fall back to no-namespace search
        if not targets:
            targets = list(root.iter("Target"))
        assert len(targets) >= 12, (
            f"Expected at least 12 Target elements (3 per assembly: Remove+Warn+Inject), "
            f"found {len(targets)}"
        )

    def test_warning_element_for_unsupported_rid(self):
        """Targets file must contain <Warning> elements for unsupported RIDs
        (HIGH-8 graceful degrade)."""
        ns = {"ms": MSBUILD_NS}
        root = parse_xml(TARGETS)
        warnings = root.findall(".//ms:Warning", ns)
        assert warnings, "No <Warning> element found; HIGH-8 requires warnings for unsupported RIDs"
        warn_texts = [w.get("Text", "") for w in warnings]
        assert any("no-op" in t or "RuntimeIdentifier" in t or "will not be overridden" in t
                   for t in warn_texts), (
            f"<Warning> text should explain the no-op behaviour. Found: {warn_texts}"
        )

    def test_no_overwrite_readonly_files(self):
        """No <Copy> task should use OverwriteReadOnlyFiles='true' (LOW-2)."""
        content = TARGETS.read_text(encoding="utf-8")
        assert "OverwriteReadOnlyFiles" not in content, (
            "OverwriteReadOnlyFiles='true' found in RuntimeOverride targets file; "
            "LOW-2 requires it be removed"
        )


# ---------------------------------------------------------------------------
# Gate 4: .props — IF_OverrideAssemblies default property defined
# ---------------------------------------------------------------------------

class TestPropsFile:
    @pytest.fixture(scope="class")
    def root(self):
        return parse_xml(PROPS)

    def test_xml_parses(self):
        """props must be well-formed XML."""
        parse_xml(PROPS)

    def test_if_override_assemblies_declared(self):
        """IF_OverrideAssemblies must be declared as a property in props."""
        content = PROPS.read_text(encoding="utf-8")
        assert "IF_OverrideAssemblies" in content

    def test_runtime_dir_property_declared(self):
        """_IFWpfRORuntimeDir must point to runtimes/win-x64/lib/net10.0-windows/."""
        content = PROPS.read_text(encoding="utf-8")
        assert "_IFWpfRORuntimeDir" in content
        assert "runtimes" in content
        assert "win-x64" in content
        assert "net10.0-windows" in content


# ---------------------------------------------------------------------------
# Gate 5: README explains RuntimeOverride vs full InitialForce.WPF choice
# ---------------------------------------------------------------------------

class TestReadme:
    @pytest.fixture(scope="class")
    def readme_text(self):
        return README.read_text(encoding="utf-8")

    def test_readme_mentions_initialforce_wpf(self, readme_text):
        """README must reference the full InitialForce.WPF package by name."""
        assert "InitialForce.WPF" in readme_text

    def test_readme_explains_when_to_use_override(self, readme_text):
        """README must explain when to use RuntimeOverride."""
        # Should contain some guidance on fallback / edge cases / framework-dependent
        keywords = ["fallback", "framework-dependent", "edge case", "subset", "only"]
        matched = [kw for kw in keywords if kw.lower() in readme_text.lower()]
        assert matched, (
            "README should explain when to use RuntimeOverride vs InitialForce.WPF; "
            f"none of the expected keywords {keywords} found"
        )

    def test_readme_mentions_if_override_assemblies(self, readme_text):
        """README must document the IF_OverrideAssemblies property."""
        assert "IF_OverrideAssemblies" in readme_text

    def test_readme_lists_supported_assemblies(self, readme_text):
        """README must list all four supported assembly names."""
        for asm in ("PresentationCore", "PresentationFramework", "WindowsBase", "System.Xaml"):
            assert asm in readme_text, f"README does not mention assembly: {asm}"

    def test_readme_mentions_mit_license(self, readme_text):
        assert "MIT" in readme_text

    def test_readme_mentions_win_x64_limitation(self, readme_text):
        """README should note the win-x64 / RID limitation."""
        assert "win-x64" in readme_text or "x64" in readme_text
