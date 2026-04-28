"""Tests for tools/check-graduated.py.

Covers:
  - graduated: all hunks present                     (test_graduated_*)
  - not-graduated: no hunks present                  (test_not_graduated_*)
  - partial: some hunks present, some missing        (test_partial_*)
  - whitespace tolerance                             (test_whitespace_*)
  - multi-file patches                               (test_multifile_*)
  - empty/degenerate inputs                          (test_edge_*)
  - stdin reading                                    (test_stdin_*)
  - JSON output                                      (test_output_json_*)
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

# ---------------------------------------------------------------------------
# Import the module under test so we can call its functions directly.
# The filename uses a hyphen, so we must load it via importlib.
# ---------------------------------------------------------------------------
_SCRIPT_PATH = Path(__file__).parent.parent / "tools" / "check-graduated.py"
_spec = importlib.util.spec_from_file_location("check_graduated", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
cg: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules["check_graduated"] = cg
_spec.loader.exec_module(cg)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Fixtures directory
# ---------------------------------------------------------------------------
FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = Path(__file__).parent.parent / "tools" / "check-graduated.py"


# ===========================================================================
# Unit helpers
# ===========================================================================


def _tmp_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a small directory tree under *tmp_path* and return its root."""
    for rel, content in files.items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# parse_hunks
# ---------------------------------------------------------------------------


class TestParseHunks:
    """parse_hunks must correctly decode hunk headers and post-image content."""

    def test_basic_two_hunks(self) -> None:
        patch = textwrap.dedent(
            """\
            diff --git a/src/Foo.cs b/src/Foo.cs
            --- a/src/Foo.cs
            +++ b/src/Foo.cs
            @@ -1,4 +1,3 @@
             line_a
            -removed_line
             line_b
             line_c
            @@ -10,4 +10,4 @@
             line_x
            -old_line
            +new_line
             line_y
             line_z
            """
        )
        hunks = cg.parse_hunks(patch)
        assert len(hunks) == 2

        # First hunk post-image: context lines only (removed line excluded)
        assert hunks[0].post_image_lines == ["line_a", "line_b", "line_c"]
        assert hunks[0].new_start == 1
        assert hunks[0].new_count == 3

        # Second hunk post-image: context + added line
        assert hunks[1].post_image_lines == ["line_x", "new_line", "line_y", "line_z"]
        assert hunks[1].new_start == 10

    def test_file_path_extracted(self) -> None:
        patch = textwrap.dedent(
            """\
            diff --git a/src/Widgets/Button.cs b/src/Widgets/Button.cs
            --- a/src/Widgets/Button.cs
            +++ b/src/Widgets/Button.cs
            @@ -5,3 +5,3 @@
             alpha
            -beta
            +gamma
             delta
            """
        )
        hunks = cg.parse_hunks(patch)
        assert len(hunks) == 1
        assert hunks[0].file_path == "src/Widgets/Button.cs"

    def test_empty_patch_returns_no_hunks(self) -> None:
        assert cg.parse_hunks("") == []

    def test_hunk_count_defaults_to_one(self) -> None:
        """@@ -1 +1 @@ (no comma-count) → new_count == 1."""
        patch = textwrap.dedent(
            """\
            diff --git a/A.cs b/A.cs
            --- a/A.cs
            +++ b/A.cs
            @@ -1 +1 @@
            +only_line
            """
        )
        hunks = cg.parse_hunks(patch)
        assert len(hunks) == 1
        assert hunks[0].new_count == 1
        assert hunks[0].post_image_lines == ["only_line"]


# ---------------------------------------------------------------------------
# _file_contains_sequence
# ---------------------------------------------------------------------------


class TestFileContainsSequence:
    def test_exact_match(self) -> None:
        file_lines = ["a", "b", "c", "d", "e"]
        needle = ["b", "c", "d"]
        found, score = cg._file_contains_sequence(
            file_lines, needle, strict_whitespace=True
        )
        assert found is True
        assert score == 1.0

    def test_no_match(self) -> None:
        file_lines = ["a", "b", "c"]
        needle = ["x", "y"]
        found, score = cg._file_contains_sequence(
            file_lines, needle, strict_whitespace=True
        )
        assert found is False
        assert score < 1.0

    def test_empty_needle_always_found(self) -> None:
        found, score = cg._file_contains_sequence([], [], strict_whitespace=True)
        assert found is True
        assert score == 1.0

    def test_whitespace_tolerant_match(self) -> None:
        file_lines = ["    alpha  ", "\tbeta\t", "  gamma"]
        needle = ["alpha", "beta", "gamma"]
        found, score = cg._file_contains_sequence(
            file_lines, needle, strict_whitespace=False
        )
        assert found is True
        assert score == 1.0

    def test_whitespace_strict_no_match(self) -> None:
        file_lines = ["    alpha  ", "\tbeta\t", "  gamma"]
        needle = ["alpha", "beta", "gamma"]
        found, score = cg._file_contains_sequence(
            file_lines, needle, strict_whitespace=True
        )
        assert found is False


