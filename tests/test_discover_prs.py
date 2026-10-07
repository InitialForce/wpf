"""Tests for tools/discover-prs.py."""

from __future__ import annotations

import importlib.util
import json
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

_SCRIPT = Path(__file__).parent.parent / "tools" / "discover-prs.py"
_spec = importlib.util.spec_from_file_location("discover_prs", _SCRIPT)
assert _spec is not None and _spec.loader is not None
discover_prs: types.ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(discover_prs)  # type: ignore[union-attr]

REPO_CONFIG = Path(__file__).parent.parent / ".if-fork" / "config.yaml"
NOW = datetime(2026, 10, 7, 4, 17, tzinfo=UTC)


def _config(cap: int = 10) -> dict[str, Any]:
    return {
        "author_allowlist": ["h3xds1nz"],
        "review_hard_fail_patterns": ["\\bunsafe\\b"],
        "tier_predicates": {
            "s": {"max_files_touched": 5, "max_loc_delta": 200},
            "a": {"max_files_touched": 20, "max_loc_delta": 800},
        },
        "discovery": {
            "base_branches": ["main"],
            "include_labels": ["Community Contribution"],
            "exclude_authors": ["dotnet-bot"],
            "merged_lookback_days": 90,
            "max_reviews_per_run": cap,
        },
    }


def _pr(number: int, sha: str = "", **overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "number": number,
        "title": f"PR {number}",
        "author": {"login": "h3xds1nz"},
        "url": f"https://github.com/dotnet/wpf/pull/{number}",
        "headRefOid": sha or f"sha{number}",
        "changedFiles": 1,
        "additions": 10,
        "deletions": 5,
        "labels": [],
        "isDraft": False,
        "baseRefName": "main",
        "mergedAt": None,
    }
    pr.update(overrides)
    return pr


def _entry(event: str, number: int, sha: str, **details: Any) -> dict[str, Any]:
    return {"event": event, "pr_number": number, "head_sha": sha, "actor": "x", "details": details}


def _run(open_prs, merged_prs=(), entries=(), cap=10):  # type: ignore[no-untyped-def]
    return discover_prs.discover(list(open_prs), list(merged_prs), list(entries), _config(cap), NOW)


def test_repo_config_has_discovery_block() -> None:
    config = yaml.safe_load(REPO_CONFIG.read_text())
    assert config["discovery"]["max_reviews_per_run"] > 0


def test_new_pr_is_recorded_and_queued() -> None:
    events, queue, counts = _run([_pr(1)])
    assert [(e["pr_number"], e["head_sha"]) for e in events] == [(1, "sha1")]
    assert events[0]["event"] == "discovered"
    assert events[0]["details"]["if_tier"] == "S"
    assert queue == [{"pr_number": 1, "head_sha": "sha1"}]
    assert counts["new_discovered"] == 1


def test_known_pair_is_not_recorded_again_but_stays_queued_until_reviewed() -> None:
    events, queue, _ = _run([_pr(1)], entries=[_entry("discovered", 1, "sha1")])
    assert events == []
    assert queue == [{"pr_number": 1, "head_sha": "sha1"}]


def test_reviewed_pair_is_not_queued() -> None:
    entries = [
        _entry("discovered", 1, "sha1"),
        _entry("merged_verdict", 1, "sha1", verdict="escalated"),
    ]
    events, queue, _ = _run([_pr(1)], entries=entries)
    assert events == [] and queue == []


def test_new_head_sha_on_known_pr_is_a_new_candidate() -> None:
    entries = [
        _entry("discovered", 1, "old"),
        _entry("merged_verdict", 1, "old", verdict="escalated"),
    ]
    events, queue, _ = _run([_pr(1, "new")], entries=entries)
    assert [(e["pr_number"], e["head_sha"]) for e in events] == [(1, "new")]
    assert queue == [{"pr_number": 1, "head_sha": "new"}]


