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
