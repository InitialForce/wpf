"""Tests for tools/resolution-scope.py and the resolution-only review contract.

Astra 3: a sync that cleanly brings an upstream change to a denylisted file
(eng/Versions.props) and has one allowed source conflict must not be judged
on the upstream part. The reviewer and the record job both get the
resolution scope: git's own merge -> the result.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TOOL = REPO_ROOT / "tools" / "resolution-scope.py"
DENYLIST = REPO_ROOT / "tools" / "check-denylist.py"
CONFIG = REPO_ROOT / ".if-fork" / "config.yaml"
PROMPT = REPO_ROOT / ".if-fork" / "prompts" / "resolution-review.md"

ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@x"}


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, env=ENV, capture_output=True, text=True)
    assert r.returncode in (0, 1), r.stderr
    return r.stdout.strip()


def write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")


def sync_fixture(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Upstream updates eng/Versions.props cleanly and conflicts in src/a.cs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, {"eng/Versions.props": "<V>1</V>\n", "src/a.cs": "int x = 1;\n"})
    git(repo, "commit", "-q", "-m", "base")
    mb = git(repo, "rev-parse", "HEAD")
    write(repo, {"eng/Versions.props": "<V>2</V>\n", "src/a.cs": "int x = 3;\n"})
    git(repo, "commit", "-q", "-m", "upstream")
    upstream = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "fork", mb)
    write(repo, {"src/a.cs": "int x = 2;\n"})
    git(repo, "commit", "-q", "-m", "fork")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "merge", "-q", "--no-commit", upstream)  # conflicts in src/a.cs
    write(repo, {"src/a.cs": "int x = 2; // keeps the fork value\n"})
    git(repo, "commit", "-q", "-m", "merge upstream")
    branch = git(repo, "rev-parse", "HEAD")
    return repo, base, upstream, branch


def scope(repo: Path, kind: str, base: str, upstream: str, branch: str, out: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(TOOL), "--kind", kind, "--base-sha", base,
         "--upstream-sha", upstream, "--branch-sha", branch, "--out", str(out)],
        cwd=repo, env=ENV, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def denylist_ok(files: Path) -> bool:
    r = subprocess.run(
        [sys.executable, str(DENYLIST), "--config", str(CONFIG), "--changed-files", str(files)],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def test_sync_scope_holds_only_the_resolved_file(tmp_path: Path) -> None:
    repo, base, upstream, branch = sync_fixture(tmp_path)
    out = tmp_path / "out"
    scope(repo, "sync", base, upstream, branch, out)
    assert (out / "resolution-files.txt").read_text().split() == ["src/a.cs"]
    diff = (out / "resolution.diff").read_text()
    assert "+int x = 2; // keeps the fork value" in diff and "Versions.props" not in diff
    # The resolution passes the denylist; the full branch diff would not.
    assert denylist_ok(out / "resolution-files.txt")
    full = tmp_path / "full.txt"
    full.write_text(git(repo, "diff", "--name-only", base, branch) + "\n")
    assert "eng/Versions.props" in full.read_text() and not denylist_ok(full)


def test_cherry_pick_scope_is_the_whole_branch_diff(tmp_path: Path) -> None:
    repo, base, upstream, branch = sync_fixture(tmp_path)
    out = tmp_path / "out"
    scope(repo, "cherry-pick", base, upstream, branch, out)
    assert (out / "resolution-files.txt").read_text().split() == [
        "eng/Versions.props", "src/a.cs"
    ]


def test_bad_sha_is_refused(tmp_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(TOOL), "--kind", "sync", "--base-sha", "x", "--upstream-sha",
         "a" * 40, "--branch-sha", "a" * 40, "--out", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1


def test_prompt_applies_its_rules_to_the_resolution_scope() -> None:
    text = PROMPT.read_text()
    assert "`review-input/resolution.diff`" in text
    assert "`review-input/resolution-files.txt`" in text
    step7 = text[text.index("**Step 7."):text.index("**Step 8.")]
    assert "resolution.diff" in step7 and "resolution-files.txt" in step7
    assert "is not a reason to reject" in step7
    # The old, wrong claim that the workflow scans the full diff is gone.
    assert "runs the same scan in code" not in text
    assert "do not judge it" in text
