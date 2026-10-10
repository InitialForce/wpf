"""
Tests for tools/verify-release-tag.sh, the tag check release.yml and
auto-release.yml run before a tag is published.

Uses real GnuPG keys and a throwaway git repository. Regression cover for the
round-2 finding that release verification depended on an unset repository
variable: the key now comes from if/main's docs/bot-gpg-pubkey.asc, pinned to
the GH_BOT_GPG_FINGERPRINT fingerprint, so a swapped or extended key file is
refused.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "verify-release-tag.sh"
TAG = "if-10.0.8-perf.20261008"

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("git") is None,
    reason="gpg and git are required",
)


@dataclass
class Env:
    repo: Path
    gnupg: Path
    bot_fpr: str
    other_fpr: str
    bot_key: Path
    other_key: Path
    both_keys: Path
    main: str
    side: str


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        list(args), cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


def _gen_key(gnupg: Path, uid: str) -> str:
    env = {**os.environ, "GNUPGHOME": str(gnupg)}
    _run("gpg", "--batch", "--passphrase", "", "--quick-gen-key", uid, "ed25519", "sign",
         "never", env=env)
    out = _run("gpg", "--batch", "--with-colons", "--list-keys", uid, env=env)
    return next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:"))


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    base = tmp_path_factory.mktemp("verify-tag")
    gnupg = base / "gnupg"
    gnupg.mkdir(mode=0o700)
    bot = _gen_key(gnupg, "Bot <bot@example.com>")
    other = _gen_key(gnupg, "Other <other@example.com>")
    genv = {**os.environ, "GNUPGHOME": str(gnupg)}
    keys = {}
    for name, fprs in (("bot", [bot]), ("other", [other]), ("both", [bot, other])):
        path = base / f"{name}.asc"
        path.write_text(_run("gpg", "--batch", "--armor", "--export", *fprs, env=genv))
        keys[name] = path

    repo = base / "repo"
    repo.mkdir()
    git = ("git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
           "-c", "commit.gpgsign=false")
    _run(*git, "init", "-q", "-b", "main")
    _run(*git, "commit", "-q", "--allow-empty", "-m", "one")
    main = _run(*git, "rev-parse", "HEAD").strip()
    _run(*git, "checkout", "-q", "-b", "side")
    _run(*git, "commit", "-q", "--allow-empty", "-m", "side")
    side = _run(*git, "rev-parse", "HEAD").strip()
    _run(*git, "checkout", "-q", "main")

    def tag(name: str, commit: str, key: str | None) -> None:
        if key is None:
            _run(*git, "tag", name, commit)
        else:
            _run(*git, "-c", f"user.signingkey={key}", "tag", "-s", name, commit, "-m", name,
                 env=genv)

    tag(TAG, main, bot)
    tag("if-10.0.8-perf.20261009", main, other)
    tag("if-10.0.8-perf.20261010", main, None)
    tag("if-10.0.8-perf.20261011", side, bot)
    yield Env(repo, gnupg, bot, other, keys["bot"], keys["other"], keys["both"], main, side)
    # gpg autostarts a gpg-agent and scdaemon per homedir; stop them or they outlive pytest.
    subprocess.run(["gpgconf", "--homedir", str(gnupg), "--kill", "all"], check=False)


def _verify(e: Env, tag: str, *, key: Path | None = None, fpr: str | None = None,
            main_ref: str = "main") -> subprocess.CompletedProcess[str]:
    run_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(e.repo.parent),
        "PUBKEY_FILE": str(key or e.bot_key),
        "MAIN_REF": main_ref,
        "BOT_FPR": e.bot_fpr if fpr is None else fpr,
    }
    return subprocess.run(["bash", str(SCRIPT), tag], cwd=e.repo, env=run_env,
                          capture_output=True, text=True)


def test_good_tag_prints_commit(env: Env) -> None:
    result = _verify(env, TAG)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == env.main


def test_fingerprint_with_spaces_and_lowercase(env: Env) -> None:
    spaced = " ".join(env.bot_fpr[i:i + 4] for i in range(0, 40, 4)).lower()
    assert _verify(env, TAG, fpr=spaced).returncode == 0


def test_tag_signed_by_other_key_refused(env: Env) -> None:
    result = _verify(env, "if-10.0.8-perf.20261009")
    assert result.returncode == 1
    assert "Unsigned or invalid" in result.stderr or "no good signature" in result.stderr


def test_swapped_key_file_refused(env: Env) -> None:
    """A key file holding another key is refused even when that key made the
    signature: the fingerprint pin is what makes the file trustworthy."""
    result = _verify(env, "if-10.0.8-perf.20261009", key=env.other_key)
    assert result.returncode == 1
    assert "must hold exactly the bot key" in result.stderr


def test_key_file_with_extra_key_refused(env: Env) -> None:
    result = _verify(env, "if-10.0.8-perf.20261009", key=env.both_keys)
    assert result.returncode == 1
    assert "must hold exactly the bot key" in result.stderr


def test_missing_fingerprint_refused(env: Env) -> None:
    result = _verify(env, TAG, fpr="")
    assert result.returncode == 1
    assert "BOT_FPR" in result.stderr


def test_lightweight_tag_refused(env: Env) -> None:
    result = _verify(env, "if-10.0.8-perf.20261010")
    assert result.returncode == 1
    assert "lightweight" in result.stderr


def test_tag_not_on_main_refused(env: Env) -> None:
    result = _verify(env, "if-10.0.8-perf.20261011")
    assert result.returncode == 1
    assert "not reachable" in result.stderr


@pytest.mark.parametrize(
    "bad", ["if-10.0.$(touch pwned)", "if-10.0.8-perf.20261008;id", "v10.0.8", ""]
)
def test_bad_name_refused_before_use(env: Env, bad: str) -> None:
    result = _verify(env, bad)
    assert result.returncode == 1
    assert "Invalid tag format" in result.stderr
    assert not (env.repo / "pwned").exists()
