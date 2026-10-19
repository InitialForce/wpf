"""Tests for tools/path-policy.py, the path rules shared by ingestion,
nightly-rebase and automerge-verify.sh."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "path-policy.py"

_spec = importlib.util.spec_from_file_location("path_policy", TOOL)
assert _spec and _spec.loader
path_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(path_policy)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/x.yml",
        ".if-fork/patch-ledger.jsonl",
        "tools/ledger-event.py",
        "tests/conftest.py",
        "test/InitialForce.WpfSmoke/Perf/PerfHarness.cs",
        "docs/bot-gpg-pubkey.asc",
        "perf/baseline.json",
        "data/x.json",
        "packaging/InitialForce.WPF/InitialForce.WPF.csproj",
        "FORK.md",
        "NOTICE.md",
        "pyproject.toml",
        "src/Microsoft.DotNet.Wpf/CLAUDE.md",
        "src/a/.claude/settings.json",
        "AGENTS.md",
        "src/.mcp.json",
        "CLAUDE.local.md",
    ],
)
@pytest.mark.parametrize("kind", ["cherry-pick", "sync"])
def test_fork_owned_paths_fail_for_both_kinds(path: str, kind: str) -> None:
    result = path_policy.check([path], kind)
    assert result["ok"] is False
    assert result["reason"] == "fork_owned_paths"
    assert result["fork_owned"] == [path]


@pytest.mark.parametrize(
    "path",
    ["README.md", "Directory.Build.props", "packaging/Directory.Build.props", "samples/a.cs"],
)
def test_cherry_pick_outside_src_and_eng_fails(path: str) -> None:
    result = path_policy.check(["src/ok.cs", path], "cherry-pick")
    assert result["reason"] == "paths_outside_allowlist"
    assert result["outside_allowlist"] == [path]


@pytest.mark.parametrize("path", ["README.md", "Directory.Build.props", "samples/a.cs"])
def test_sync_may_change_upstream_paths_outside_src_and_eng(path: str) -> None:
    assert path_policy.check([path], "sync") == {
        "ok": True, "reason": "", "fork_owned": [], "outside_allowlist": []
    }


def test_src_and_eng_pass_for_a_cherry_pick() -> None:
    assert path_policy.check(["src/a.cs", "eng/Build.props"], "cherry-pick")["ok"] is True


def test_fork_owned_wins_when_both_rules_hit() -> None:
    result = path_policy.check(["tools/x.py", "README.md"], "cherry-pick")
    assert result["reason"] == "fork_owned_paths"
    assert result["outside_allowlist"] == ["tools/x.py", "README.md"]


def test_lookalike_paths_are_not_fork_owned() -> None:
    # Only top-level folders are fork-owned; deeper folders of the same name are upstream's.
    paths = ["src/tools/a.cs", "src/docs/b.md", "src/test/c.cs", "eng/common/tools.ps1"]
    assert path_policy.check(paths, "cherry-pick")["ok"] is True


def test_matches_fork_owned_part_of_config_denylist() -> None:
    # Every fork-owned path must also be on file_denylist, so ingestion's
    # denylist check and this rule agree on them.
    import yaml

    config = yaml.safe_load((REPO_ROOT / ".if-fork" / "config.yaml").read_text())
    deny = " ".join(config["file_denylist"])
    for prefix in (".github/", ".if-fork/", "tools/", "tests/", "test/", "docs/", "perf/"):
        assert prefix in deny, prefix


def _cli(tmp_path: Path, kind: str, lines: str) -> subprocess.CompletedProcess[str]:
    files = tmp_path / "changed.txt"
    files.write_text(lines)
    return subprocess.run(
        [sys.executable, str(TOOL), "--kind", kind, "--changed-files", str(files)],
        capture_output=True,
        text=True,
    )


def test_cli_exit_codes(tmp_path: Path) -> None:
    ok = _cli(tmp_path, "cherry-pick", "src/a.cs\n\n")
    assert ok.returncode == 0 and json.loads(ok.stdout)["ok"] is True
    bad = _cli(tmp_path, "cherry-pick", "src/a.cs\nREADME.md\n")
    assert bad.returncode == 2
    assert json.loads(bad.stdout)["reason"] == "paths_outside_allowlist"


def test_cli_missing_file_is_bad_input(tmp_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(TOOL), "--kind", "sync", "--changed-files", str(tmp_path / "no")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1 and json.loads(r.stdout)["ok"] is False