# ---------------------------------------------------------------------------
# compute_verdict
# ---------------------------------------------------------------------------


class TestComputeVerdict:
    def _hr(self, found: bool) -> cg.HunkResult:
        return cg.HunkResult(
            file="f", line_range=[1, 2], found=found, match_score=1.0 if found else 0.0
        )

    def test_all_found_graduated(self) -> None:
        results = [self._hr(True), self._hr(True)]
        assert cg.compute_verdict("p", results) == "graduated"

    def test_none_found_not_graduated(self) -> None:
        results = [self._hr(False), self._hr(False)]
        assert cg.compute_verdict("p", results) == "not-graduated"

    def test_mixed_partial(self) -> None:
        results = [self._hr(True), self._hr(False)]
        assert cg.compute_verdict("p", results) == "partial"

    def test_empty_hunks_not_graduated(self) -> None:
        assert cg.compute_verdict("p", []) == "not-graduated"


# ===========================================================================
# Integration: run main() against the fixtures directory
# ===========================================================================


class TestFixtures:
    """Validate against the sample fixture files in tests/fixtures/."""

    def test_graduated_with_fixture_files(self, tmp_path: Path) -> None:
        """sample-patch.patch vs sample-target-file.txt → graduated (exit 1)."""
        # The patch references src/Foo/Bar.cs; place the file at that exact path.
        _tmp_tree(
            tmp_path,
            {"src/Foo/Bar.cs": (FIXTURES / "sample-target-file.txt").read_text()},
        )
        patch_path = FIXTURES / "sample-patch.patch"
        rc = cg.main(["--patch", str(patch_path), "--target-root", str(tmp_path)])
        assert rc == 1, "Expected exit 1 (graduated)"

    def test_not_graduated_with_fixture_files(self, tmp_path: Path) -> None:
        """sample-patch.patch vs sample-target-file-missing.txt → not-graduated (exit 0)."""
        _tmp_tree(
            tmp_path,
            {"src/Foo/Bar.cs": (FIXTURES / "sample-target-file-missing.txt").read_text()},
        )
        patch_path = FIXTURES / "sample-patch.patch"
        rc = cg.main(["--patch", str(patch_path), "--target-root", str(tmp_path)])
        assert rc == 0, "Expected exit 0 (not-graduated)"


# ===========================================================================
# Integration: programmatic main() calls
# ===========================================================================


