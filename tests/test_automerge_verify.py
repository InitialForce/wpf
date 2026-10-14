"""Tests for tools/automerge-verify.sh, run for real on throwaway git repos.

A throwaway GPG key stands in for the bot key. The trusted directory holds
the real tools and config from this repository plus that key's public half.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "automerge-verify.sh"

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not available")

UPSTREAM_HEAD = "b" * 40


def run(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        list(args), cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def bot_key(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, str]]:
    home = tmp_path_factory.mktemp("gnupg")
    home.chmod(0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    run(
        home,
        env,
        "gpg",
        "--batch",
        "--passphrase",
        "",
        "--quick-gen-key",
        "Test Bot <bot@example.com>",
        "ed25519",
        "sign",
        "never",
    )
    colons = run(home, env, "gpg", "--batch", "--with-colons", "--fingerprint")
    fpr = next(line.split(":")[9] for line in colons.splitlines() if line.startswith("fpr"))
    yield home, fpr
    # gpg autostarts a gpg-agent and scdaemon per homedir; stop them or they outlive pytest.
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=False)


@pytest.fixture
def env(tmp_path: Path, bot_key: tuple[Path, str]) -> dict[str, str]:
    home, fpr = bot_key
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Bot\n\temail = bot@example.com\n"
        f"\tsigningkey = {fpr}\n[init]\n\tdefaultBranch = main\n"
    )
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(cfg),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GNUPGHOME": str(home),
    }


@pytest.fixture
def trusted(tmp_path: Path, env: dict[str, str], bot_key: tuple[Path, str]) -> Path:
    t = tmp_path / "trusted"
    (t / ".if-fork").mkdir(parents=True)
    (t / "docs").mkdir()
    (t / "tools").symlink_to(REPO_ROOT / "tools")
    shutil.copy(REPO_ROOT / ".if-fork" / "config.yaml", t / ".if-fork" / "config.yaml")
    pub = run(t, env, "gpg", "--batch", "--armor", "--export", bot_key[1])
    (t / "docs" / "bot-gpg-pubkey.asc").write_text(pub + "\n")
    return t


def commit(
    repo: Path, env: dict[str, str], files: dict[str, str], msg: str, sign: bool = True
) -> str:
    for path, text in files.items():
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        run(repo, env, "git", "add", path)
    run(repo, env, "git", "commit", "-q", "-S" if sign else "--no-gpg-sign", "-m", msg)
    return run(repo, env, "git", "rev-parse", "HEAD")


def ledger(approved: bool) -> str:
    event = "approved" if approved else "rejected"
    return json.dumps({"event": event, "pr_number": 77, "head_sha": UPSTREAM_HEAD}) + "\n"


@pytest.fixture
def origin(tmp_path: Path, env: dict[str, str]) -> Path:
    """Origin repo with if/main holding an approved ledger entry for #77."""
    o = tmp_path / "origin"
    o.mkdir()
    run(o, env, "git", "init", "-q")
    commit(o, env, {"src/a.cs": "a\n", ".if-fork/patch-ledger.jsonl": ledger(True)}, "base")
    run(o, env, "git", "branch", "-m", "if/main")
    return o


def make_pick(
    origin: Path,
    env: dict[str, str],
    files: dict[str, str],
    msg: str | None = None,
    sign: bool = True,
) -> str:
    run(origin, env, "git", "checkout", "-q", "-b", "claude/cherry-pick-pr-77", "if/main")
    msg = msg or (
        "pick\n\nUpstream-PR: https://github.com/dotnet/wpf/pull/77\n"
        f"Upstream-PR-Head: {UPSTREAM_HEAD}\n"
    )
    sha = commit(origin, env, files, msg, sign=sign)
    run(origin, env, "git", "update-ref", "refs/pull/5/head", sha)
    run(origin, env, "git", "checkout", "-q", "if/main")
    return sha


def verify(
    tmp_path: Path,
    env: dict[str, str],
    origin: Path,
    trusted: Path,
    sha: str,
    branch: str,
    fpr: str,
    upstream: Path | None = None,
) -> tuple[str, str]:
    work = tmp_path / "work"
    if not work.exists():
        run(tmp_path, env, "git", "clone", "-q", "-b", "if/main", str(origin), str(work))
        if upstream is not None:
            run(work, env, "git", "remote", "add", "upstream", str(upstream))
            run(work, env, "git", "fetch", "-q", "upstream", "release/10.0")
    else:
        run(work, env, "git", "fetch", "-q", "origin")
    out = tmp_path / "out"
    shutil.rmtree(out, ignore_errors=True)
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=work,
        capture_output=True,
        text=True,
        env={
            **env,
            "PR": "5",
            "HEAD_SHA": sha,
            "HEAD_BRANCH": branch,
            "BOT_FPR": fpr,
            "TRUSTED": str(trusted),
            "OUT": str(out),
        },
    )
    assert r.returncode == 0, r.stderr
    reason = (out / "reason").read_text().strip() if (out / "reason").exists() else ""
    return (out / "result").read_text().strip(), reason


