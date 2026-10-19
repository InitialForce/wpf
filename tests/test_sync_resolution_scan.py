"""Regression tests for the sync resolution hard-fail scan, on real git merges.

The record job scans git's automatic merge -> the result (`scan.diff`) with
upstream's own diff for context (record-resolution-verdict.py
--sync-upstream-diff). A resolution may discard upstream's or the base's side
of a conflict region, but it must not silently drop fork protections, inside
or outside the conflict region (Opus M3, Astra 1).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
TOOL = REPO_ROOT / "tools" / "record-resolution-verdict.py"
CONFIG = REPO_ROOT / ".if-fork" / "config.yaml"
PATTERNS = yaml.safe_load(CONFIG.read_text())["review_hard_fail_patterns"]
REVIEWER = "claude-fable-5-1-resolution-review"

_spec = importlib.util.spec_from_file_location("record_resolution_verdict", TOOL)
assert _spec is not None and _spec.loader is not None
rrv: Any = importlib.util.module_from_spec(_spec)
sys.modules["record_resolution_verdict"] = rrv
_spec.loader.exec_module(rrv)

BASE = """class A
{
    void F()
    {
        permission.Demand();
        Work();
        Step1();
        Step2();
        Step3();
    }

    int x = 1;
}
"""


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    r = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    assert r.returncode in (0, 1), r.stderr
    return r.stdout


def _commit(repo: Path, text: str, msg: str) -> str:
    (repo / "a.cs").write_text(text)
    _git(repo, "add", "a.cs")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def _scan(tmp_path: Path, base: str, fork: str, upstream: str, result: str) -> list[str]:
    """Merge fork and upstream as nightly-merge does, resolve to *result*, scan."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    mb = _commit(repo, base, "base")
    up = _commit(repo, upstream, "upstream")
    _git(repo, "checkout", "-q", "-b", "fork", mb)
    fork_sha = _commit(repo, fork, "fork")
    auto = _git(repo, "merge-tree", "--write-tree", fork_sha, up).splitlines()[0]
    assert "<<<<<<<" in _git(repo, "show", f"{auto}:a.cs"), "the fixture must conflict"
    branch = _commit(repo, result, "resolution")
    scan = _git(repo, "diff", "--no-color", "--no-renames", auto, branch)
    up_diff = _git(repo, "diff", "--no-color", "--no-renames", mb, up)
    (tmp_path / "scan.diff").write_text(scan)
    (tmp_path / "upstream.diff").write_text(up_diff)
    return [h["pattern"] for h in rrv.hard_fail_hits(scan, PATTERNS, up_diff)]


FORK = BASE.replace("int x = 1;", "int x = 2;")
UPSTREAM = BASE.replace("int x = 1;", "int x = 3;\n    unsafe { Fast(); }")


def test_dropping_a_fork_demand_outside_the_conflict_hunk_is_caught(tmp_path: Path) -> None:
    # The conflict is on "int x"; the resolution also deletes the Demand()
    # line several lines away, in merged content.
    result = FORK.replace("        permission.Demand();\n", "")
    assert r"Demand\(\)" in _scan(tmp_path, BASE, FORK, UPSTREAM, result)


def test_discarding_the_upstream_alternative_is_allowed(tmp_path: Path) -> None:
    # Taking the fork side drops upstream's "unsafe" line from the region.
    assert _scan(tmp_path, BASE, FORK, UPSTREAM, FORK) == []


def test_keeping_both_sides_is_allowed(tmp_path: Path) -> None:
    # Upstream's own "unsafe" line stays: it is upstream content, shown as
    # context in the scan diff, not something the resolution wrote.
    result = FORK.replace("int x = 2;", "int x = 2;\n    unsafe { Fast(); }")
    assert _scan(tmp_path, BASE, FORK, UPSTREAM, result) == []


def test_adding_a_new_matching_line_is_caught(tmp_path: Path) -> None:
    result = FORK.replace("int x = 2;", 'int x = 2;\n    Process.Start("x");')
    assert r"Process\.Start" in _scan(tmp_path, BASE, FORK, UPSTREAM, result)


def test_dropping_a_fork_protection_inside_the_region_is_caught(tmp_path: Path) -> None:
    fork = BASE.replace("    int x = 1;", "    [SecurityCritical]\n    int x = 2;")
    upstream = BASE.replace("int x = 1;", "int x = 3;")
    result = BASE.replace("int x = 1;", "int x = 3;")
    assert r"\[SecurityCritical\]\s*$" in _scan(tmp_path, BASE, fork, upstream, result)


def test_dropping_a_fork_line_upstream_also_removed_is_allowed(tmp_path: Path) -> None:
    base = BASE.replace("    int x = 1;", "    [SecurityCritical]\n    int x = 1;")
    fork = base.replace("int x = 1;", "int x = 2;")
    upstream = BASE.replace("int x = 1;", "int x = 3;")  # upstream removed the attribute
    result = upstream
    assert _scan(tmp_path, base, fork, upstream, result) == []


def test_record_tool_rejects_the_outside_drop_end_to_end(tmp_path: Path) -> None:
    result = FORK.replace("        permission.Demand();\n", "")
    _scan(tmp_path, BASE, FORK, UPSTREAM, result)
    verdict = tmp_path / "resolution.json"
    verdict.write_text(
        json.dumps(
            {
                "verdict": "approve",
                "confidence": 0.99,
                "novel_lines_justified": True,
                "reviewer_id": REVIEWER,
            }
        )
    )
    novel = tmp_path / "novel.json"
    novel.write_text(json.dumps({"novel_count": 0, "dropped_count": 1}))
    out = tmp_path / "decision.json"
    r = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--kind",
            "sync",
            "--sync-upstream-diff",
            str(tmp_path / "upstream.diff"),
            "--verdict",
            str(verdict),
            "--reviewer-id",
            REVIEWER,
            "--verdict-sha256",
            hashlib.sha256(verdict.read_bytes()).hexdigest(),
            "--upload",
            "success",
            "--config",
            str(CONFIG),
            "--final-diff",
            str(tmp_path / "scan.diff"),
            "--novel-lines",
            str(novel),
            "--novel-sha256",
            hashlib.sha256(novel.read_bytes()).hexdigest(),
            "--pr-number",
            "0",
            "--head-sha",
            "a" * 40,
            "--branch-sha",
            "c" * 40,
            "--branch",
            "if/staging",
            "--output",
            str(out),
            "--events-out",
            str(tmp_path / "events.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1, r.stderr
    assert any("Demand" in x for x in json.loads(out.read_text())["reasons"])


@pytest.mark.parametrize("kind_args", [["--kind", "sync"], ["--sync-upstream-diff", "x"]])
def test_sync_and_its_upstream_diff_go_together(tmp_path: Path, kind_args: list[str]) -> None:
    r = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            *kind_args,
            "--verdict",
            "v",
            "--reviewer-id",
            "r",
            "--config",
            str(CONFIG),
            "--final-diff",
            "f",
            "--novel-lines",
            "n",
            "--pr-number",
            "0" if "sync" in kind_args else "7",
            "--head-sha",
            "a" * 40,
            "--branch-sha",
            "c" * 40,
            "--branch",
            "b",
            "--output",
            str(tmp_path / "o"),
            "--events-out",
            str(tmp_path / "e"),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 3