def test_applied_graduated_rejected_prs_are_skipped() -> None:
    entries = [
        _entry("discovered", 1, "sha1", if_already_applied_in_local_main=True),
        _entry("cherry_picked", 2, "sha2"),
        _entry("graduated_upstream", 3, "sha3"),
        _entry("rejected", 4, "sha4"),
        _entry("merged_verdict", 5, "old", verdict="rejected"),
    ]
    prs = [_pr(1), _pr(2, "new2"), _pr(3), _pr(4, "new4"), _pr(5, "new5")]
    events, queue, _ = _run(prs, entries=entries)
    assert events == [] and queue == []


def test_ineligible_prs_are_ignored() -> None:
    prs = [
        _pr(1, isDraft=True),
        _pr(2, baseRefName="release/10.0"),
        _pr(3, author={"login": "app/dotnet-maestro"}),
        _pr(4, author={"login": "dotnet-bot"}),
        _pr(5, author={"login": "someone"}),  # not allowlisted, no label
        _pr(6, author={"login": "someone"}, labels=[{"name": "Community Contribution"}]),
    ]
    events, queue, _ = _run(prs)
    assert [e["pr_number"] for e in events] == [6]
    assert queue == [{"pr_number": 6, "head_sha": "sha6"}]


def test_merged_prs_outside_lookback_are_ignored() -> None:
    merged = [
        _pr(1, mergedAt="2026-09-01T00:00:00Z"),
        _pr(2, mergedAt="2025-01-01T00:00:00Z"),
    ]
    events, _, _ = _run([], merged)
    assert [e["pr_number"] for e in events] == [1]
    assert events[0]["details"]["state"] == "MERGED"


def test_seeded_merged_backlog_is_queued_without_upstream_query() -> None:
    seed = _entry("discovered", 7, "seedsha", state="MERGED", if_tier="A")
    seed["actor"] = "seed-bulk-import"
    events, queue, _ = _run([], entries=[seed])
    assert events == []
    assert queue == [{"pr_number": 7, "head_sha": "seedsha"}]


def test_queue_is_capped_and_ordered() -> None:
    open_prs = [
        _pr(30, changedFiles=50),  # tier B
        _pr(20),  # tier S
        _pr(10, author={"login": "x"}, labels=[{"name": "Community Contribution"}]),
    ]
    merged = [_pr(40, mergedAt="2026-10-01T00:00:00Z", changedFiles=50)]
    _, queue, counts = _run(open_prs, merged, cap=3)
    assert [q["pr_number"] for q in queue] == [40, 20, 30]
    assert counts["pending_review"] == 4


def test_hard_fail_title_is_suspect() -> None:
    events, _, _ = _run([_pr(1, title="Use unsafe pointer")])
    assert events[0]["details"]["if_tier"] == "SUSPECT"


def test_cli_writes_events_and_queue(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({**_config(), "upstream": {"repo": "dotnet/wpf"}}))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(_entry("discovered", 1, "sha1")) + "\n")
    open_json = tmp_path / "open.json"
    open_json.write_text(json.dumps([_pr(1), _pr(2)]))
    merged_json = tmp_path / "merged.json"
    merged_json.write_text("[]")
    events_out = tmp_path / "events.jsonl"
    queue_out = tmp_path / "queue.json"

    ret = discover_prs.main(
        [
            "--config",
            str(config),
            "--ledger-path",
            str(ledger),
            "--events-out",
            str(events_out),
            "--queue-out",
            str(queue_out),
            "--open-json",
            str(open_json),
            "--merged-json",
            str(merged_json),
            "--now",
            "2026-10-07T04:17:00Z",
        ]
    )

    assert ret == 0
    events = [json.loads(line) for line in events_out.read_text().splitlines()]
    assert [e["pr_number"] for e in events] == [2]
    assert json.loads(queue_out.read_text()) == [
        {"pr_number": 1, "head_sha": "sha1"},
        {"pr_number": 2, "head_sha": "sha2"},
    ]
