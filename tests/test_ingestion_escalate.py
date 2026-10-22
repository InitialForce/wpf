"""Tests for tools/ingestion-escalate.sh: a designed stop ends green."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "ingestion-escalate.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.email=t@example.com",
         "-c", "user.name=T", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _run(tmp_path: Path, reason: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "if/main", str(work))
    (work / "a.txt").write_text("a\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "if/main")
    _git(work, "fetch", "-q", "origin")
    trusted = tmp_path / "trusted"
    (trusted / "tools").mkdir(parents=True)
    calls = tmp_path / "ledger-calls.txt"
    (trusted / "tools" / "ledger-event.py").write_text(
        "import sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
    )
    shutil.copy(REPO_ROOT / "tools" / "close-ingestion-pr.sh", trusted / "tools")
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", str(SCRIPT), reason],
        cwd=work, capture_output=True, text=True,
        env={
            **{k: v for k, v in os.environ.items() if k != "GH_TOKEN"},
            "PR_NUMBER": "9551",
            "PINNED_HEAD_SHA": "a" * 40,
            "TRUSTED": str(trusted),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "InitialForce/wpf",
            "GITHUB_RUN_ID": "1",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )
    return result, calls


@pytest.mark.parametrize(
    "reason",
    ["file_denylist", "fork_owned_paths", "head_moved_after_review",
     "cherry_pick_conflict", "conflict_in_denylist", "commit_denylist"],
)
def test_designed_stop_records_and_exits_zero(tmp_path: Path, reason: str) -> None:
    """Regression: every designed stop exited 1, so Claude On Failure opened
    a "CI failure" issue for each (#26, #29-#31, #33, #34)."""
    result, calls = _run(tmp_path, reason)
    assert result.returncode == 0, result.stderr
    assert "::notice::" in result.stdout and "::error::" not in result.stdout
    # It tries to close the fork PR (no token here, so only a warning).
    assert "close-ingestion-pr: no GH_TOKEN" in result.stdout
    assert f'"reason":"{reason}"' in calls.read_text()


@pytest.mark.parametrize("reason", ["apply_failed", "reapply_mismatch", "Not-A-Reason"])
def test_real_failure_still_fails(tmp_path: Path, reason: str) -> None:
    result, calls = _run(tmp_path, reason)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "--event escalated" in calls.read_text()
