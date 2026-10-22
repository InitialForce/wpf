"""Tests for tools/apply-upstream-pr.sh with a stub gh on PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "apply-upstream-pr.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not available")


def _run(tmp_path: Path, upr: dict) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "upr.json").write_text(json.dumps(upr))
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\ncat "{tmp_path / "upr.json"}"\n')
    gh.chmod(0o755)
    out = tmp_path / "out"
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PR_NUMBER": "12",
            "PINNED_HEAD_SHA": "a" * 40,
            "BRANCH": "claude/cherry-pick-pr-12",
            "BASE_SHA": "c" * 40,
            "OUT": str(out),
            "TRUSTED": str(REPO_ROOT),
            "GH_TOKEN": "x",
        },
    )
    return r, out


def test_title_with_newline_is_one_line(tmp_path: Path) -> None:
    # A head that moved makes the script stop right after it writes the title.
    upr = {
        "head": {"sha": "b" * 40},
        "merged": False,
        "merge_commit_sha": None,
        "user": {"login": "someone"},
        "title": "Fix a thing\r\nresult=applied\nreason=",
    }
    r, out = _run(tmp_path, upr)
    assert r.returncode == 0, r.stderr
    title = (out / "title").read_text()
    assert "\n" not in title and "\r" not in title
    assert title == "Fix a thingresult=appliedreason="
    assert (out / "result").read_text().strip() == "refused"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.email=t@example.com",
         "-c", "user.name=T", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _merged_pr_repos(tmp_path: Path) -> tuple[Path, str, str]:
    """Upstream with PR #12 merged into main; a fork whose if/main has it."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    (up / "a.cs").write_text("int x = 0;\n")
    _git(up, "add", ".")
    _git(up, "commit", "-q", "-m", "base")
    _git(up, "checkout", "-q", "-b", "pr")
    (up / "a.cs").write_text("int x = 1;\n")
    _git(up, "commit", "-q", "-am", "change")
    head = _git(up, "rev-parse", "HEAD")
    _git(up, "update-ref", "refs/pull/12/head", head)
    _git(up, "checkout", "-q", "main")
    _git(up, "merge", "-q", "--no-ff", "-m", "Merge pull request #12 from x/pr", "pr")
    merge = _git(up, "rev-parse", "HEAD")
    fork = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", str(up), str(fork))
    _git(fork, "remote", "add", "upstream", str(up))
    # A later fork change to the same line: a fresh cherry-pick would conflict.
    (fork / "a.cs").write_text("int x = 2;\n")
    _git(fork, "commit", "-q", "-am", "later fork edit")
    return fork, head, merge


def test_merge_commit_already_in_base_is_empty(tmp_path: Path) -> None:
    """Regression: #9551, #9431, #9676, #9215 and #9222 were cherry-picked
    again although their upstream merge commits were already in if/main
    through upstream release/10.0. Each ended in a conflict and a stop."""
    fork, head, merge = _merged_pr_repos(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    upr = {"head": {"sha": head}, "merged": True, "merge_commit_sha": merge,
           "user": {"login": "someone"}, "title": "Change"}
    (tmp_path / "upr.json").write_text(json.dumps(upr))
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\ncat "{tmp_path / "upr.json"}"\n')
    gh.chmod(0o755)
    out = tmp_path / "out"
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=fork, capture_output=True, text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PR_NUMBER": "12",
            "PINNED_HEAD_SHA": head,
            "BRANCH": "claude/cherry-pick-pr-12",
            "BASE_SHA": _git(fork, "rev-parse", "HEAD"),
            "OUT": str(out),
            "TRUSTED": str(REPO_ROOT),
            "GH_TOKEN": "x",
        },
    )
    assert r.returncode == 0, r.stderr
    assert (out / "result").read_text().strip() == "empty"
    assert (out / "detection").read_text().strip() == "merge_commit_in_history"
    assert _git(fork, "status", "--porcelain") == ""


