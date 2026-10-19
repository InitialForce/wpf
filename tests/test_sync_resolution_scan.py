"""Regression tests for the sync resolution hard-fail scan, on real git merges.

The record job scans git's own merge (diff3 markers) -> the result, with
full context (tools/resolution-scope.py --full-context). A resolution may
discard upstream's or the base's side of a conflict region, but it must not
silently drop fork protections, inside or outside the conflict region (Opus
M3, Astra 1). A dropped fork line is excused only when its own region shows
upstream removed it (Astra re-review).
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

import yaml

REPO_ROOT = Path(__file__).parent.parent
TOOL = REPO_ROOT / "tools" / "record-resolution-verdict.py"
SCOPE = REPO_ROOT / "tools" / "resolution-scope.py"
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


ENV = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, env=ENV, capture_output=True, text=True)
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
    _git(repo, "checkout", "-q", "-b", "merge", fork_sha)
    _git(repo, "merge", "-q", "--no-commit", up)
    assert "<<<<<<<" in (repo / "a.cs").read_text(), "the fixture must conflict"
    branch = _commit(repo, result, "resolution")  # a merge of fork and upstream
    # The record job's own scope tool, as resolution-review.yml runs it.
    r = subprocess.run(
        [sys.executable, str(SCOPE), "--kind", "sync", "--base-sha", fork_sha,
         "--upstream-sha", up, "--branch-sha", branch, "--out", str(tmp_path / "scope"),
         "--full-context"],
        cwd=repo, env=ENV, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    scan = (tmp_path / "scope" / "resolution-full.diff").read_text()
    (tmp_path / "scan.diff").write_text(scan)
    return [h["pattern"] for h in rrv.hard_fail_hits(scan, PATTERNS, sync=True)]


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


ASTRA_BASE = """class A
{
    void MethodA()
    {
        permission.Demand();
        A1();
    }

    void MethodB()
    {
        int y = 1;
    }
}
"""


def test_upstream_removal_elsewhere_does_not_excuse_a_dropped_fork_demand(
    tmp_path: Path,
) -> None:
    # Astra re-review, exact scenario: upstream removes Demand() from
    # MethodA; the fork adds a Demand() in MethodB, where the conflict is.
    # The resolution drops B's Demand(). A's removal must not excuse it.
    upstream = ASTRA_BASE.replace("        permission.Demand();\n        A1();", "        A1();")
    upstream = upstream.replace("int y = 1;", "int y = 3;")
    fork = ASTRA_BASE.replace(
        "        int y = 1;", "        permission.Demand();\n        int y = 2;"
    )
    result = upstream
    assert r"Demand\(\)" in _scan(tmp_path, ASTRA_BASE, fork, upstream, result)


def test_the_merge_uses_diff3_markers(tmp_path: Path) -> None:
    # The excuse needs each region's base side; without it nothing is excused.
    fork = BASE.replace("    int x = 1;", "    [SecurityCritical]\n    int x = 2;")
    upstream = BASE.replace("int x = 1;", "int x = 3;")
    _scan(tmp_path, BASE, fork, upstream, upstream)
    assert "-|||||||" in (tmp_path / "scan.diff").read_text()


def test_a_region_without_a_base_side_excuses_nothing() -> None:
    diff = (
        "diff --git a/a.cs b/a.cs\n--- a/a.cs\n+++ b/a.cs\n@@ -1,4 +1,1 @@\n"
        "-<<<<<<< ours\n-    [SecurityCritical]\n-=======\n->>>>>>> theirs\n"
    )
    assert rrv.hard_fail_hits(diff, PATTERNS, sync=True)


DUP_BASE = """class A
{
    void F()
    {
        permission.Demand();
        A();
        B();
    }
}
"""


def test_dropping_a_fork_added_copy_is_caught_despite_an_upstream_removal(
    tmp_path: Path,
) -> None:
    # Astra round 3, exact scenario: the fork changes both calls and adds a
    # second Demand() before B; upstream removes the original Demand() and
    # changes both calls (one region). The resolution keeps the original
    # Demand() and drops the fork-added one. The fork side has 2 copies and
    # the base side 1, so the upstream removal excuses nothing.
    fork = DUP_BASE.replace("        A();\n        B();",
                            "        A2();\n        permission.Demand();\n        B2();")
    upstream = DUP_BASE.replace("        permission.Demand();\n        A();\n        B();",
                                "        A3();\n        B3();")
    result = DUP_BASE.replace("        A();\n        B();", "        A2();\n        B2();")
    hits = _scan(tmp_path, DUP_BASE, fork, upstream, result)
    assert "        permission.Demand();" in (tmp_path / "scan.diff").read_text()
    assert r"Demand\(\)" in hits


def test_dropping_the_copy_upstream_removed_is_allowed_when_counts_match(
    tmp_path: Path,
) -> None:
    # Fork side and base side each hold one Demand(); upstream removed it in
    # this region; the resolution drops it, as upstream did.
    fork = DUP_BASE.replace("        A();\n        B();", "        A2();\n        B2();")
    upstream = DUP_BASE.replace("        permission.Demand();\n        A();\n        B();",
                                "        A3();\n        B3();")
    result = DUP_BASE.replace("        permission.Demand();\n        A();\n        B();",
                              "        A2();\n        B3();")
    assert _scan(tmp_path, DUP_BASE, fork, upstream, result) == []
