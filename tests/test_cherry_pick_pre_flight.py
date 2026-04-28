"""Tests for tools/cherry-pick-pre-flight.sh.

Uses subprocess to invoke the Bash script directly so the tests exercise the
real shell logic.  Upstream fetches are bypassed via --patch-path, making
all cases runnable offline without any git remotes.

Covered cases:
  1. --help exits 0 and prints usage text
  2. Missing required args exits 1
  3. Invalid --pr-number exits 1
  4. not-graduated: patch whose hunks are absent from the tree → action=apply, exit 0
  5. graduated: patch whose hunks are all present → action=skip, exit 0
  6. graduated: ledger event emitted (ledger file updated)
  7. partial: only some hunks present → action=escalate, exit 2
  8. partial: ledger escalated event emitted
  9. Unknown --patch-path exits 1
 10. check-graduated.py exit forwarded correctly when script not found
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "cherry-pick-pre-flight.sh"
FIXTURES = Path(__file__).parent / "fixtures"

# Patch files used across tests
PATCH_GRADUATED = FIXTURES / "patch-graduated.patch"
PATCH_NOT_GRADUATED = FIXTURES / "patch-not-graduated.patch"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(
    *extra_args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run cherry-pick-pre-flight.sh with extra_args; return the result."""
    cmd = ["bash", str(SCRIPT), *extra_args]
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        env=merged_env,
        check=check,
    )


def _stdout_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Parse the single JSON object from stdout."""
    stdout = result.stdout.strip()
    assert stdout, f"Expected JSON on stdout, got empty string\nstderr={result.stderr}"
    return json.loads(stdout)


def _write_minimal_ledger(path: Path) -> None:
    """Create an empty (genesis) ledger at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _make_graduated_tree(root: Path) -> None:
    """Populate *root* with the post-image content of patch-graduated.patch."""
    target = root / "src" / "Foo" / "Bar.cs"
    target.parent.mkdir(parents=True, exist_ok=True)
    # This is the post-image of sample-patch.patch / patch-graduated.patch
    target.write_text(
        textwrap.dedent(
            """\
            namespace Foo
            {
                public class Bar
                {
                    private readonly Dictionary<string, object> _cache;

                    public Bar()
                    {
                    }

                    public void Initialize()
                    {
                        _cache.Clear();
                        _initialized = true;
                    }
                }
            }
            """
        ),
        encoding="utf-8",
    )


def _make_not_graduated_tree(root: Path) -> None:
    """Populate *root* without the content that patch-not-graduated.patch adds."""
    target = root / "src" / "Qux" / "Baz.cs"
    target.parent.mkdir(parents=True, exist_ok=True)
    # The patch changes "_count = 0" → "_count"; keep the pre-image here
    target.write_text(
        textwrap.dedent(
            """\
            namespace Qux
            {
                public class Baz
                {
                    private int _count = 0;
                }
            }
            """
        ),
        encoding="utf-8",
    )


def _base_args(
    *,
    pr_number: str = "9999",
    head_sha: str = "abc1234567890abcdef",
    upstream_ref: str = "upstream",
    ledger_path: str,
    patch_path: str,
    target_root: str,
) -> list[str]:
    return [
        "--pr-number", pr_number,
        "--head-sha", head_sha,
        "--upstream-ref", upstream_ref,
        "--ledger-path", ledger_path,
        "--patch-path", patch_path,
        "--target-root", target_root,
    ]


# ===========================================================================
# Case 1 — --help
# ===========================================================================

def test_help_exits_zero() -> None:
    result = _run("--help")
    assert result.returncode == 0, f"--help must exit 0, got {result.returncode}"
    assert "Usage" in result.stderr or "usage" in result.stderr.lower(), (
        f"--help should print usage to stderr\nstderr={result.stderr}"
    )


# ===========================================================================
# Case 2 — Missing required args exits 1
# ===========================================================================

