"""Tests for tools/record-resolution-verdict.py."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent
TOOL = REPO_ROOT / "tools" / "record-resolution-verdict.py"
CONFIG = REPO_ROOT / ".if-fork" / "config.yaml"
HEAD = "a" * 40
BRANCH_SHA = "c" * 40
REVIEWER = "claude-fable-5-1-resolution-review"

CLEAN_DIFF = (
    "diff --git a/src/a.cs b/src/a.cs\n--- a/src/a.cs\n+++ b/src/a.cs\n"
    "@@ -1,1 +1,1 @@\n-int x = 1;\n+int x = 2;\n"
)


def _verdict(**overrides: Any) -> dict[str, Any]:
    return {
        "verdict": "approve",
        "confidence": 0.95,
        "reasoning": "Keeps both sides.",
        "novel_lines_justified": True,
        "reviewer_id": REVIEWER,
        **overrides,
    }


def _run(
    tmp_path: Path,
    verdict: dict[str, Any] | str | None,
    diff: str = CLEAN_DIFF,
    sha256: str | None = None,
    upload: str = "success",
    reviewer_id: str = REVIEWER,
    config: Path = CONFIG,
) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    vpath = tmp_path / "resolution.json"
    if verdict is not None:
        vpath.write_text(verdict if isinstance(verdict, str) else json.dumps(verdict))
        if sha256 is None:
            sha256 = hashlib.sha256(vpath.read_bytes()).hexdigest()
    (tmp_path / "final.diff").write_text(diff)
    (tmp_path / "novel.json").write_text(json.dumps({"novel_count": 2, "dropped_count": 1}))
    out, events = tmp_path / "decision.json", tmp_path / "events.jsonl"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--verdict", str(vpath), "--reviewer-id", reviewer_id,
         "--verdict-sha256", sha256 or "", "--upload", upload, "--config", str(config),
         "--final-diff", str(tmp_path / "final.diff"),
         "--novel-lines", str(tmp_path / "novel.json"),
         "--pr-number", "77", "--head-sha", HEAD, "--branch-sha", BRANCH_SHA,
         "--branch", "claude/cherry-pick-pr-77", "--run-url", "https://example/run",
         "--output", str(out), "--events-out", str(events)],
        capture_output=True, text=True,
    )
    if r.returncode == 3:
        return 3, {}, None
    event = json.loads(events.read_text())
    return r.returncode, json.loads(out.read_text()), event


def test_approved(tmp_path: Path) -> None:
    rc, decision, event = _run(tmp_path, _verdict())
    assert rc == 0 and decision["decision"] == "approved" and decision["reasons"] == []
    assert event is not None
    assert event["event"] == "resolution_approved"
    assert event["pr_number"] == 77 and event["head_sha"] == HEAD
    d = event["details"]
    assert d["branch_sha"] == BRANCH_SHA and d["reviewer_id"] == REVIEWER
    assert d["novel_count"] == 2 and d["run_url"] == "https://example/run"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"verdict": "reject"}, "rejected the resolution"),
        ({"verdict": "safe"}, "neither approve nor reject"),
        ({"novel_lines_justified": False}, "did not justify every novel line"),
        ({"novel_lines_justified": "true"}, "did not justify every novel line"),
        ({"confidence": 0.5}, "below the threshold"),
        ({"confidence": "0.99"}, "no numeric confidence"),
        ({"confidence": True}, "no numeric confidence"),
        ({"confidence": 1.5}, "outside 0 to 1"),
    ],
)
def test_verdict_rules(tmp_path: Path, overrides: dict[str, Any], reason: str) -> None:
    rc, decision, event = _run(tmp_path, _verdict(**overrides))
    assert rc == 1 and decision["decision"] == "rejected"
    assert any(reason in r for r in decision["reasons"]), decision["reasons"]
    assert event is not None and event["event"] == "resolution_rejected"
    assert event["details"]["branch_sha"] == BRANCH_SHA


def test_missing_verdict_is_a_rejection(tmp_path: Path) -> None:
    rc, decision, event = _run(tmp_path, None)
    assert rc == 1 and decision["reasons"] == ["the review left no verdict file"]
    assert event is not None and event["event"] == "resolution_rejected"


def test_failed_upload_is_a_rejection(tmp_path: Path) -> None:
    rc, decision, _ = _run(tmp_path, _verdict(), upload="failure")
    assert rc == 1 and "upload step did not succeed" in decision["reasons"][0]


def test_hash_mismatch_is_a_rejection(tmp_path: Path) -> None:
    rc, decision, _ = _run(tmp_path, _verdict(), sha256="0" * 64)
    assert rc == 1 and "does not match the hash" in decision["reasons"][0]


def test_no_recorded_hash_is_a_rejection(tmp_path: Path) -> None:
    rc, decision, _ = _run(tmp_path, _verdict(), sha256="")
    # An empty --verdict-sha256 means the review job saw no file.
    assert rc == 1 and decision["reasons"] == ["the review job recorded no verdict hash"]


def test_other_reviewer_id_is_a_rejection(tmp_path: Path) -> None:
    rc, decision, _ = _run(tmp_path, _verdict(reviewer_id="review-1"))
    assert rc == 1 and "reviewer_id" in decision["reasons"][0]


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_malformed_verdict_is_a_rejection(tmp_path: Path, text: str) -> None:
    rc, decision, _ = _run(tmp_path, text)
    assert rc == 1 and "verdict file is not" in decision["reasons"][0]


def test_hard_fail_pattern_in_final_diff_overrides_approval(tmp_path: Path) -> None:
    diff = CLEAN_DIFF + "+    System.Diagnostics.Process.Start(\"calc\");\n"
    rc, decision, event = _run(tmp_path, _verdict(), diff=diff)
    assert rc == 1
    assert decision["reasons"] == ["hard-fail pattern in the final diff: Process\\.Start"]
    assert event is not None
    assert event["details"]["hard_fail_hits"][0]["pattern"] == "Process\\.Start"


def test_bad_arguments_exit_3(tmp_path: Path) -> None:
    (tmp_path / "final.diff").write_text(CLEAN_DIFF)
    r = subprocess.run(
        [sys.executable, str(TOOL), "--verdict", "x", "--reviewer-id", REVIEWER,
         "--config", str(CONFIG), "--final-diff", str(tmp_path / "final.diff"),
         "--pr-number", "77", "--head-sha", "abc", "--branch-sha", BRANCH_SHA,
         "--branch", "b", "--output", str(tmp_path / "o"), "--events-out", str(tmp_path / "e")],
        capture_output=True, text=True,
    )
    assert r.returncode == 3


def test_empty_final_diff_exits_3(tmp_path: Path) -> None:
    assert _run(tmp_path, _verdict(), diff="")[0] == 3


def test_config_without_threshold_exits_3(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("review_hard_fail_patterns: []\nconflict_resolution: {}\n")
    assert _run(tmp_path, _verdict(), config=cfg)[0] == 3


# ---------------------------------------------------------------------------
# Attempts and the next action
# ---------------------------------------------------------------------------


def _ledger(tmp_path: Path, events: list[tuple[str, int, str]]) -> Path:
    path = tmp_path / "ledger.jsonl"
    path.write_text("".join(
        json.dumps({"event": e, "pr_number": pr, "head_sha": sha,
                    "details": {"branch_sha": "d" * 40}}) + "\n"
        for e, pr, sha in events
    ))
    return path


def _run_with(tmp_path: Path, verdict: dict[str, Any], *extra: str) -> dict[str, Any]:
    vpath = tmp_path / "resolution.json"
    vpath.write_text(json.dumps(verdict))
    (tmp_path / "final.diff").write_text(CLEAN_DIFF)
    out, events = tmp_path / "decision.json", tmp_path / "events.jsonl"
    args = ["--pr-number", "77", "--head-sha", HEAD, *extra]
    r = subprocess.run(
        [sys.executable, str(TOOL), "--verdict", str(vpath), "--reviewer-id", REVIEWER,
         "--verdict-sha256", hashlib.sha256(vpath.read_bytes()).hexdigest(),
         "--upload", "success", "--config", str(CONFIG),
         "--final-diff", str(tmp_path / "final.diff"),
         "--branch-sha", BRANCH_SHA, "--branch", "b",
         "--output", str(out), "--events-out", str(events), *args],
        capture_output=True, text=True,
    )
    assert r.returncode in (0, 1), r.stderr
    return {**json.loads(out.read_text()), "event": json.loads(events.read_text())}


def test_first_rejection_is_retried(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [])
    d = _run_with(tmp_path, _verdict(verdict="reject"), "--ledger", str(led))
    assert (d["decision"], d["attempt"], d["next_action"]) == ("rejected", 1, "retry")
    assert d["event"]["details"]["attempt"] == 1


def test_second_rejection_gives_up(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("resolution_rejected", 77, HEAD)])
    d = _run_with(tmp_path, _verdict(verdict="reject"), "--ledger", str(led))
    assert (d["attempt"], d["next_action"]) == (2, "give_up")


def test_approval_after_a_rejection(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("resolution_rejected", 77, HEAD)])
    d = _run_with(tmp_path, _verdict(), "--ledger", str(led))
    assert (d["decision"], d["attempt"], d["next_action"]) == ("approved", 2, "approved")


def test_a_rebuild_after_an_approval_starts_again(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [
        ("resolution_rejected", 77, HEAD),
        ("resolution_approved", 77, HEAD),
    ])
    d = _run_with(tmp_path, _verdict(verdict="reject"), "--ledger", str(led))
    assert (d["attempt"], d["next_action"]) == (1, "retry")


def test_rejections_of_other_pairs_do_not_count(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [
        ("resolution_rejected", 78, HEAD),
        ("resolution_rejected", 77, "e" * 40),
        ("resolution_rejected", 0, HEAD),
    ])
    d = _run_with(tmp_path, _verdict(verdict="reject"), "--ledger", str(led))
    assert d["next_action"] == "retry"


def test_max_attempts_from_the_command_line(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [])
    d = _run_with(tmp_path, _verdict(verdict="reject"), "--ledger", str(led),
                  "--max-attempts", "1")
    assert d["next_action"] == "give_up"


def test_sync_is_recorded_with_pr_0_and_upstream_sha(tmp_path: Path) -> None:
    vpath = tmp_path / "resolution.json"
    vpath.write_text(json.dumps(_verdict()))
    # Upstream code the resolution kept from release/10.0 shows up as a
    # removed marker-region line; only added lines are scanned for a sync.
    diff = CLEAN_DIFF + "-    unsafe { Upstream(); }\n"
    (tmp_path / "final.diff").write_text(diff)
    out, events = tmp_path / "d.json", tmp_path / "e.jsonl"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--kind", "sync", "--added-only",
         "--verdict", str(vpath), "--reviewer-id", REVIEWER,
         "--verdict-sha256", hashlib.sha256(vpath.read_bytes()).hexdigest(),
         "--upload", "success", "--config", str(CONFIG),
         "--final-diff", str(tmp_path / "final.diff"),
         "--pr-number", "0", "--head-sha", HEAD, "--branch-sha", BRANCH_SHA,
         "--branch", "if/staging", "--output", str(out), "--events-out", str(events)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    event = json.loads(events.read_text())
    assert event["event"] == "resolution_approved" and event["pr_number"] == 0
    assert event["details"]["upstream_sha"] == HEAD and event["details"]["kind"] == "sync"


def test_sync_added_line_with_hard_fail_is_rejected(tmp_path: Path) -> None:
    vpath = tmp_path / "resolution.json"
    vpath.write_text(json.dumps(_verdict()))
    (tmp_path / "final.diff").write_text(CLEAN_DIFF + "+    unsafe { New(); }\n")
    out, events = tmp_path / "d.json", tmp_path / "e.jsonl"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--kind", "sync", "--added-only",
         "--verdict", str(vpath), "--reviewer-id", REVIEWER,
         "--verdict-sha256", hashlib.sha256(vpath.read_bytes()).hexdigest(),
         "--upload", "success", "--config", str(CONFIG),
         "--final-diff", str(tmp_path / "final.diff"),
         "--pr-number", "0", "--head-sha", HEAD, "--branch-sha", BRANCH_SHA,
         "--branch", "if/staging", "--output", str(out), "--events-out", str(events)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1


@pytest.mark.parametrize(("kind", "pr"), [("sync", "77"), ("cherry-pick", "0")])
def test_pr_number_must_match_the_kind(tmp_path: Path, kind: str, pr: str) -> None:
    (tmp_path / "final.diff").write_text(CLEAN_DIFF)
    r = subprocess.run(
        [sys.executable, str(TOOL), "--kind", kind, "--verdict", "x",
         "--reviewer-id", REVIEWER, "--config", str(CONFIG),
         "--final-diff", str(tmp_path / "final.diff"), "--pr-number", pr,
         "--head-sha", HEAD, "--branch-sha", BRANCH_SHA, "--branch", "b",
         "--output", str(tmp_path / "o"), "--events-out", str(tmp_path / "e")],
        capture_output=True, text=True,
    )
    assert r.returncode == 3