BRANCH = "claude/cherry-pick-pr-77"


def test_cherry_pick_ok(tmp_path, env, origin, trusted, bot_key) -> None:
    sha = make_pick(origin, env, {"src/b.cs": "b\n"})
    assert verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1]) == ("ok", "")


def test_conflict_resolved_cherry_pick_needs_a_person(
    tmp_path, env, origin, trusted, bot_key
) -> None:
    msg = (
        "pick\n\nUpstream-PR: https://github.com/dotnet/wpf/pull/77\n"
        f"Upstream-PR-Head: {UPSTREAM_HEAD}\nConflict-Resolved-By: claude\n"
    )
    sha = make_pick(origin, env, {"src/b.cs": "b\n"}, msg=msg)
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "refuse" and "needs-human-merge" in reason


def test_wrong_fingerprint_refused(tmp_path, env, origin, trusted) -> None:
    sha = make_pick(origin, env, {"src/b.cs": "b\n"})
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, "0" * 40)
    assert result == "refuse" and "fingerprint" in reason


def test_unsigned_commit_refused(tmp_path, env, origin, trusted, bot_key) -> None:
    sha = make_pick(origin, env, {"src/b.cs": "b\n"}, sign=False)
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "refuse" and "not signed" in reason


def test_not_approved_refused(tmp_path, env, origin, trusted, bot_key) -> None:
    commit(origin, env, {".if-fork/patch-ledger.jsonl": ledger(True) + ledger(False)}, "reject")
    sha = make_pick(origin, env, {"src/b.cs": "b\n"})
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "refuse" and "approved" in reason


def test_trailer_for_other_pr_refused(tmp_path, env, origin, trusted, bot_key) -> None:
    msg = (
        "pick\n\nUpstream-PR: https://github.com/dotnet/wpf/pull/78\n"
        f"Upstream-PR-Head: {UPSTREAM_HEAD}\n"
    )
    sha = make_pick(origin, env, {"src/b.cs": "b\n"}, msg=msg)
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "refuse" and "Upstream-PR trailer" in reason


def test_denylisted_file_refused(tmp_path, env, origin, trusted, bot_key) -> None:
    # eng/ passes the src/ and eng/ allowlist, but file_denylist still applies.
    sha = make_pick(origin, env, {"eng/Versions.props": "<Project />\n"})
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "refuse" and "denylisted" in reason


def test_protected_path_refused(tmp_path, env, origin, trusted, bot_key) -> None:
    sha = make_pick(origin, env, {".github/workflows/x.yml": "on: push\n"})
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "refuse" and "human merge" in reason


@pytest.mark.parametrize(
    "path",
    [
        "tools/ledger-event.py",
        ".if-fork/patch-ledger.jsonl",
        "docs/bot-gpg-pubkey.asc",
        ".github/actions/import-bot-gpg/action.yml",
        "tests/conftest.py",
        "test/InitialForce.WpfSmoke/Perf/PerfHarness.cs",
        "packaging/InitialForce.WPF/InitialForce.WPF.csproj",
        "src/Microsoft.DotNet.Wpf/CLAUDE.md",
    ],
)
def test_fork_owned_path_refused(tmp_path, env, origin, trusted, bot_key, path) -> None:
    sha = make_pick(origin, env, {path: "x\n"})
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "refuse" and "fork-owned" in reason and path in reason


@pytest.mark.parametrize("path", ["Directory.Build.props", "packaging/Directory.Build.props"])
def test_cherry_pick_outside_src_and_eng_refused(
    tmp_path, env, origin, trusted, bot_key, path
) -> None:
    sha = make_pick(origin, env, {path: "<Project />\n"})
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "refuse" and "outside src/ and eng/" in reason


def test_cherry_pick_in_eng_is_ok(tmp_path, env, origin, trusted, bot_key) -> None:
    sha = make_pick(origin, env, {"eng/Build.props": "<Project />\n"})
    assert verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1]) == ("ok", "")


def test_ledger_only_move_is_still_ok(tmp_path, env, origin, trusted, bot_key) -> None:
    sha = make_pick(origin, env, {"src/b.cs": "b\n"})
    commit(origin, env, {".if-fork/patch-ledger.jsonl": ledger(True) * 2}, "ledger")
    assert verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])[0] == "ok"


def test_real_move_on_if_main_is_behind(tmp_path, env, origin, trusted, bot_key) -> None:
    sha = make_pick(origin, env, {"src/b.cs": "b\n"})
    commit(origin, env, {"src/c.cs": "c\n"}, "other change")
    result, reason = verify(tmp_path, env, origin, trusted, sha, BRANCH, bot_key[1])
    assert result == "behind" and "src/c.cs" in reason
    assert (tmp_path / "out" / "upstream_head").read_text().strip() == UPSTREAM_HEAD