def _merged_upstream(
    tmp_path: Path, merge_edit: tuple[str, str] | None = None
) -> tuple[Path, str, str]:
    """Upstream where PR #12 (head H) is merged as M, and a fork from before M.

    *merge_edit* (path, content) is written into M as well: a change the
    review of H never saw (an edit made while merging)."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    (up / "a.cs").write_text("int x = 0;\n")
    (up / "b.cs").write_text("int y = 0;\n")
    _git(up, "add", ".")
    _git(up, "commit", "-q", "-m", "base")
    fork = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", str(up), str(fork))
    _git(fork, "remote", "add", "upstream", str(up))
    _git(up, "checkout", "-q", "-b", "pr")
    (up / "a.cs").write_text('string s = "a b";\n')
    _git(up, "commit", "-q", "-am", "change")
    head = _git(up, "rev-parse", "HEAD")
    _git(up, "update-ref", "refs/pull/12/head", head)
    _git(up, "checkout", "-q", "main")
    _git(up, "merge", "-q", "--no-ff", "--no-commit", "pr")
    if merge_edit:
        (up / merge_edit[0]).write_text(merge_edit[1])
        _git(up, "add", merge_edit[0])
    _git(up, "commit", "-q", "-m", "Merge pull request #12 from x/pr")
    return fork, head, _git(up, "rev-parse", "HEAD")


def _apply_merged(tmp_path: Path, fork: Path, head: str, merge: str) -> tuple[Path, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    upr = {"head": {"sha": head}, "merged": True, "merge_commit_sha": merge,
           "user": {"login": "someone"}, "title": "Change"}
    (tmp_path / "upr.json").write_text(json.dumps(upr))
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\ncat "{tmp_path / "upr.json"}"\n')
    gh.chmod(0o755)
    out = tmp_path / "out"
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=fork, capture_output=True, text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
            "PR_NUMBER": "12",
            "PINNED_HEAD_SHA": head,
            "BRANCH": "claude/cherry-pick-pr-12",
            "BASE_SHA": _git(fork, "rev-parse", "HEAD"),
            "OUT": str(out),
            "TRUSTED": str(REPO_ROOT),
            "GH_TOKEN": "x",
        },
    )
    assert r.returncode == 0, r.stderr
    assert (out / "result").read_text().strip() == "applied"
    return out, r.stdout


def test_merge_commit_with_the_approved_diff_is_applied(tmp_path: Path) -> None:
    fork, head, merge = _merged_upstream(tmp_path)
    out, _ = _apply_merged(tmp_path, fork, head, merge)
    assert (out / "source_kind").read_text().strip() == "merge_commit"
    assert (out / "source").read_text().strip() == merge
    assert (fork / "a.cs").read_text() == 'string s = "a b";\n'
    assert merge in _git(fork, "log", "-1", "--format=%B")


def test_merge_commit_that_differs_applies_only_the_approved_diff(tmp_path: Path) -> None:
    """Astra: H approved, upstream merges it as M with an extra edit. Only the
    reviewed diff merge-base..H may land in the fork."""
    fork, head, merge = _merged_upstream(tmp_path, ("b.cs", "int y = 666;\n"))
    out, stdout = _apply_merged(tmp_path, fork, head, merge)
    assert (out / "source_kind").read_text().strip() == "approved_diff"
    assert (out / "source").read_text().strip() == head
    assert (fork / "a.cs").read_text() == 'string s = "a b";\n'
    assert (fork / "b.cs").read_text() == "int y = 0;\n"
    assert "differs from the approved diff" in stdout


def test_whitespace_only_edit_in_a_string_literal_is_not_the_approved_diff(
    tmp_path: Path,
) -> None:
    """Astra: H sets "a b", the merge commit sets "ab". git patch-id --stable
    gives both the same id; the exact comparison must not."""
    fork, head, merge = _merged_upstream(tmp_path, ("a.cs", 'string s = "ab";\n'))
    out, _ = _apply_merged(tmp_path, fork, head, merge)
    assert (out / "source_kind").read_text().strip() == "approved_diff"
    assert (fork / "a.cs").read_text() == 'string s = "a b";\n'
