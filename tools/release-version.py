#!/usr/bin/env python3
"""Release tags and NuGet versions for the InitialForce WPF packages.

Tag scheme
----------
  if-<upstream>-perf.<yyyymmdd>          first release of a UTC day
  if-<upstream>-perf.<yyyymmdd>.<rev>    extra same-day release (rev >= 2, manual only)

``<upstream>`` is the dotnet/wpf servicing version from eng/Versions.props
(Major.Minor.Patch) at the tagged commit.

Version scheme
--------------
The NuGet version is derived from the tag alone, so a re-run of release.yml
for the same tag always packs the same version:

  if-10.0.8-perf.20261008     -> 10.0.8-if.20261008.1
  if-10.0.8-perf.20261008.2   -> 10.0.8-if.20261008.2

Both the date and the upstream version only move forward, so versions are
monotonic. They also sort above the old build.yml versions (10.0.0-if.<run>),
because SemVer compares the numeric identifier 20261008 > 981.

Usage
-----
  python tools/release-version.py version --tag if-10.0.8-perf.20261008
      Print the NuGet version for a tag.

  python tools/release-version.py plan --target <sha> \\
      --published-tags-file <file> [--today yyyymmdd] [--force]
      Decide whether auto-release.yml should tag <sha>. Prints
      ``release=true|false``, ``tag=<tag>`` and ``reason=<text>`` lines,
      ready to append to $GITHUB_OUTPUT. <file> lists the tags that have a
      GitHub Release (one per line), which release.yml creates only after a
      successful publish.

  python tools/release-version.py reconcile \\
      --published-tags-file <file> --runs-file <json> [--today yyyymmdd]
      Decide whether auto-release.yml must (re)dispatch release.yml for an
      existing tag before it plans a new one. Prints ``action=dispatch|wait|none``,
      ``tag=<tag>`` and ``reason=<text>`` lines. <json> is
      ``gh run list --workflow release.yml --event workflow_dispatch
      --json displayTitle,status,conclusion``.

Rules for ``reconcile`` (only the newest release tag is considered; older
unpublished tags are superseded by it):
  1. none if there is no release tag, it has a GitHub Release, or it is more
     than --max-age-days (default 7) old.
  2. wait if a Release run for it is queued or in progress.
  3. none if a Release run for it succeeded (it ran but did not publish, for
     example because the autonomy gate was off).
  4. none if --max-attempts (default 3) Release runs for it already failed.
  5. Otherwise dispatch. This repairs a lost dispatch: the tag was pushed but
     release.yml never started.

Rules for ``plan`` (``--force`` skips the first three):
  1. Skip if <sha> already has a release tag (``reconcile`` retries its
     release; ``plan`` never tags the same commit twice).
  2. Skip if a release tag already exists for today (at most one per day).
  3. Skip if src/ is unchanged between the last published tag and <sha>.
  4. Otherwise release <sha> as the next tag for today.

Exit codes: 0 ok, 2 bad input.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TAG_RE = re.compile(r"^if-(\d+\.\d+\.\d+)-perf\.(\d{8})(?:\.(\d+))?$")
RELEASE_PATHS = ("src/",)
VERSIONS_PROPS = "eng/Versions.props"


@dataclass(frozen=True)
class ReleaseTag:
    upstream: str
    date: str
    rev: int
    name: str

    @property
    def sort_key(self) -> tuple[str, int]:
        return (self.date, self.rev)


def parse_tag(tag: str) -> ReleaseTag | None:
    m = TAG_RE.match(tag.strip())
    if not m:
        return None
    rev = int(m.group(3)) if m.group(3) else 1
    if rev < 1:
        return None
    return ReleaseTag(upstream=m.group(1), date=m.group(2), rev=rev, name=tag.strip())


def version_from_tag(tag: str) -> str:
    parsed = parse_tag(tag)
    if parsed is None:
        raise ValueError(
            f"not a release tag: {tag!r} (expected if-<x.y.z>-perf.<yyyymmdd>[.<rev>])"
        )
    return f"{parsed.upstream}-if.{parsed.date}.{parsed.rev}"


def make_tag(upstream: str, date: str, rev: int) -> str:
    base = f"if-{upstream}-perf.{date}"
    return base if rev == 1 else f"{base}.{rev}"


def upstream_version(props_text: str) -> str:
    parts = []
    for name in ("MajorVersion", "MinorVersion", "PatchVersion"):
        m = re.search(rf"<{name}>\s*(\d+)\s*</{name}>", props_text)
        if not m:
            raise ValueError(f"{VERSIONS_PROPS} has no numeric <{name}>")
        parts.append(m.group(1))
    return ".".join(parts)


def latest(tags: list[ReleaseTag]) -> ReleaseTag | None:
    return max(tags, key=lambda t: t.sort_key, default=None)


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def _git(*args: str, repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _release_tags(repo: Path, *extra: str) -> list[ReleaseTag]:
    out = _git("tag", "--list", "if-*", *extra, repo=repo)
    return [t for t in (parse_tag(line) for line in out.splitlines()) if t]


def _src_changed(repo: Path, base: str, target: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", base, target, "--", *RELEASE_PATHS],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git diff {base} {target} failed: {result.stderr.strip()}")
    return result.returncode == 1


@dataclass(frozen=True)
class Plan:
    release: bool
    tag: str
    reason: str


def plan(
    *, repo: Path, target: str, published: set[str], today: str, force: bool
) -> Plan:
    target = _git("rev-parse", "--verify", f"{target}^{{commit}}", repo=repo).strip()
    all_tags = _release_tags(repo)
    today_tags = [t for t in all_tags if t.date == today]
    on_target = _release_tags(repo, "--points-at", target)
    last_published = latest([t for t in all_tags if t.name in published])

    upstream = upstream_version(_git("show", f"{target}:{VERSIONS_PROPS}", repo=repo))
    rev = max((t.rev for t in today_tags), default=0) + 1
    tag = make_tag(upstream, today, rev)
    short = target[:12]

    if not force:
        if on_target:
            names = ", ".join(t.name for t in on_target)
            return Plan(False, "", f"{short} is already tagged ({names})")
        newest_today = latest(today_tags)
        if newest_today:
            return Plan(False, "", f"already released today ({newest_today.name})")
        if last_published and not _src_changed(repo, last_published.name, target):
            return Plan(False, "", f"src/ unchanged since {last_published.name}")

    if last_published is None:
        why = "no published release yet"
    elif force:
        why = f"forced (last published {last_published.name})"
    else:
        why = f"src/ changed since {last_published.name}"
    return Plan(True, tag, f"{why}; tagging {short} as {tag}")


@dataclass(frozen=True)
class Reconcile:
    action: str  # dispatch, wait or none
    tag: str
    reason: str


ACTIVE_RUN_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}


def reconcile(
    *,
    tags: list[ReleaseTag],
    published: set[str],
    runs: list[dict[str, object]],
    today: str,
    max_age_days: int,
    max_attempts: int,
) -> Reconcile:
    newest = latest(tags)
    if newest is None:
        return Reconcile("none", "", "no release tag yet")
    if newest.name in published:
        return Reconcile("none", "", f"newest tag {newest.name} is published")
    age = (
        datetime.datetime.strptime(today, "%Y%m%d")
        - datetime.datetime.strptime(newest.date, "%Y%m%d")
    ).days
    if age > max_age_days:
        return Reconcile(
            "none", "", f"newest tag {newest.name} is unpublished but {age} days old"
        )

    # release.yml sets run-name to "Release <tag>".
    mine = [r for r in runs if r.get("displayTitle") == f"Release {newest.name}"]
    if any(r.get("status") in ACTIVE_RUN_STATUSES for r in mine):
        return Reconcile("wait", newest.name, f"a Release run for {newest.name} is running")
    if any(r.get("conclusion") == "success" for r in mine):
        return Reconcile(
            "none", "", f"a Release run for {newest.name} succeeded without publishing"
        )
    failed = len(mine)
    if failed >= max_attempts:
        return Reconcile(
            "none", "", f"gave up on {newest.name} after {failed} failed Release runs"
        )
    if failed == 0:
        why = "has no Release run (lost dispatch)"
    else:
        why = f"has {failed} failed Release run(s)"
    return Reconcile(
        "dispatch",
        newest.name,
        f"{newest.name} {why}; dispatching attempt {failed + 1} of {max_attempts}",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="print the NuGet version for a tag")
    p_version.add_argument("--tag", required=True)

    p_plan = sub.add_parser("plan", help="decide whether to tag a commit for release")
    p_plan.add_argument("--target", required=True, help="commit to release")
    p_plan.add_argument("--published-tags-file", type=Path, required=True)
    p_plan.add_argument("--today", help="UTC date as yyyymmdd (default: today)")
    p_plan.add_argument("--force", action="store_true")
    p_plan.add_argument("--repo", type=Path, default=Path.cwd())

    p_rec = sub.add_parser("reconcile", help="re-dispatch an unpublished release tag")
    p_rec.add_argument("--published-tags-file", type=Path, required=True)
    p_rec.add_argument("--runs-file", type=Path, required=True)
    p_rec.add_argument("--today", help="UTC date as yyyymmdd (default: today)")
    p_rec.add_argument("--max-age-days", type=int, default=7)
    p_rec.add_argument("--max-attempts", type=int, default=3)
    p_rec.add_argument("--repo", type=Path, default=Path.cwd())

    args = parser.parse_args(argv)

    try:
        if args.command == "version":
            print(version_from_tag(args.tag))
            return 0

        today = args.today or datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")
        if not re.fullmatch(r"\d{8}", today):
            raise ValueError(f"--today must be yyyymmdd, got {today!r}")
        published = {
            line.strip()
            for line in args.published_tags_file.read_text().splitlines()
            if line.strip()
        }
        if args.command == "reconcile":
            runs = json.loads(args.runs_file.read_text() or "[]")
            if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
                raise ValueError("--runs-file must hold a JSON list of runs")
            rec = reconcile(
                tags=_release_tags(args.repo),
                published=published,
                runs=runs,
                today=today,
                max_age_days=args.max_age_days,
                max_attempts=args.max_attempts,
            )
            print(f"action={rec.action}")
            print(f"tag={rec.tag}")
            print(f"reason={rec.reason}")
            return 0

        result = plan(
            repo=args.repo,
            target=args.target,
            published=published,
            today=today,
            force=args.force,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"release={'true' if result.release else 'false'}")
    print(f"tag={result.tag}")
    print(f"reason={result.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
