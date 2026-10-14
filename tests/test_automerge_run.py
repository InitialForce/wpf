"""Tests for tools/automerge-run.sh with a stub gh and a stub verifier.

The stub gh answers from files and logs every call. The stub
automerge-verify.sh returns a canned result per PR and can push to if/main
while it "verifies", to check that a moved if/main stops the merge.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "automerge-run.sh"
REPO = "InitialForce/wpf"
BOT = "if-wpf-bot[bot]"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not available")

GH_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB/gh.log"
case "$1 $2" in
  "pr list") cat "$STUB/candidates" ;;
  "pr merge") echo "$3" >> "$STUB/merged" ;;
  "pr comment") printf '%s\n' "$7" >> "$STUB/comments-$3" ;;
  "api "*)
    case "$2" in
      */pulls/*) cat "$STUB/pr-${2##*/}.json" ;;
      */actions/workflows/build.yml/runs*)
        sha="${2##*head_sha=}"; sha="${sha%%&*}"; cat "$STUB/build-$sha" 2>/dev/null ;;
      */issues/*/comments) n="${2%/comments}"; cat "$STUB/comments-${n##*/}" 2>/dev/null ;;
      */dispatches) jq -c . >> "$STUB/dispatches" ;;
    esac ;;
esac
"""

VERIFY_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$OUT"
cat "$STUB/verify-$PR" > "$OUT/result"
echo "stub reason" > "$OUT/reason"
echo 77 > "$OUT/upstream_pr"
echo "$(printf 'b%.0s' {1..40})" > "$OUT/upstream_head"
if [ -f "$STUB/move-main-$PR" ]; then
  git -C "$STUB/origin" commit -q --allow-empty -m "moved"
fi
"""


def git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def setup(tmp_path: Path):
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = T\n\temail = t@example.com\n"
        "[init]\n\tdefaultBranch = if/main\n[commit]\n\tgpgsign = false\n"
    )
    stub = tmp_path / "stub"
    stub.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(cfg),
        "GIT_CONFIG_NOSYSTEM": "1",
        "STUB": str(stub),
    }
    origin = stub / "origin"
    origin.mkdir()
    git(origin, env, "init", "-q")
    git(origin, env, "commit", "-q", "--allow-empty", "-m", "base")
    work = tmp_path / "work"
    git(tmp_path, env, "clone", "-q", str(origin), str(work))
    git(work, env, "remote", "add", "upstream", str(origin))
    git(origin, env, "branch", "release/10.0")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(GH_STUB)
    (bin_dir / "gh").chmod(0o755)
    trusted = tmp_path / "trusted"
    (trusted / "tools").mkdir(parents=True)
    (trusted / "tools" / "automerge-verify.sh").write_text(VERIFY_STUB)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.update(
        GH_TOKEN="t",
        GITHUB_REPOSITORY=REPO,
        BOT_LOGIN=BOT,
        BOT_FPR="f" * 40,
        TRUSTED=str(trusted),
        OUT=str(tmp_path / "out"),
        PROCEED="true",
        GATE_REASON="ok",
    )
    return stub, work, env


def add_pr(
    stub: Path,
    number: int,
    verify: str,
    build: str = "success",
    branch: str | None = None,
    user: str = BOT,
) -> str:
    sha = f"{number:x}" * 40
    sha = sha[:40]
    branch = branch or f"claude/cherry-pick-pr-{number + 100}"
    with (stub / "candidates").open("a") as fh:
        fh.write(f"{number} {sha} {branch}\n")
    (stub / f"pr-{number}.json").write_text(
        json.dumps(
            {
                "state": "open",
                "base": {"ref": "if/main"},
                "head": {"repo": {"full_name": REPO}, "sha": sha},
                "user": {"login": user},
                "labels": [{"name": "automated"}],
            }
        )
    )
    (stub / f"build-{sha}").write_text(build + "\n")
    (stub / f"verify-{number}").write_text(verify + "\n")
    return sha


def run(work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT)], cwd=work, env=env, capture_output=True, text=True)


def lines(path: Path) -> list[str]:
    return path.read_text().split() if path.exists() else []


def test_merges_ok_prs_and_skips_others(setup) -> None:
    stub, work, env = setup
    add_pr(stub, 1, "ok")
    add_pr(stub, 2, "ok", build="failure")
    add_pr(stub, 3, "ok", user="someone")
    add_pr(stub, 4, "ok", branch="feature/x")
    r = run(work, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines(stub / "merged") == ["1"]
    merge_call = next(c for c in (stub / "gh.log").read_text().splitlines() if "pr merge" in c)
    assert f"--merge --match-head-commit {'1' * 40}" in merge_call


def test_moved_if_main_stops_the_merge(setup) -> None:
    stub, work, env = setup
    add_pr(stub, 1, "ok")
    (stub / "move-main-1").touch()
    r = run(work, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines(stub / "merged") == []
    assert "moved" in r.stdout


def test_frozen_gate_does_not_merge_or_rebuild(setup) -> None:
    stub, work, env = setup
    add_pr(stub, 1, "ok")
    add_pr(stub, 2, "behind")
    r = run(work, {**env, "PROCEED": "false"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert lines(stub / "merged") == []
    assert not (stub / "dispatches").exists()


def test_behind_pr_is_rebuilt_once_per_head(setup) -> None:
    stub, work, env = setup
    add_pr(stub, 1, "behind")
    add_pr(stub, 2, "behind", branch="if/staging")
    assert run(work, env).returncode == 0
    assert run(work, env).returncode == 0
    sent = [json.loads(x) for x in (stub / "dispatches").read_text().split("\n") if x.strip()]
    assert len(sent) == 2
    assert sent[0]["event_type"] == "pr-ingestion-requested"
    assert sent[0]["client_payload"] == {"pr_number": 77, "head_sha": "b" * 40}
    assert sent[1] == {"event_type": "nightly-rebase", "client_payload": {"force": True}}


def test_refusal_is_reported_once_per_head(setup) -> None:
    stub, work, env = setup
    sha = add_pr(stub, 1, "refuse")
    assert run(work, env).returncode == 0
    assert run(work, env).returncode == 0
    text = (stub / "comments-1").read_text()
    assert text.count(f"<!-- automerge-refused:{sha} -->") == 1
    assert lines(stub / "merged") == []


def test_one_broken_pr_does_not_stop_the_others(setup) -> None:
    stub, work, env = setup
    add_pr(stub, 1, "nonsense")
    add_pr(stub, 2, "ok")
    r = run(work, env)
    assert r.returncode == 1
    assert lines(stub / "merged") == ["2"]
