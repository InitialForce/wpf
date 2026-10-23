"""Tests for tools/check-approval.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).parent.parent / "tools" / "check-approval.py"
SHA_A = "a" * 40
SHA_B = "b" * 40


def _ledger(tmp_path: Path, events: list[tuple[str, int, str]]) -> Path:
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        "".join(
            json.dumps({"event": e, "pr_number": pr, "head_sha": sha}) + "\n"
            for e, pr, sha in events
        )
    )
    return path


def _run(ledger: Path, pr: str, sha: str) -> tuple[int, dict]:
    r = subprocess.run(
        [sys.executable, str(TOOL), "--ledger", str(ledger), "--pr-number", pr,
         "--head-sha", sha],
        capture_output=True, text=True,
    )
    return r.returncode, json.loads(r.stdout)


def test_approved_pair(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("review_1", 7, SHA_A), ("approved", 7, SHA_A)])
    assert _run(led, "7", SHA_A) == (0, {"approved": True, "reason": "approved"})


def test_reviewed_but_not_approved(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("review_1", 7, SHA_A), ("review_2", 7, SHA_A)])
    rc, out = _run(led, "7", SHA_A)
    assert rc == 2 and out["reason"] == "no review outcome for this head"


def test_later_rejection_wins(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("approved", 7, SHA_A), ("escalated", 7, SHA_A)])
    rc, out = _run(led, "7", SHA_A)
    assert rc == 2 and out["reason"] == "last outcome is escalated"


def test_approval_of_other_head_does_not_count(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("approved", 7, SHA_B)])
    assert _run(led, "7", SHA_A)[0] == 2


def test_approval_of_other_pr_does_not_count(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("approved", 8, SHA_A)])
    assert _run(led, "7", SHA_A)[0] == 2


def test_rejects_malformed_input(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("approved", 7, SHA_A)])
    assert _run(led, "7; rm -rf /", SHA_A)[0] == 1
    assert _run(led, "7", "abc")[0] == 1
    assert _run(led, "7", SHA_A.upper())[0] == 1


# ---------------------------------------------------------------------------
# --resolution-branch-sha: a Claude conflict resolution must be approved for
# exactly this commit
# ---------------------------------------------------------------------------

BRANCH_1 = "1" * 40
BRANCH_2 = "2" * 40


def _resolution(event: str, pr: int, sha: str, branch_sha: str) -> dict:
    return {"event": event, "pr_number": pr, "head_sha": sha,
            "details": {"branch_sha": branch_sha}}


def _ledger_with(tmp_path: Path, extra: list[dict]) -> Path:
    path = tmp_path / "ledger.jsonl"
    lines = [{"event": "approved", "pr_number": 7, "head_sha": SHA_A}, *extra]
    path.write_text("".join(json.dumps(e) + "\n" for e in lines))
    return path


def _run_resolution(ledger: Path, branch_sha: str, pr: str = "7") -> tuple[int, dict]:
    r = subprocess.run(
        [sys.executable, str(TOOL), "--ledger", str(ledger), "--pr-number", pr,
         "--head-sha", SHA_A, "--resolution-branch-sha", branch_sha],
        capture_output=True, text=True,
    )
    return r.returncode, json.loads(r.stdout)


def test_resolution_approved_for_this_commit(tmp_path: Path) -> None:
    led = _ledger_with(tmp_path, [_resolution("resolution_approved", 7, SHA_A, BRANCH_1)])
    assert _run_resolution(led, BRANCH_1) == (0, {"approved": True, "reason": "approved"})


def test_resolution_missing(tmp_path: Path) -> None:
    rc, out = _run_resolution(_ledger_with(tmp_path, []), BRANCH_1)
    assert rc == 2 and out["reason"] == f"no resolution review approved commit {BRANCH_1}"


def test_resolution_approval_is_stale_after_a_new_push(tmp_path: Path) -> None:
    led = _ledger_with(tmp_path, [_resolution("resolution_approved", 7, SHA_A, BRANCH_1)])
    assert _run_resolution(led, BRANCH_2)[0] == 2


def test_later_resolution_rejection_wins(tmp_path: Path) -> None:
    led = _ledger_with(tmp_path, [
        _resolution("resolution_approved", 7, SHA_A, BRANCH_1),
        # A rejection of the commit counts whatever pair it names.
        _resolution("resolution_rejected", 99, SHA_B, BRANCH_1),
    ])
    rc, out = _run_resolution(led, BRANCH_1)
    assert rc == 2 and "was rejected" in out["reason"]


def test_resolution_approval_after_rejection_counts(tmp_path: Path) -> None:
    led = _ledger_with(tmp_path, [
        _resolution("resolution_rejected", 7, SHA_A, BRANCH_1),
        _resolution("resolution_approved", 7, SHA_A, BRANCH_1),
    ])
    assert _run_resolution(led, BRANCH_1)[0] == 0


def test_resolution_approval_for_other_pair_does_not_count(tmp_path: Path) -> None:
    led = _ledger_with(tmp_path, [
        _resolution("resolution_approved", 8, SHA_A, BRANCH_1),
        _resolution("resolution_approved", 7, SHA_B, BRANCH_1),
    ])
    assert _run_resolution(led, BRANCH_1)[0] == 2


def test_resolution_approval_needs_the_upstream_approval(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps(_resolution("resolution_approved", 7, SHA_A, BRANCH_1)) + "\n")
    rc, out = _run_resolution(path, BRANCH_1)
    assert rc == 2 and out["reason"] == "no review outcome for this head"


def test_resolution_branch_sha_is_validated(tmp_path: Path) -> None:
    led = _ledger_with(tmp_path, [])
    assert _run_resolution(led, "abc")[0] == 1


def test_resolution_only_skips_the_review_outcome(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps(_resolution("resolution_approved", 0, SHA_A, BRANCH_1)) + "\n")
    r = subprocess.run(
        [sys.executable, str(TOOL), "--ledger", str(path), "--pr-number", "0",
         "--head-sha", SHA_A, "--resolution-branch-sha", BRANCH_1, "--resolution-only"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout
    r = subprocess.run(
        [sys.executable, str(TOOL), "--ledger", str(path), "--pr-number", "0",
         "--head-sha", SHA_A, "--resolution-branch-sha", BRANCH_2, "--resolution-only"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2


def test_requeued_pair_is_approved_again(tmp_path: Path) -> None:
    """pr-discovery requeues an escalated pair once; the same pinned head is
    accepted again without a new review."""
    led = _ledger(tmp_path, [
        ("approved", 7, SHA_A), ("escalated", 7, SHA_A), ("requeued", 7, SHA_A),
    ])
    assert _run(led, "7", SHA_A) == (0, {"approved": True, "reason": "approved"})
    # Bound to the head: the other head of the same PR is not approved.
    assert _run(led, "7", SHA_B)[0] == 2


def test_requeue_counts_once(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [
        ("approved", 7, SHA_A), ("escalated", 7, SHA_A), ("requeued", 7, SHA_A),
        ("escalated", 7, SHA_A), ("requeued", 7, SHA_A),
    ])
    assert _run(led, "7", SHA_A) == (2, {"approved": False, "reason": "last outcome is escalated"})


def test_requeue_needs_an_earlier_approval(tmp_path: Path) -> None:
    led = _ledger(tmp_path, [("escalated", 7, SHA_A), ("requeued", 7, SHA_A)])
    assert _run(led, "7", SHA_A)[0] == 2
    rejected = _ledger(tmp_path, [
        ("approved", 7, SHA_A), ("rejected", 7, SHA_A), ("requeued", 7, SHA_A),
    ])
    assert _run(rejected, "7", SHA_A)[0] == 2


def test_print_merge_base_returns_the_last_approved_base(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"event": "approved", "pr_number": 7, "head_sha": SHA_A,
         "details": {"merge_base": "1" * 40}},
        {"event": "approved", "pr_number": 7, "head_sha": SHA_B,
         "details": {"merge_base": "2" * 40}},
        {"event": "approved", "pr_number": 7, "head_sha": SHA_A,
         "details": {"merge_base": "3" * 40}},
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def base(sha: str) -> str:
        r = subprocess.run(
            [sys.executable, str(TOOL), "--ledger", str(ledger), "--pr-number", "7",
             "--head-sha", sha, "--print-merge-base"],
            capture_output=True, text=True, check=True,
        )
        return r.stdout.strip()

    assert base(SHA_A) == "3" * 40
    assert base(SHA_B) == "2" * 40
    assert base("c" * 40) == ""


def test_print_merge_base_is_empty_for_an_older_approval(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [("approved", 7, SHA_A)])
    r = subprocess.run(
        [sys.executable, str(TOOL), "--ledger", str(ledger), "--pr-number", "7",
         "--head-sha", SHA_A, "--print-merge-base"],
        capture_output=True, text=True, check=True,
    )
    assert r.stdout.strip() == ""