def _write_patch(tmp_path: Path, content: str, name: str = "test.patch") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestGraduated:
    """All hunks present → graduated → exit 1."""

    def test_single_hunk_graduated(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/Hello.cs b/Hello.cs
            --- a/Hello.cs
            +++ b/Hello.cs
            @@ -1,3 +1,3 @@
             namespace X
             {
            +    int x = 1;
             }
            """
        )
        target_text = "namespace X\n{\n    int x = 1;\n}\n"
        tree = _tmp_tree(tmp_path / "tree", {"Hello.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 1

    def test_multi_hunk_all_present_graduated(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/M.cs b/M.cs
            --- a/M.cs
            +++ b/M.cs
            @@ -1,2 +1,2 @@
             alpha
            +beta
            @@ -10,2 +10,2 @@
             gamma
            +delta
            """
        )
        target_text = "\n".join(["alpha", "beta"] + [""] * 7 + ["gamma", "delta"]) + "\n"
        tree = _tmp_tree(tmp_path / "tree", {"M.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 1

    def test_graduated_output_json(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/A.cs b/A.cs
            --- a/A.cs
            +++ b/A.cs
            @@ -1,2 +1,2 @@
             line1
            +line2
            """
        )
        target_text = "line1\nline2\n"
        tree = _tmp_tree(tmp_path / "tree", {"A.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)
        out_path = tmp_path / "result.json"
        rc = cg.main(
            ["--patch", str(patch), "--target-root", str(tree), "--output", str(out_path)]
        )
        assert rc == 1
        data = json.loads(out_path.read_text())
        assert data["verdict"] == "graduated"
        assert all(h["found"] for h in data["hunks"])


class TestNotGraduated:
    """No hunks present → not-graduated → exit 0."""

    def test_single_hunk_absent(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/X.cs b/X.cs
            --- a/X.cs
            +++ b/X.cs
            @@ -1,2 +1,2 @@
             old_context
            +brand_new_line
            """
        )
        target_text = "old_context\noriginal_line\n"
        tree = _tmp_tree(tmp_path / "tree", {"X.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 0

    def test_empty_tree_not_graduated(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/Z.cs b/Z.cs
            --- a/Z.cs
            +++ b/Z.cs
            @@ -1,2 +1,2 @@
             ctx
            +added
            """
        )
        tree = tmp_path / "empty_tree"
        tree.mkdir()
        patch = _write_patch(tmp_path, patch_text)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 0

    def test_not_graduated_output_json(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/Q.cs b/Q.cs
            --- a/Q.cs
            +++ b/Q.cs
            @@ -1,2 +1,2 @@
             ctx
            +unique_token_xyz_12345
            """
        )
        target_text = "ctx\nsome_other_content\n"
        tree = _tmp_tree(tmp_path / "tree", {"Q.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)
        out_path = tmp_path / "result.json"
        rc = cg.main(
            ["--patch", str(patch), "--target-root", str(tree), "--output", str(out_path)]
        )
        assert rc == 0
        data = json.loads(out_path.read_text())
        assert data["verdict"] == "not-graduated"
        assert all(not h["found"] for h in data["hunks"])


class TestPartial:
    """Some hunks present, some missing → partial → exit 2."""

    def test_two_hunks_one_present(self, tmp_path: Path) -> None:
        # Hunk 1: present; Hunk 2: absent
        patch_text = textwrap.dedent(
            """\
            diff --git a/P.cs b/P.cs
            --- a/P.cs
            +++ b/P.cs
            @@ -1,2 +1,2 @@
             ctx_a
            +present_line
            @@ -10,2 +10,2 @@
             ctx_b
            +missing_line_zzz
            """
        )
        # File has hunk-1 content but NOT hunk-2 content
        target_text = "\n".join(["ctx_a", "present_line"] + [""] * 7 + ["ctx_b", "old_line"]) + "\n"
        tree = _tmp_tree(tmp_path / "tree", {"P.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 2

    def test_partial_output_json(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/R.cs b/R.cs
            --- a/R.cs
            +++ b/R.cs
            @@ -1,2 +1,2 @@
             ctx1
            +found_content
            @@ -5,2 +5,2 @@
             ctx2
            +unfound_content_abc
            """
        )
        target_text = "\n".join(
            ["ctx1", "found_content", "", "", "ctx2", "NOT_the_right_content"]
        ) + "\n"
        tree = _tmp_tree(tmp_path / "tree", {"R.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)
        out_path = tmp_path / "result.json"
        rc = cg.main(
            ["--patch", str(patch), "--target-root", str(tree), "--output", str(out_path)]
        )
        assert rc == 2
        data = json.loads(out_path.read_text())
        assert data["verdict"] == "partial"
        found_flags = [h["found"] for h in data["hunks"]]
        assert True in found_flags
        assert False in found_flags


class TestWhitespaceTolerance:
    """Without --strict-whitespace, indentation differences should still match."""

    def test_tolerant_match_tabs_vs_spaces(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/T.cs b/T.cs
            --- a/T.cs
            +++ b/T.cs
            @@ -1,3 +1,3 @@
             class Foo
             {
            +    int x = 42;
             }
            """
        )
        # File uses tabs instead of spaces
        target_text = "class Foo\n{\n\tint x = 42;\n}\n"
        tree = _tmp_tree(tmp_path / "tree", {"T.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)

        # Without --strict-whitespace: should be graduated
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 1

    def test_strict_whitespace_no_match(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/T.cs b/T.cs
            --- a/T.cs
            +++ b/T.cs
            @@ -1,3 +1,3 @@
             class Foo
             {
            +    int x = 42;
             }
            """
        )
        # File uses tabs instead of spaces
        target_text = "class Foo\n{\n\tint x = 42;\n}\n"
        tree = _tmp_tree(tmp_path / "tree", {"T.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)

        # With --strict-whitespace: should NOT match
        rc = cg.main(
            ["--patch", str(patch), "--target-root", str(tree), "--strict-whitespace"]
        )
        assert rc == 0

    def test_trailing_whitespace_tolerance(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/W.cs b/W.cs
            --- a/W.cs
            +++ b/W.cs
            @@ -1,2 +1,2 @@
             context_line
            +added_content
            """
        )
        # Target has trailing spaces on the added line
        target_text = "context_line\nadded_content   \n"
        tree = _tmp_tree(tmp_path / "tree", {"W.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)

        # Without strict: should match (trailing spaces stripped)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 1


class TestMultiFilePatch:
    """Multi-file patches: hunks from different files are searched independently."""

    def test_both_files_present(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/A.cs b/A.cs
            --- a/A.cs
            +++ b/A.cs
            @@ -1,2 +1,2 @@
             ctx_a
            +content_a
            diff --git a/B.cs b/B.cs
            --- a/B.cs
            +++ b/B.cs
            @@ -1,2 +1,2 @@
             ctx_b
            +content_b
            """
        )
        tree = _tmp_tree(
            tmp_path / "tree",
            {
                "A.cs": "ctx_a\ncontent_a\n",
                "B.cs": "ctx_b\ncontent_b\n",
            },
        )
        patch = _write_patch(tmp_path, patch_text)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 1

    def test_second_file_missing_partial(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/A.cs b/A.cs
            --- a/A.cs
            +++ b/A.cs
            @@ -1,2 +1,2 @@
             ctx_a
            +content_a
            diff --git a/B.cs b/B.cs
            --- a/B.cs
            +++ b/B.cs
            @@ -1,2 +1,2 @@
             ctx_b
            +absent_content_xyz
            """
        )
        tree = _tmp_tree(
            tmp_path / "tree",
            {
                "A.cs": "ctx_a\ncontent_a\n",
                "B.cs": "ctx_b\noriginal_content\n",
            },
        )
        patch = _write_patch(tmp_path, patch_text)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 2


class TestEdgeCases:
    """Empty patch, no-newline-at-eof, very large context."""

    def test_empty_patch_not_graduated(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        tree.mkdir()
        patch = _write_patch(tmp_path, "")
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 0

    def test_json_output_has_required_keys(self, tmp_path: Path) -> None:
        patch_text = textwrap.dedent(
            """\
            diff --git a/K.cs b/K.cs
            --- a/K.cs
            +++ b/K.cs
            @@ -1,2 +1,2 @@
             ctx
            +new_line
            """
        )
        target_text = "ctx\nnew_line\n"
        tree = _tmp_tree(tmp_path / "tree", {"K.cs": target_text})
        patch = _write_patch(tmp_path, patch_text)
        out_path = tmp_path / "out.json"
        cg.main(["--patch", str(patch), "--target-root", str(tree), "--output", str(out_path)])
        data = json.loads(out_path.read_text())
        assert "patch" in data
        assert "verdict" in data
        assert "hunks" in data
        assert isinstance(data["hunks"], list)
        for h in data["hunks"]:
            assert "file" in h
            assert "line_range" in h
            assert "found" in h
            assert "match_score" in h

    def test_fallback_search_by_basename(self, tmp_path: Path) -> None:
        """If exact path not found, fall back to searching by basename."""
        patch_text = textwrap.dedent(
            """\
            diff --git a/deep/path/Util.cs b/deep/path/Util.cs
            --- a/deep/path/Util.cs
            +++ b/deep/path/Util.cs
            @@ -1,2 +1,2 @@
             ctx
            +util_content
            """
        )
        # Tree has Util.cs but at a different subdirectory
        tree = _tmp_tree(
            tmp_path / "tree",
            {"src/Util.cs": "ctx\nutil_content\n"},
        )
        patch = _write_patch(tmp_path, patch_text)
        rc = cg.main(["--patch", str(patch), "--target-root", str(tree)])
        assert rc == 1


# ===========================================================================
# Subprocess integration (validates CLI exit codes via real process execution)
# ===========================================================================


class TestCLISubprocess:
    """Run the script as a subprocess to verify exit codes end-to-end."""

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + args,
            capture_output=True,
            text=True,
        )

    def test_cli_graduated_exit_1(self, tmp_path: Path) -> None:
        patch_text = (
            "diff --git a/S.cs b/S.cs\n--- a/S.cs\n+++ b/S.cs\n"
            "@@ -1,2 +1,2 @@\n ctx\n+present\n"
        )
        tree = _tmp_tree(tmp_path / "tree", {"S.cs": "ctx\npresent\n"})
        patch = _write_patch(tmp_path, patch_text)
        result = self._run(["--patch", str(patch), "--target-root", str(tree)])
        assert result.returncode == 1

    def test_cli_not_graduated_exit_0(self, tmp_path: Path) -> None:
        patch_text = (
            "diff --git a/U.cs b/U.cs\n--- a/U.cs\n+++ b/U.cs\n"
            "@@ -1,2 +1,2 @@\n ctx\n+absent_zzz\n"
        )
        tree = _tmp_tree(tmp_path / "tree", {"U.cs": "ctx\nother_content\n"})
        patch = _write_patch(tmp_path, patch_text)
        result = self._run(["--patch", str(patch), "--target-root", str(tree)])
        assert result.returncode == 0

    def test_cli_output_is_valid_json(self, tmp_path: Path) -> None:
        patch_text = (
            "diff --git a/V.cs b/V.cs\n--- a/V.cs\n+++ b/V.cs\n"
            "@@ -1,2 +1,2 @@\n ctx\n+val\n"
        )
        tree = _tmp_tree(tmp_path / "tree", {"V.cs": "ctx\nval\n"})
        patch = _write_patch(tmp_path, patch_text)
        result = self._run(["--patch", str(patch), "--target-root", str(tree)])
        data = json.loads(result.stdout)
        assert data["verdict"] in {"graduated", "not-graduated", "partial"}
