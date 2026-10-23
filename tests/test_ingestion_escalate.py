"""Tests for tools/ingestion-escalate.sh: a designed stop ends green."""

from __future__ import annotations

import os
import re
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


DESIGNED = [
    "head_moved_after_review", "commit_denylist", "conflict_in_denylist",
    "file_denylist", "fork_owned_paths", "paths_outside_allowlist", "resolver_refused",
]


@pytest.mark.parametrize("reason", DESIGNED)
def test_designed_refusal_records_and_exits_zero(tmp_path: Path, reason: str) -> None:
    """Regression: every designed stop exited 1, so Claude On Failure opened
    a "CI failure" issue for each (#26, #29-#31, #33, #34)."""
    result, calls = _run(tmp_path, reason)
    assert result.returncode == 0, result.stderr
    assert "::notice::" in result.stdout and "::error::" not in result.stdout
    assert f'"reason":"{reason}"' in calls.read_text()
    # It tries to close the fork PR (no token here, so only a warning).
    assert "close-ingestion-pr: no GH_TOKEN" in result.stdout


@pytest.mark.parametrize(
    "reason",
    ["apply_failed", "reapply_mismatch", "resolution_incomplete", "path_policy_error",
     "not_exactly_one_commit", "cherry_pick_conflict", "some_new_reason", "Not-A-Reason"],
)
def test_any_other_reason_fails_red_and_stays_retryable(tmp_path: Path, reason: str) -> None:
    """Review finding: real failures (resolver outage, bad artifact, path
    policy error) ended green and terminal. Now only the allowlist is green;
    everything else fails, writes no ledger event and closes no PR, so
    pr-discovery retries the pair."""
    result, calls = _run(tmp_path, reason)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert not calls.exists()
    assert "close-ingestion-pr" not in result.stdout


def test_every_refusal_reason_the_scripts_write_is_classified() -> None:
    """Each reason apply-upstream-pr.sh and finish-ingestion-branch.sh can
    refuse with is either a designed refusal or a known failure."""
    failures = {"apply_failed", "not_exactly_one_commit", "path_policy_error"}
    found: set[str] = set()
    for name in ("apply-upstream-pr.sh", "finish-ingestion-branch.sh"):
        text = (REPO_ROOT / "tools" / name).read_text()
        found |= set(re.findall(r"^\s*refuse ([a-z_]+)\s*$", text, re.M))
        found |= set(re.findall(r"REASON=([a-z_]+)", text))
    assert found, "no refusal reasons found"
    assert found <= set(DESIGNED) | failures, found - set(DESIGNED) - failures
    script = SCRIPT.read_text()
    for reason in DESIGNED:
        assert f"  {reason} " in script
