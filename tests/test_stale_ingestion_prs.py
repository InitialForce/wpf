"""Tests for tools/stale-ingestion-prs.py and tools/close-ingestion-pr.sh."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "stale-ingestion-prs.py"
CLOSE = REPO_ROOT / "tools" / "close-ingestion-pr.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stale_ingestion_prs", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sip = _load()


def _ev(event: str, pr: int, **details: Any) -> dict[str, Any]:
    return {"event": event, "pr_number": pr, "head_sha": "a" * 40, "details": details}


def _pr(number: int, upstream: int, cross: bool = False) -> dict[str, Any]:
    return {"number": number, "headRefName": f"claude/cherry-pick-pr-{upstream}",
            "isCrossRepository": cross}


def test_escalated_upstream_pr_leaves_a_stale_fork_pr() -> None:
    """Regression: PR #27 (upstream #9215) stayed open with needs-human-merge
    after ingestion escalated the upstream PR."""
    ledger = [
        _ev("approved", 9215),
        _ev("ingestion_requested", 9215),
        _ev("cherry_picked", 9215),
        _ev("escalated", 9215, reason="cherry_pick_conflict"),
    ]
    assert sip.stale_prs(ledger, [_pr(27, 9215)]) == [
        {"fork_pr": 27, "upstream_pr": 9215, "reason": "escalated (cherry_pick_conflict)"}
    ]


def test_graduated_and_rejected_are_stale_too() -> None:
    ledger = [_ev("graduated_upstream", 1, detection_method="empty_resolution",
                  reason="fork_superseded"), _ev("rejected", 2)]
    stale = sip.stale_prs(ledger, [_pr(10, 1), _pr(11, 2)])
    assert [(s["fork_pr"], s["reason"]) for s in stale] == [
        (10, "graduated_upstream (fork_superseded)"), (11, "rejected")
    ]


def test_active_ingestion_is_not_stale() -> None:
    """A later approval, request or resolution verdict means a run is still
    working on the PR (for example a retry after a rejected resolution)."""
    for later in ("approved", "ingestion_requested", "cherry_picked",
                  "resolution_approved", "resolution_rejected"):
        ledger = [_ev("escalated", 5, reason="x"), _ev(later, 5)]
        assert sip.stale_prs(ledger, [_pr(12, 5)]) == [], later
    assert sip.stale_prs([_ev("cherry_picked", 6)], [_pr(13, 6)]) == []


def test_a_fork_branch_with_the_same_name_is_ignored() -> None:
    ledger = [_ev("escalated", 9215, reason="cherry_pick_conflict")]
    assert sip.stale_prs(ledger, [_pr(50, 9215, cross=True)]) == []
    unknown = {"number": 51, "headRefName": "claude/cherry-pick-pr-9215"}
    assert sip.stale_prs(ledger, [unknown]) == []


def test_only_ingestion_branches_count() -> None:
    ledger = [_ev("escalated", 0, reason="sync")]
    prs = [{"number": 32, "headRefName": "if/staging"},
           {"number": 33, "headRefName": "claude/cherry-pick-pr-x"}]
    assert sip.stale_prs(ledger, prs) == []


def test_cli(tmp_path: Path) -> None:
    (tmp_path / "ledger.jsonl").write_text(
        json.dumps(_ev("escalated", 7, reason="file_denylist")) + "\n"
    )
    (tmp_path / "open.json").write_text(json.dumps([_pr(40, 7)]))
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(tmp_path / "ledger.jsonl"),
         "--open-prs", str(tmp_path / "open.json")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)[0]["fork_pr"] == 40
    (tmp_path / "open.json").write_text('{"not": "a list"}')
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(tmp_path / "ledger.jsonl"),
         "--open-prs", str(tmp_path / "open.json")],
        capture_output=True, text=True,
    )
    assert bad.returncode == 2


BOT = "initial-force-wpf-bot[bot]"
HEAD = "c" * 40


def _pr_json(**over: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "state": "open",
        "base": {"ref": "if/main"},
        "head": {"ref": "claude/cherry-pick-pr-9215", "sha": HEAD,
                 "repo": {"full_name": "InitialForce/wpf"}},
        "user": {"login": BOT},
        "labels": [{"name": "automated"}, {"name": "needs-human-merge"}],
    }
    pr.update(over)
    return pr


def _close(
    tmp_path: Path, pr: dict[str, Any] | None, ref_sha: str = HEAD, bot: str | None = BOT
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run close-ingestion-pr.sh with a gh stub that knows PR #27."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh-calls.txt"
    (tmp_path / "pr.json").write_text(json.dumps(pr or {}))
    listed = "27\\n" if pr else ""
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$1 $2" in\n'
        f'  "pr list") printf "{listed}" ;;\n'
        f'  "api repos/InitialForce/wpf/pulls/27") cat "{tmp_path / "pr.json"}" ;;\n'
        '  "api repos/InitialForce/wpf/git/ref/heads/claude/cherry-pick-pr-9215")'
        f" echo {ref_sha} ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "GH_TOKEN": "x", "GITHUB_REPOSITORY": "InitialForce/wpf", "GITHUB_RUN_ID": "5"}
    env.pop("BOT_LOGIN", None)
    if bot is not None:
        env["BOT_LOGIN"] = bot
    r = subprocess.run(
        ["bash", str(CLOSE), "9215", "escalated (cherry_pick_conflict)"],
        capture_output=True, text=True, env=env,
    )
    return r, calls.read_text() if calls.exists() else ""


def test_close_removes_label_comments_closes_and_deletes_branch(tmp_path: Path) -> None:
    r, calls = _close(tmp_path, _pr_json())
    assert r.returncode == 0, r.stderr
    assert "--head claude/cherry-pick-pr-9215" in calls
    assert "pr edit 27 --repo InitialForce/wpf --remove-label needs-human-merge" in calls
    close = next(line for line in calls.splitlines() if line.startswith("pr close 27"))
    assert "cherry_pick_conflict" in close and "--delete-branch" not in close
    assert "api -X DELETE repos/InitialForce/wpf/git/refs/heads/claude/cherry-pick-pr-9215" in calls


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"head": {"ref": "claude/cherry-pick-pr-9215", "sha": HEAD,
                   "repo": {"full_name": "someone/wpf"}}}, "head is not in"),
        ({"user": {"login": "someone"}}, "opened by someone"),
        ({"labels": [{"name": "needs-human-merge"}]}, "no automated label"),
        ({"base": {"ref": "main"}}, "base is not if/main"),
    ],
)
def test_close_leaves_prs_that_are_not_the_pipelines(
    tmp_path: Path, override: dict[str, Any], why: str
) -> None:
    """Review finding: a fork's branch of the same name, or a PR a person
    opened, must never be closed or have its label or branch touched."""
    r, calls = _close(tmp_path, _pr_json(**override))
    assert r.returncode == 0
    assert why in r.stdout
    assert "pr close" not in calls and "pr edit" not in calls and "DELETE" not in calls


def test_close_keeps_a_branch_that_moved(tmp_path: Path) -> None:
    r, calls = _close(tmp_path, _pr_json(), ref_sha="d" * 40)
    assert "pr close 27" in calls
    assert "DELETE" not in calls
    assert "Kept claude/cherry-pick-pr-9215" in r.stdout


def test_close_reads_the_bot_login_from_config(tmp_path: Path) -> None:
    r, calls = _close(tmp_path, _pr_json(), bot=None)
    assert "pr close 27" in calls, r.stdout
    config = yaml.safe_load((REPO_ROOT / ".if-fork" / "config.yaml").read_text())
    assert config["bot_identity"]["github_login"] == BOT


def test_close_with_nothing_open_does_nothing(tmp_path: Path) -> None:
    r, calls = _close(tmp_path, None)
    assert r.returncode == 0
    assert "pr close" not in calls


def test_close_never_fails_without_a_token(tmp_path: Path) -> None:
    r = subprocess.run(
        ["bash", str(CLOSE), "1", "x"], capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if k != "GH_TOKEN"},
    )
    assert r.returncode == 0
    assert "::warning::" in r.stdout


def test_every_ingestion_stop_closes_the_fork_pr() -> None:
    """Escalated, graduated (pre-flight, empty pick, fork-only resolution) and
    SHA-smuggling stops all close the PR; escalations do it in
    ingestion-escalate.sh."""
    text = (WORKFLOWS / "pr-ingestion.yml").read_text()
    assert text.count("close-ingestion-pr.sh") == 4
    assert "close-ingestion-pr.sh" in (REPO_ROOT / "tools" / "ingestion-escalate.sh").read_text()
    doc = yaml.safe_load(text)
    for job in ("cherry-pick", "finalize"):
        for step in doc["jobs"][job]["steps"]:
            run = step.get("run", "")
            if "ingestion-escalate.sh" in run or "close-ingestion-pr.sh" in run:
                assert step["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"


def test_discovery_sweeps_stale_prs_without_failing() -> None:
    doc = yaml.safe_load((WORKFLOWS / "pr-discovery.yml").read_text())
    steps = [s for job in doc["jobs"].values() for s in job.get("steps", [])]
    sweep = next(s for s in steps if "stale-ingestion-prs.py" in s.get("run", ""))
    assert sweep["continue-on-error"] is True
    assert "isCrossRepository" in sweep["run"]
    assert "close-ingestion-pr.sh" in sweep["run"]