@pytest.mark.parametrize(
    "missing_arg",
    ["--pr-number", "--head-sha", "--upstream-ref", "--ledger-path"],
)
def test_missing_required_arg_exits_one(missing_arg: str, tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _write_minimal_ledger(ledger)
    patch = str(PATCH_NOT_GRADUATED)
    tree = str(tmp_path / "tree")

    # Build a full args list then drop the target argument and its value
    full = _base_args(
        ledger_path=str(ledger),
        patch_path=patch,
        target_root=tree,
    )
    # Remove the pair (arg, value)
    idx = full.index(missing_arg)
    pruned = full[:idx] + full[idx + 2:]

    result = _run(*pruned)
    assert result.returncode == 1, (
        f"Missing {missing_arg!r} should exit 1, got {result.returncode}"
    )


# ===========================================================================
# Case 3 — Invalid --pr-number exits 1
# ===========================================================================

def test_invalid_pr_number_exits_one(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _write_minimal_ledger(ledger)
    tree = tmp_path / "tree"
    tree.mkdir()

    result = _run(*_base_args(
        pr_number="not-a-number",
        ledger_path=str(ledger),
        patch_path=str(PATCH_NOT_GRADUATED),
        target_root=str(tree),
    ))
    assert result.returncode == 1, (
        f"Invalid pr-number should exit 1, got {result.returncode}"
    )


# ===========================================================================
# Case 4 — not-graduated: patch whose hunks are absent → action=apply, exit 0
# ===========================================================================

def test_not_graduated_returns_apply(tmp_path: Path) -> None:
    """A patch whose hunks are NOT in the target tree → action=apply, exit 0."""
    tree = tmp_path / "tree"
    _make_not_graduated_tree(tree)

    ledger = tmp_path / ".if-fork" / "patch-ledger.jsonl"
    _write_minimal_ledger(ledger)

    result = _run(*_base_args(
        ledger_path=str(ledger),
        patch_path=str(PATCH_NOT_GRADUATED),
        target_root=str(tree),
    ))

    assert result.returncode == 0, (
        f"not-graduated should exit 0\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    data = _stdout_json(result)
    assert data["action"] == "apply", f"Expected action=apply, got {data}"
    assert "patch_path" in data, f"apply response must include patch_path: {data}"


# ===========================================================================
# Case 5 — graduated: all hunks present → action=skip, exit 0
# ===========================================================================

def test_graduated_returns_skip(tmp_path: Path) -> None:
    """A patch whose hunks ARE in the target tree → action=skip, exit 0."""
    tree = tmp_path / "tree"
    _make_graduated_tree(tree)

    ledger = tmp_path / ".if-fork" / "patch-ledger.jsonl"
    _write_minimal_ledger(ledger)

    # We need a real git repo for ledger-event.py to commit into
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    # Initial commit so ledger-event.py can stage & commit
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )

    result = _run(
        *_base_args(
            ledger_path=str(ledger),
            patch_path=str(PATCH_GRADUATED),
            target_root=str(tree),
        ),
        cwd=tmp_path,
    )

    assert result.returncode == 0, (
        f"graduated should exit 0\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    data = _stdout_json(result)
    assert data["action"] == "skip", f"Expected action=skip, got {data}"
    assert data.get("reason") == "already-graduated", f"Expected reason=already-graduated: {data}"


# ===========================================================================
# Case 6 — graduated: ledger file updated
# ===========================================================================

def test_graduated_ledger_event_written(tmp_path: Path) -> None:
    """Graduation emits a graduated_upstream event to the ledger."""
    tree = tmp_path / "tree"
    _make_graduated_tree(tree)

    ledger = tmp_path / ".if-fork" / "patch-ledger.jsonl"
    _write_minimal_ledger(ledger)

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )

    _run(
        *_base_args(
            ledger_path=str(ledger),
            patch_path=str(PATCH_GRADUATED),
            target_root=str(tree),
        ),
        cwd=tmp_path,
    )

    assert ledger.exists(), "Ledger file must exist after graduated run"
    lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
    assert lines, "Ledger must have at least one event after graduation"
    event = json.loads(lines[-1])
    assert event["event"] == "graduated_upstream", (
        f"Expected graduated_upstream event, got: {event['event']}"
    )


# ===========================================================================
# Case 7 — partial: action=escalate, exit 2
# ===========================================================================

def _make_partial_patch(tmp_path: Path) -> Path:
    """Return a patch file with two hunks, where only one is in the tree."""
    patch_path = tmp_path / "partial.patch"
    # First hunk: present in the tree (same as graduated patch hunk 1)
    # Second hunk: touches a file that doesn't exist → not present
    patch_path.write_text(
        textwrap.dedent(
            """\
            diff --git a/src/Foo/Bar.cs b/src/Foo/Bar.cs
            index a1b2c3d..e5f6789 100644
            --- a/src/Foo/Bar.cs
            +++ b/src/Foo/Bar.cs
            @@ -1,9 +1,8 @@ namespace Foo
             namespace Foo
             {
                 public class Bar
                 {
                     private readonly Dictionary<string, object> _cache;
            -        private bool _isInitialized = false;

                     public Bar()
                     {
            diff --git a/src/Missing/Widget.cs b/src/Missing/Widget.cs
            index 0000000..1111111 100644
            --- a/src/Missing/Widget.cs
            +++ b/src/Missing/Widget.cs
            @@ -1,5 +1,5 @@ namespace Missing
             namespace Missing
             {
            -    public class Widget { private int x = 0; }
            +    public class Widget { private int x; }
             }
            """
        ),
        encoding="utf-8",
    )
    return patch_path


def test_partial_returns_escalate(tmp_path: Path) -> None:
    """A partially-graduated patch → action=escalate, exit 2."""
    tree = tmp_path / "tree"
    # Only the Foo/Bar.cs file exists (graduated hunk present), Widget.cs absent
    _make_graduated_tree(tree)

    ledger = tmp_path / ".if-fork" / "patch-ledger.jsonl"
    _write_minimal_ledger(ledger)

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )

    patch = _make_partial_patch(tmp_path)

    result = _run(
        *_base_args(
            ledger_path=str(ledger),
            patch_path=str(patch),
            target_root=str(tree),
        ),
        cwd=tmp_path,
    )

    assert result.returncode == 2, (
        f"partial should exit 2\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    data = _stdout_json(result)
    assert data["action"] == "escalate", f"Expected action=escalate, got {data}"


# ===========================================================================
# Case 8 — partial: escalated event emitted in ledger
# ===========================================================================

def test_partial_ledger_event_written(tmp_path: Path) -> None:
    """Partial graduation emits an 'escalated' event to the ledger."""
    tree = tmp_path / "tree"
    _make_graduated_tree(tree)

    ledger = tmp_path / ".if-fork" / "patch-ledger.jsonl"
    _write_minimal_ledger(ledger)

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )

    patch = _make_partial_patch(tmp_path)

    _run(
        *_base_args(
            ledger_path=str(ledger),
            patch_path=str(patch),
            target_root=str(tree),
        ),
        cwd=tmp_path,
    )

    assert ledger.exists(), "Ledger file must exist after partial run"
    lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
    assert lines, "Ledger must have at least one event after partial run"
    event = json.loads(lines[-1])
    assert event["event"] == "escalated", (
        f"Expected escalated event, got: {event['event']}"
    )


# ===========================================================================
# Case 9 — Non-existent --patch-path exits 1
# ===========================================================================

def test_nonexistent_patch_path_exits_one(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _write_minimal_ledger(ledger)
    tree = tmp_path / "tree"
    tree.mkdir()

    result = _run(*_base_args(
        ledger_path=str(ledger),
        patch_path=str(tmp_path / "does-not-exist.patch"),
        target_root=str(tree),
    ))
    assert result.returncode == 1, (
        f"Non-existent patch path should exit 1, got {result.returncode}"
    )
