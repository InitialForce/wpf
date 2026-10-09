"""
Tests for tools/release-version.py

Covers:
  - tag parsing and tag -> NuGet version mapping (deterministic, monotonic)
  - upstream version read from eng/Versions.props
  - plan: first release, src/ changed, src/ unchanged, one-per-day,
    already-tagged commit, --force, failed (unpublished) tags ignored as base
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "release-version.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_version"] = module
    spec.loader.exec_module(module)
    return module


rv = _load()

PROPS = """<Project>
  <PropertyGroup>
    <MajorVersion>10</MajorVersion>
    <MinorVersion>0</MinorVersion>
    <PatchVersion>{patch}</PatchVersion>
  </PropertyGroup>
</Project>
"""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("if-10.0.8-perf.20261008", "10.0.8-if.20261008.1"),
        ("if-10.0.8-perf.20261008.2", "10.0.8-if.20261008.2"),
        ("if-10.0.0-perf.20260516", "10.0.0-if.20260516.1"),
    ],
)
def test_version_from_tag(tag: str, version: str) -> None:
    assert rv.version_from_tag(tag) == version


@pytest.mark.parametrize(
    "tag", ["if-10.0.8", "v10.0.8", "if-10.0-perf.20261008", "if-10.0.8-perf.2026100", ""]
)
def test_version_from_bad_tag_raises(tag: str) -> None:
    with pytest.raises(ValueError):
        rv.version_from_tag(tag)


def test_make_tag_omits_rev_1() -> None:
    assert rv.make_tag("10.0.8", "20261008", 1) == "if-10.0.8-perf.20261008"
    assert rv.make_tag("10.0.8", "20261008", 3) == "if-10.0.8-perf.20261008.3"


def test_upstream_version_from_props() -> None:
    assert rv.upstream_version(PROPS.format(patch=8)) == "10.0.8"


def test_upstream_version_missing_patch_raises() -> None:
    with pytest.raises(ValueError):
        rv.upstream_version("<Project><MajorVersion>10</MajorVersion></Project>")


def _semver_key(v: str) -> tuple[int, ...]:
    core, pre = v.split("-", 1)
    nums = [int(x) for x in core.split(".")]
    pre_parts = pre.split(".")[1:]  # drop the "if" label, all the rest are numeric
    return (*nums, *(int(x) for x in pre_parts))


def test_new_versions_sort_above_old_run_number_versions() -> None:
    """10.0.0-if.981 (old build.yml scheme) must sort below every new version."""
    old = _semver_key("10.0.0-if.981")
    assert _semver_key(rv.version_from_tag("if-10.0.0-perf.20261008")) > old
    assert _semver_key(rv.version_from_tag("if-10.0.8-perf.20261008")) > old


# ---------------------------------------------------------------------------
# plan() against a real temporary git repository
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, path: str, content: str) -> str:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", f"change {path}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "tag.gpgsign", "false")
    _commit(tmp_path, "eng/Versions.props", PROPS.format(patch=8))
    _commit(tmp_path, "src/a.cs", "a")
    return tmp_path


def _plan(repo: Path, target: str = "HEAD", published: set[str] | None = None,
          today: str = "20261008", force: bool = False):
    return rv.plan(repo=repo, target=target, published=published or set(),
                   today=today, force=force)


def test_first_release_when_nothing_published(repo: Path) -> None:
    p = _plan(repo)
    assert p.release is True
    assert p.tag == "if-10.0.8-perf.20261008"


def test_unpublished_failed_tag_is_not_a_base(repo: Path) -> None:
    """An older tag without a GitHub Release (failed publish) does not block."""
    _git(repo, "tag", "if-10.0.0-perf.20260516", "HEAD~1")
    p = _plan(repo)
    assert p.release is True


def test_skip_when_src_unchanged(repo: Path) -> None:
    _git(repo, "tag", "if-10.0.8-perf.20261007")
    _commit(repo, ".if-fork/patch-ledger.jsonl", "{}\n")
    _commit(repo, "docs/x.md", "doc")
    p = _plan(repo, published={"if-10.0.8-perf.20261007"})
    assert p.release is False
    assert "src/ unchanged" in p.reason


def test_release_when_src_changed(repo: Path) -> None:
    _git(repo, "tag", "if-10.0.8-perf.20261007")
    _commit(repo, "src/b.cs", "b")
    p = _plan(repo, published={"if-10.0.8-perf.20261007"})
    assert p.release is True
    assert p.tag == "if-10.0.8-perf.20261008"


def test_upstream_bump_goes_into_tag(repo: Path) -> None:
    _git(repo, "tag", "if-10.0.8-perf.20261007")
    _commit(repo, "eng/Versions.props", PROPS.format(patch=9))
    _commit(repo, "src/b.cs", "b")
    p = _plan(repo, published={"if-10.0.8-perf.20261007"})
    assert p.tag == "if-10.0.9-perf.20261008"


def test_one_release_per_day(repo: Path) -> None:
    _git(repo, "tag", "if-10.0.8-perf.20261008")
    _commit(repo, "src/b.cs", "b")
    p = _plan(repo, published={"if-10.0.8-perf.20261008"})
    assert p.release is False
    assert "already released today" in p.reason


def test_failed_tag_today_also_blocks(repo: Path) -> None:
    """A tag created today counts even if its release failed (no daily retry storm)."""
    _git(repo, "tag", "if-10.0.8-perf.20261008", "HEAD~1")
    p = _plan(repo)
    assert p.release is False


def test_skip_commit_already_tagged(repo: Path) -> None:
    _git(repo, "tag", "if-10.0.8-perf.20261007")
    p = _plan(repo)
    assert p.release is False
    assert "already tagged" in p.reason


def test_force_overrides_and_bumps_rev(repo: Path) -> None:
    _git(repo, "tag", "if-10.0.8-perf.20261008")
    p = _plan(repo, published={"if-10.0.8-perf.20261008"}, force=True)
    assert p.release is True
    assert p.tag == "if-10.0.8-perf.20261008.2"
    _git(repo, "tag", p.tag)
    p2 = _plan(repo, published={"if-10.0.8-perf.20261008"}, force=True)
    assert p2.tag == "if-10.0.8-perf.20261008.3"


def test_cli_plan_output(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    published = tmp_path_factory.mktemp("pub") / "published.txt"
    published.write_text("")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "plan", "--repo", str(repo), "--target", "HEAD",
         "--published-tags-file", str(published), "--today", "20261008"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert lines["release"] == "true"
    assert lines["tag"] == "if-10.0.8-perf.20261008"


def test_cli_version_bad_tag_exit_2() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "version", "--tag", "nope"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# reconcile(): re-dispatch an unpublished release tag (lost dispatch)
# ---------------------------------------------------------------------------

def _tags(*names: str) -> list:
    return [rv.parse_tag(n) for n in names]


def _run(tag: str, status: str = "completed", conclusion: str = "failure") -> dict:
    return {"displayTitle": f"Release {tag}", "status": status, "conclusion": conclusion}


def _rec(tags: list, runs: list | None = None, published: set[str] | None = None,
         today: str = "20261009"):
    return rv.reconcile(tags=tags, published=published or set(), runs=runs or [],
                        today=today, max_age_days=7, max_attempts=3)


T = "if-10.0.8-perf.20261008"


def test_reconcile_lost_dispatch_is_dispatched() -> None:
    """Regression: the tag was pushed but release.yml never started. Before,
    plan() skipped the tagged commit forever and nothing published it."""
    r = _rec(_tags(T))
    assert (r.action, r.tag) == ("dispatch", T)
    assert "lost dispatch" in r.reason


def test_reconcile_failed_runs_retry_up_to_limit() -> None:
    assert _rec(_tags(T), [_run(T)]).action == "dispatch"
    assert _rec(_tags(T), [_run(T), _run(T, conclusion="cancelled")]).action == "dispatch"
    r = _rec(_tags(T), [_run(T)] * 3)
    assert r.action == "none"
    assert "gave up" in r.reason


def test_reconcile_waits_for_running_release() -> None:
    for status in ("queued", "in_progress", "waiting"):
        r = _rec(_tags(T), [_run(T, status=status, conclusion="")])
        assert (r.action, r.tag) == ("wait", T)


def test_reconcile_successful_run_is_not_repeated() -> None:
    assert _rec(_tags(T), [_run(T, conclusion="success")]).action == "none"


def test_reconcile_published_tag_needs_nothing() -> None:
    assert _rec(_tags(T), published={T}).action == "none"


def test_reconcile_only_newest_tag() -> None:
    """An older unpublished tag is superseded by a newer published one."""
    newer = "if-10.0.8-perf.20261009"
    assert _rec(_tags(T, newer), published={newer}).action == "none"
    r = _rec(_tags(T, newer))
    assert r.tag == newer


def test_reconcile_ignores_old_tags_and_other_runs() -> None:
    """The May tags that never published must not be re-released."""
    assert _rec(_tags("if-10.0.0-perf.20260516")).action == "none"
    other = {"displayTitle": "ci(release): fix", "status": "completed", "conclusion": "failure"}
    assert _rec(_tags(T), [other] * 5).action == "dispatch"


def test_reconcile_no_tags() -> None:
    assert _rec([]).action == "none"


def test_cli_reconcile_output(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    _git(repo, "tag", T)
    d = tmp_path_factory.mktemp("rec")
    (d / "published.txt").write_text("")
    (d / "runs.json").write_text(json.dumps([_run(T)]))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "reconcile", "--repo", str(repo),
         "--published-tags-file", str(d / "published.txt"),
         "--runs-file", str(d / "runs.json"), "--today", "20261009"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert lines["action"] == "dispatch"
    assert lines["tag"] == T
    (d / "runs.json").write_text('{"not": "a list"}')
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "reconcile", "--repo", str(repo),
         "--published-tags-file", str(d / "published.txt"),
         "--runs-file", str(d / "runs.json")],
        capture_output=True, text=True,
    )
    assert bad.returncode == 2
