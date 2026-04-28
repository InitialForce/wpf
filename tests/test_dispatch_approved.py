"""Tests for tools/dispatch-approved.py.

Covers:
  - approved verdict  → repository_dispatch POST
  - escalated verdict → issues POST
  - rejected verdict  → ledger-event.py subprocess invocation
  - dry-run mode for all three verdict types
  - missing/malformed verdict files
  - unknown verdict value
  - missing token env var (non-dry-run)
  - correct URL construction per repo arg
  - payload shape for approved dispatch
  - issue title and labels for escalated
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load module (hyphen in filename)
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).parent.parent / "tools" / "dispatch-approved.py"
_spec = importlib.util.spec_from_file_location("dispatch_approved", _SCRIPT)
assert _spec is not None and _spec.loader is not None
dispatch_approved: types.ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispatch_approved)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"

APPROVED_VERDICT = {
    "verdict": "approved",
    "pr_number": 6511,
    "head_sha": "c8742b5c3fa1d2e3f4a5b6c7d8e9f0a1b2c3d4e5",
    "review_1_summary": "Looks safe.",
    "review_2_summary": "Agrees with review-1.",
}

ESCALATED_VERDICT = {
    "verdict": "escalated",
    "pr_number": 6502,
    "head_sha": "81ddc95ac1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    "review_1_summary": "Possibly safe.",
    "review_2_summary": "Cannot confirm — human review needed.",
}

REJECTED_VERDICT = {
    "verdict": "rejected",
    "pr_number": 1234,
    "head_sha": "deadbeef1234567890abcdef1234567890abcdef",
    "review_1_summary": "Unsafe change.",
    "review_2_summary": "Agrees — unsafe.",
}

REPO = "InitialForce/wpf"
TOKEN = "ghp_test_token_1234"


def _write_verdict(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "verdict.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _argv(
    verdict_path: Path,
    repo: str = REPO,
    token_env: str = "GITHUB_TOKEN",
    dry_run: bool = False,
) -> list[str]:
    args = ["--verdict", str(verdict_path), "--repo", repo,
            "--token-env", token_env]
    if dry_run:
        args.append("--dry-run")
    return args


# ===========================================================================
# 1 — approved + dry-run: prints dispatches URL and correct body shape
# ===========================================================================

def test_approved_dry_run_prints_dispatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write_verdict(tmp_path, APPROVED_VERDICT)
    rc = dispatch_approved.main(_argv(p, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"/repos/{REPO}/dispatches" in out
    assert "pr-ingestion-requested" in out
    assert str(APPROVED_VERDICT["pr_number"]) in out
    assert APPROVED_VERDICT["head_sha"] in out


# ===========================================================================
# 2 — approved + live: calls urlopen with correct URL and payload
# ===========================================================================

def test_approved_live_calls_urlopen(tmp_path: Path) -> None:
    p = _write_verdict(tmp_path, APPROVED_VERDICT)

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 204

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open, \
         patch.dict("os.environ", {"GITHUB_TOKEN": TOKEN}):
        rc = dispatch_approved.main(_argv(p))

    assert rc == 0
    assert mock_open.called
    req = mock_open.call_args[0][0]
    assert f"/repos/{REPO}/dispatches" in req.full_url
    body = json.loads(req.data)
    assert body["event_type"] == "pr-ingestion-requested"
    payload = body["client_payload"]
    assert payload["pr_number"] == APPROVED_VERDICT["pr_number"]
    assert payload["head_sha"] == APPROVED_VERDICT["head_sha"]
    assert "review_1_summary" in payload
    assert "review_2_summary" in payload


# ===========================================================================
# 3 — escalated + dry-run: prints issues URL, title, and labels
# ===========================================================================

def test_escalated_dry_run_prints_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write_verdict(tmp_path, ESCALATED_VERDICT)
    rc = dispatch_approved.main(_argv(p, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"/repos/{REPO}/issues" in out
    pr = ESCALATED_VERDICT["pr_number"]
    assert f"review-disagreement: PR #{pr}" in out
    assert "review-disagreement" in out
    assert "needs-human" in out


# ===========================================================================
# 4 — escalated + live: calls urlopen with correct URL and issue payload
# ===========================================================================

def test_escalated_live_calls_urlopen(tmp_path: Path) -> None:
    p = _write_verdict(tmp_path, ESCALATED_VERDICT)

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 201

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open, \
         patch.dict("os.environ", {"GITHUB_TOKEN": TOKEN}):
        rc = dispatch_approved.main(_argv(p))

    assert rc == 0
    assert mock_open.called
    req = mock_open.call_args[0][0]
    assert f"/repos/{REPO}/issues" in req.full_url
    body = json.loads(req.data)
    pr = ESCALATED_VERDICT["pr_number"]
    assert f"PR #{pr}" in body["title"]
    assert "review-disagreement" in body["labels"]
    assert "needs-human" in body["labels"]


# ===========================================================================
# 5 — rejected + dry-run: prints ledger-event.py invocation
# ===========================================================================

def test_rejected_dry_run_prints_ledger_cmd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write_verdict(tmp_path, REJECTED_VERDICT)
    rc = dispatch_approved.main(_argv(p, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ledger-event.py" in out
    assert "rejected" in out
    assert str(REJECTED_VERDICT["pr_number"]) in out


# ===========================================================================
# 6 — rejected + live: calls subprocess.run with ledger-event.py
# ===========================================================================

def test_rejected_live_calls_subprocess(tmp_path: Path) -> None:
    p = _write_verdict(tmp_path, REJECTED_VERDICT)

    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result) as mock_run, \
         patch.dict("os.environ", {"GITHUB_TOKEN": TOKEN}):
        rc = dispatch_approved.main(_argv(p))

    assert rc == 0
    assert mock_run.called
    cmd: list[str] = mock_run.call_args[0][0]
    assert "ledger-event.py" in " ".join(cmd)
    assert "--event" in cmd
    assert "rejected" in cmd


# ===========================================================================
# 7 — missing verdict file → exit code 2
# ===========================================================================

def test_missing_verdict_file_exits_2(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-file.json"
    rc = dispatch_approved.main(_argv(missing, dry_run=True))
    assert rc == 2


# ===========================================================================
# 8 — invalid JSON in verdict file → exit code 2
# ===========================================================================

def test_invalid_json_verdict_exits_2(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not-json", encoding="utf-8")
    rc = dispatch_approved.main(_argv(p, dry_run=True))
    assert rc == 2


# ===========================================================================
# 9 — unknown verdict value → exit code 2
# ===========================================================================

def test_unknown_verdict_value_exits_2(tmp_path: Path) -> None:
    data = {**APPROVED_VERDICT, "verdict": "bogus_verdict"}
    p = _write_verdict(tmp_path, data)
    rc = dispatch_approved.main(_argv(p, dry_run=True))
    assert rc == 2


# ===========================================================================
# 10 — missing GitHub token in live mode → exit code 2
# ===========================================================================

def test_missing_token_exits_2(tmp_path: Path) -> None:
    p = _write_verdict(tmp_path, APPROVED_VERDICT)
    # Use a token-env var that is definitely not set
    with patch.dict("os.environ", {}, clear=True):
        rc = dispatch_approved.main(
            _argv(p, token_env="SURELY_NOT_SET_XYZ_99999")
        )
    assert rc == 2


# ===========================================================================
# 11 — approved dry-run uses --repo in dispatch URL
# ===========================================================================

def test_approved_dry_run_uses_repo_in_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    custom_repo = "Acme/my-wpf"
    p = _write_verdict(tmp_path, APPROVED_VERDICT)
    rc = dispatch_approved.main(_argv(p, repo=custom_repo, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"/repos/{custom_repo}/dispatches" in out


# ===========================================================================
# 12 — escalated dry-run: review summaries appear in printed payload
# ===========================================================================

def test_escalated_dry_run_includes_summaries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write_verdict(tmp_path, ESCALATED_VERDICT)
    rc = dispatch_approved.main(_argv(p, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert ESCALATED_VERDICT["review_1_summary"] in out
    assert ESCALATED_VERDICT["review_2_summary"] in out


# ===========================================================================
# 13 — fixture files are loadable and match expected verdict values
# ===========================================================================

def test_fixture_approved_loads() -> None:
    data = json.loads((FIXTURES / "merged-verdict-approved.json").read_text())
    assert data["verdict"] == "approved"
    assert "pr_number" in data
    assert "head_sha" in data


def test_fixture_escalated_loads() -> None:
    data = json.loads((FIXTURES / "merged-verdict-escalated.json").read_text())
    assert data["verdict"] == "escalated"
    assert "pr_number" in data
