"""Tests for tools/check-prompt-schema.py."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).parent.parent / "tools" / "check-prompt-schema.py"

REQUIRED_SECTIONS = [
    "## Inherits from preamble.md",
    "## Allowed tools",
    "## Inputs",
    "## Output contract",
]

VALID_PROMPT_BODY = textwrap.dedent(
    """\
    ## Inherits from preamble.md

    ## Role

    Some role description.

    ## Allowed tools

    - Bash
    - Read

    ## Inputs

    | Variable | Description |
    |---|---|
    | FOO | bar |

    ## Output contract

    Outputs something.

    ## Procedure

    Do stuff.
    """
)

VALID_PREAMBLE_BODY = textwrap.dedent(
    """\
    ## Preamble (Inheritable)

    1. Never do bad things.
    """
)


def run_validator(prompts_dir: Path) -> tuple[int, dict]:  # type: ignore[type-arg]
    """Run the validator against the given directory; return (exit_code, parsed_json)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--prompts-dir", str(prompts_dir)],
        capture_output=True,
        text=True,
    )
    parsed = json.loads(result.stdout)
    return result.returncode, parsed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidDirectory:
    """Happy-path: directory with all required sections passes."""

    def test_valid_prompt_passes(self, tmp_path: Path) -> None:
        (tmp_path / "pr-review-1.md").write_text(VALID_PROMPT_BODY)
        code, data = run_validator(tmp_path)
        assert code == 0
        assert data["valid"] is True
        assert data["files_checked"] == 1
        assert data["errors"] == []

    def test_valid_preamble_passes(self, tmp_path: Path) -> None:
        (tmp_path / "preamble.md").write_text(VALID_PREAMBLE_BODY)
        code, data = run_validator(tmp_path)
        assert code == 0
        assert data["valid"] is True

    def test_multiple_valid_files(self, tmp_path: Path) -> None:
        (tmp_path / "preamble.md").write_text(VALID_PREAMBLE_BODY)
        (tmp_path / "pr-review-1.md").write_text(VALID_PROMPT_BODY)
        (tmp_path / "cherry-pick.md").write_text(VALID_PROMPT_BODY)
        code, data = run_validator(tmp_path)
        assert code == 0
        assert data["files_checked"] == 3

    def test_files_checked_count(self, tmp_path: Path) -> None:
        for i in range(5):
            (tmp_path / f"prompt-{i}.md").write_text(VALID_PROMPT_BODY)
        code, data = run_validator(tmp_path)
        assert code == 0
        assert data["files_checked"] == 5


class TestMissingSection:
    """Each missing required section is detected and reported."""

    @pytest.mark.parametrize("missing_section", REQUIRED_SECTIONS)
    def test_missing_required_section(
        self, tmp_path: Path, missing_section: str
    ) -> None:
        body = VALID_PROMPT_BODY.replace(missing_section, "")
        (tmp_path / "test-prompt.md").write_text(body)
        code, data = run_validator(tmp_path)
        assert code == 2
        assert data["valid"] is False
        assert any(missing_section in err for err in data["errors"])

    def test_all_sections_missing(self, tmp_path: Path) -> None:
        (tmp_path / "bare.md").write_text("# Bare file\n\nNo sections here.\n")
        code, data = run_validator(tmp_path)
        assert code == 2
        assert len(data["errors"]) == len(REQUIRED_SECTIONS)


class TestEdgeCases:
    """Edge cases: empty files, missing dir, preamble special handling."""

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "empty.md").write_text("")
        code, data = run_validator(tmp_path)
        assert code == 2
        assert data["valid"] is False
        assert any("empty" in err for err in data["errors"])

    def test_whitespace_only_file_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "blank.md").write_text("   \n\t\n  ")
        code, data = run_validator(tmp_path)
        assert code == 2
        assert data["valid"] is False

    def test_missing_directory_returns_error(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "no-such-dir"
        code, data = run_validator(nonexistent)
        assert code == 2
        assert data["valid"] is False
        assert data["files_checked"] == 0

    def test_no_md_files_returns_error(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").write_text("not a markdown file")
        code, data = run_validator(tmp_path)
        assert code == 2
        assert data["valid"] is False

    def test_preamble_missing_heading_fails(self, tmp_path: Path) -> None:
        (tmp_path / "preamble.md").write_text("# Wrong heading\n\nNo preamble marker.\n")
        code, data = run_validator(tmp_path)
        assert code == 2
        assert data["valid"] is False

    def test_preamble_not_checked_for_inherits_line(self, tmp_path: Path) -> None:
        """preamble.md should NOT be required to have '## Inherits from preamble.md'."""
        (tmp_path / "preamble.md").write_text(VALID_PREAMBLE_BODY)
        code, data = run_validator(tmp_path)
        assert code == 0
        assert data["valid"] is True
        assert data["errors"] == []

    def test_partial_section_name_not_sufficient(self, tmp_path: Path) -> None:
        """A heading that partially matches must not satisfy the requirement."""
        # Replace the exact required section with a truncated / wrong version
        # that does NOT contain the original string as a substring.
        body = VALID_PROMPT_BODY.replace(
            "## Allowed tools",
            "## Tools allowed",  # reversed-word variant — not a substring match
        )
        (tmp_path / "prompt.md").write_text(body)
        code, data = run_validator(tmp_path)
        assert code == 2
        assert any("## Allowed tools" in err for err in data["errors"])

    def test_output_is_valid_json(self, tmp_path: Path) -> None:
        (tmp_path / "pr-review-1.md").write_text(VALID_PROMPT_BODY)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--prompts-dir", str(tmp_path)],
            capture_output=True,
            text=True,
        )
        # Should not raise
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, dict)
        assert "valid" in parsed
        assert "files_checked" in parsed
        assert "errors" in parsed


class TestRealPromptsDirectory:
    """Integration: run against the actual .if-fork/prompts directory."""

    def test_real_prompts_pass_validation(self) -> None:
        repo_root = Path(__file__).parent.parent
        prompts_dir = repo_root / ".if-fork" / "prompts"
        if not prompts_dir.exists():
            pytest.skip("prompts directory not found — skipping integration test")
        code, data = run_validator(prompts_dir)
        assert code == 0, f"Validation errors: {data['errors']}"
        assert data["valid"] is True
        assert data["files_checked"] >= 8
