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
