"""Tests for the scripts that move work between untrusted and trusted jobs.

tools/conflict-handoff.sh, tools/nightly-merge.sh and
tools/finish-ingestion-branch.sh are run for real on throwaway git repos.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HANDOFF = REPO_ROOT / "tools" / "conflict-handoff.sh"
NIGHTLY_MERGE = REPO_ROOT / "tools" / "nightly-merge.sh"
FINISH = REPO_ROOT / "tools" / "finish-ingestion-branch.sh"


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """Environment that ignores the user's git config (signing, hooks)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
    )
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1")
    return env


def git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_file(repo: Path, env: dict[str, str], path: str, text: str, msg: str) -> None:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(repo, env, "add", path)
    git(repo, env, "commit", "-q", "-m", msg)


@pytest.fixture
def conflicted(tmp_path: Path, git_env: dict[str, str]) -> Path:
    """A repo stopped in a cherry-pick with conflicts on a.txt and gone.txt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, git_env, "init", "-q")
    commit_file(repo, git_env, "a.txt", "base\n", "base")
    commit_file(repo, git_env, "gone.txt", "base\n", "base2")
    git(repo, git_env, "checkout", "-q", "-b", "theirs")
    commit_file(repo, git_env, "a.txt", "theirs\n", "theirs a")
    commit_file(repo, git_env, "gone.txt", "theirs\n", "theirs gone")
    git(repo, git_env, "checkout", "-q", "main")
    commit_file(repo, git_env, "a.txt", "ours\n", "ours a")
    git(repo, git_env, "rm", "-q", "gone.txt")
    git(repo, git_env, "commit", "-q", "-m", "ours deletes gone")
    rc = subprocess.run(
        ["git", "merge", "--no-commit", "theirs"], cwd=repo, env=git_env, capture_output=True
    ).returncode
    assert rc != 0
    (tmp_path / "conflicts.txt").write_text(
        git(repo, git_env, "diff", "--name-only", "--diff-filter=U") + "\n"
    )
    return repo


def handoff(
    mode: str, repo: Path, env: dict[str, str], lst: Path, d: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HANDOFF), mode, str(lst), str(d)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_collect_then_apply_resolves(
    conflicted: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    lst = tmp_path / "conflicts.txt"
    assert set(lst.read_text().split()) == {"a.txt", "gone.txt"}
    (conflicted / "a.txt").write_text("resolved\n")
    (conflicted / "gone.txt").unlink()
    (conflicted / "stray.txt").write_text("should not travel\n")
    out = tmp_path / "res"
    assert handoff("collect", conflicted, git_env, lst, out).returncode == 0
    assert (out / "files" / "a.txt").read_text() == "resolved\n"
    assert (out / "deleted.txt").read_text().strip() == "gone.txt"
    assert not (out / "files" / "stray.txt").exists()

    # A fresh copy of the same conflict, as the trusted job would recreate it.
    git(conflicted, git_env, "merge", "--abort")
    (conflicted / "stray.txt").unlink()
    subprocess.run(
        ["git", "merge", "--no-commit", "theirs"], cwd=conflicted, env=git_env, capture_output=True
    )
    r = handoff("apply", conflicted, git_env, lst, out)
    assert r.returncode == 0, r.stderr
    assert (conflicted / "a.txt").read_text() == "resolved\n"
    assert not (conflicted / "gone.txt").exists()
    assert git(conflicted, git_env, "diff", "--name-only", "--diff-filter=U") == ""


def test_apply_ignores_paths_not_in_its_own_list(
    conflicted: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    lst = tmp_path / "conflicts.txt"
    out = tmp_path / "res"
    (out / "files" / ".github" / "workflows").mkdir(parents=True)
    (out / "files" / ".github" / "workflows" / "evil.yml").write_text("on: push\n")
    (out / "files" / "a.txt").write_text("resolved\n")
    (out / "deleted.txt").write_text("gone.txt\n")
    r = handoff("apply", conflicted, git_env, lst, out)
    assert r.returncode == 0, r.stderr
    assert not (conflicted / ".github").exists()


def test_apply_refuses_conflict_markers(
    conflicted: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    out = tmp_path / "res"
    (out / "files").mkdir(parents=True)
    (out / "files" / "a.txt").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> theirs\n")
    (out / "deleted.txt").write_text("gone.txt\n")
    r = handoff("apply", conflicted, git_env, tmp_path / "conflicts.txt", out)
    assert r.returncode != 0
    assert "Conflict markers" in r.stderr


def test_apply_refuses_symlink(conflicted: Path, git_env: dict[str, str], tmp_path: Path) -> None:
    out = tmp_path / "res"
    (out / "files").mkdir(parents=True)
    (out / "files" / "a.txt").symlink_to("/etc/passwd")
    (out / "deleted.txt").write_text("gone.txt\n")
    r = handoff("apply", conflicted, git_env, tmp_path / "conflicts.txt", out)
    assert r.returncode != 0
    assert "symlink" in r.stderr


def test_apply_refuses_missing_resolution(
    conflicted: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    out = tmp_path / "res"
    (out / "files").mkdir(parents=True)
    (out / "files" / "a.txt").write_text("resolved\n")
    r = handoff("apply", conflicted, git_env, tmp_path / "conflicts.txt", out)
    assert r.returncode != 0
    assert "No resolution for gone.txt" in r.stderr


# ---------------------------------------------------------------------------
# nightly-merge.sh
# ---------------------------------------------------------------------------


def _origin_and_clone(tmp_path: Path, env: dict[str, str]) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, env, "init", "-q")
    commit_file(origin, env, "a.txt", "base\n", "base")
    commit_file(origin, env, ".github/workflows/up.yml", "up\n", "upstream workflow")
    git(origin, env, "branch", "upstream")
    git(origin, env, "rm", "-q", ".github/workflows/up.yml")
    git(origin, env, "commit", "-q", "-m", "fork deletes upstream workflow")
    git(origin, env, "branch", "-m", "if/main")
    work = tmp_path / "work"
    git(tmp_path, env, "clone", "-q", str(origin), str(work))
    return origin, work


def test_nightly_merge_keeps_fork_deletion_of_github_files(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    origin, work = _origin_and_clone(tmp_path, git_env)
    git(origin, git_env, "checkout", "-q", "upstream")
    commit_file(origin, git_env, ".github/workflows/up.yml", "changed\n", "upstream edits workflow")
    commit_file(origin, git_env, "a.txt", "upstream\n", "upstream edits a")
    sha = git(origin, git_env, "rev-parse", "HEAD")
    git(work, git_env, "fetch", "-q", "origin", "upstream")
    out = tmp_path / "out"
    r = subprocess.run(
        ["bash", str(NIGHTLY_MERGE)],
        cwd=work,
        capture_output=True,
        text=True,
        env={**git_env, "UPSTREAM_SHA": sha, "OUT": str(out)},
    )
    assert r.returncode == 0, r.stderr
    assert (out / "conflicts.txt").read_text() == ""
    assert not (work / ".github" / "workflows" / "up.yml").exists()
    assert (work / "a.txt").read_text() == "upstream\n"
    assert git(work, git_env, "rev-parse", "--abbrev-ref", "HEAD") == "if/staging"


def test_nightly_merge_rejects_bad_sha(tmp_path: Path, git_env: dict[str, str]) -> None:
    r = subprocess.run(
        ["bash", str(NIGHTLY_MERGE)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**git_env, "UPSTREAM_SHA": "main; rm -rf /", "OUT": str(tmp_path / "o")},
    )
    assert r.returncode == 1
    assert "invalid UPSTREAM_SHA" in r.stderr


# ---------------------------------------------------------------------------
# finish-ingestion-branch.sh
# ---------------------------------------------------------------------------


def _finish(work: Path, env: dict[str, str], out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FINISH)],
        cwd=work,
        capture_output=True,
        text=True,
        env={
            **env,
            "PR_NUMBER": "123",
            "PINNED_HEAD_SHA": "a" * 40,
            "BRANCH": "claude/cherry-pick-pr-123",
            "TRUSTED": str(REPO_ROOT),
            "OUT": str(out),
        },
    )


def test_finish_refuses_denylisted_file_and_does_not_push(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    origin, work = _origin_and_clone(tmp_path, git_env)
    git(work, git_env, "checkout", "-q", "-b", "claude/cherry-pick-pr-123")
    commit_file(work, git_env, "global.json", "{}\n", "pick")
    out = tmp_path / "out"
    r = _finish(work, git_env, out)
    assert r.returncode == 0, r.stderr
    assert (out / "reason").read_text().strip() == "file_denylist"
    assert not (out / "pushed").exists()
    branches = git(origin, git_env, "branch", "--list", "claude/*")
    assert branches == ""


def test_finish_pushes_clean_branch_with_trailers(tmp_path: Path, git_env: dict[str, str]) -> None:
    origin, work = _origin_and_clone(tmp_path, git_env)
    git(work, git_env, "checkout", "-q", "-b", "claude/cherry-pick-pr-123")
    commit_file(work, git_env, "src/x.cs", "class X {}\n", "pick")
    out = tmp_path / "out"
    r = _finish(work, git_env, out)
    assert r.returncode == 0, r.stderr
    assert (out / "pushed").read_text().strip() == "true"
    msg = git(origin, git_env, "log", "-1", "--format=%B", "claude/cherry-pick-pr-123")
    assert "Upstream-PR-Head: " + "a" * 40 in msg


def test_finish_refuses_more_than_one_commit(tmp_path: Path, git_env: dict[str, str]) -> None:
    _, work = _origin_and_clone(tmp_path, git_env)
    git(work, git_env, "checkout", "-q", "-b", "claude/cherry-pick-pr-123")
    commit_file(work, git_env, "src/x.cs", "1\n", "one")
    commit_file(work, git_env, "src/y.cs", "2\n", "two")
    out = tmp_path / "out"
    _finish(work, git_env, out)
    assert (out / "reason").read_text().strip() == "not_exactly_one_commit"