# ---------------------------------------------------------------------------
# if/staging (nightly sync PR)
# ---------------------------------------------------------------------------


def staging(
    tmp_path: Path,
    env: dict[str, str],
    origin: Path,
    resolve: dict[str, str],
    extra_commit: bool = False,
    fork_file: str = "src/a.cs",
    trailer: str = "",
    upstream_extra: dict[str, str] | None = None,
) -> tuple[str, Path]:
    """Build upstream from the if/main base and a bot-signed merge into if/staging.

    Upstream changes src/a.cs and global.json. The fork changes fork_file
    first: src/a.cs gives a conflict, any other file a clean merge.
    """
    up = tmp_path / "upstream"
    run(tmp_path, env, "git", "clone", "-q", "-b", "if/main", str(origin), str(up))
    run(up, env, "git", "checkout", "-q", "-b", "release/10.0")
    changes = {"src/a.cs": "upstream\n", "global.json": '{"v":2}\n', **(upstream_extra or {})}
    commit(up, env, changes, "upstream", sign=False)
    run(origin, env, "git", "checkout", "-q", "if/main")
    commit(origin, env, {fork_file: "fork\n"}, "fork patch")
    run(origin, env, "git", "checkout", "-q", "-b", "if/staging")
    if extra_commit:
        commit(origin, env, {"src/z.cs": "z\n"}, "sneaky", sign=False)
    run(origin, env, "git", "fetch", "-q", str(up), "release/10.0")
    subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "FETCH_HEAD"],
        cwd=origin,
        env=env,
        capture_output=True,
    )
    for path, text in resolve.items():
        (origin / path).write_text(text)
        run(origin, env, "git", "add", path)
    run(origin, env, "git", "commit", "-q", "-S", "-m", "Merge upstream" + trailer)
    sha = run(origin, env, "git", "rev-parse", "HEAD")
    run(origin, env, "git", "update-ref", "refs/pull/5/head", sha)
    run(origin, env, "git", "checkout", "-q", "if/main")
    return sha, up


def test_staging_ok(tmp_path, env, origin, trusted, bot_key) -> None:
    sha, up = staging(tmp_path, env, origin, {}, fork_file="src/f.cs")
    result = verify(tmp_path, env, origin, trusted, sha, "if/staging", bot_key[1], upstream=up)
    assert result == ("ok", "")


def test_staging_with_a_conflict_resolution_needs_a_person(
    tmp_path, env, origin, trusted, bot_key
) -> None:
    # Even without the trailer: content that differs from git's own merge is
    # a resolution nobody reviewed.
    sha, up = staging(tmp_path, env, origin, {"src/a.cs": "fork+upstream\n"})
    result, reason = verify(
        tmp_path, env, origin, trusted, sha, "if/staging", bot_key[1], upstream=up
    )
    assert result == "refuse" and "needs-human-merge" in reason and "src/a.cs" in reason


def test_staging_with_conflict_trailer_needs_a_person(
    tmp_path, env, origin, trusted, bot_key
) -> None:
    sha, up = staging(
        tmp_path, env, origin, {}, fork_file="src/f.cs",
        trailer="\n\nConflict-Resolved-By: claude",
    )
    result, reason = verify(
        tmp_path, env, origin, trusted, sha, "if/staging", bot_key[1], upstream=up
    )
    assert result == "refuse" and "needs-human-merge" in reason


def test_staging_refuses_non_upstream_commit(tmp_path, env, origin, trusted, bot_key) -> None:
    sha, up = staging(tmp_path, env, origin, {"src/a.cs": "x\n"}, extra_commit=True)
    result, reason = verify(
        tmp_path, env, origin, trusted, sha, "if/staging", bot_key[1], upstream=up
    )
    assert result == "refuse" and "neither upstream nor bot-signed" in reason


def test_staging_refuses_resolution_that_edits_denylisted_file(
    tmp_path, env, origin, trusted, bot_key
) -> None:
    # git merges global.json cleanly; a different result means someone edited it.
    sha, up = staging(tmp_path, env, origin, {"src/a.cs": "x\n", "global.json": '{"v":9}\n'})
    result, reason = verify(
        tmp_path, env, origin, trusted, sha, "if/staging", bot_key[1], upstream=up
    )
    assert result == "refuse" and "global.json" in reason


def test_staging_refuses_upstream_change_to_fork_owned_path(
    tmp_path, env, origin, trusted, bot_key
) -> None:
    sha, up = staging(
        tmp_path, env, origin, {}, fork_file="src/f.cs", upstream_extra={"tools/x.py": "x\n"}
    )
    result, reason = verify(
        tmp_path, env, origin, trusted, sha, "if/staging", bot_key[1], upstream=up
    )
    assert result == "refuse" and "fork-owned" in reason and "tools/x.py" in reason
