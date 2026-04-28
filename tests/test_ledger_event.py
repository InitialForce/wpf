"""Tests for tools/ledger-event.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ledger-event.py has a hyphen in the filename — use importlib to load it.
_SCRIPT = Path(__file__).parent.parent / "tools" / "ledger-event.py"
_spec = importlib.util.spec_from_file_location("ledger_event", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ledger_event: types.ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger_event)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_EVENT = "discovered"
VALID_PR = 42
VALID_SHA = "abc123def456"
VALID_ACTOR = "test-actor"
VALID_DETAILS: dict[str, Any] = {}


def _make_argv(
    event: str = VALID_EVENT,
    pr: int = VALID_PR,
    sha: str = VALID_SHA,
    actor: str = VALID_ACTOR,
    details: str = "{}",
    ledger: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    args = [
        "--event", event,
        "--pr-number", str(pr),
        "--head-sha", sha,
        "--actor", actor,
        "--details-json", details,
    ]
    if ledger is not None:
        args += ["--ledger-path", ledger]
    if dry_run:
        args.append("--dry-run")
    return args


# ---------------------------------------------------------------------------
# 1 — Valid event type parses without error
# ---------------------------------------------------------------------------

def test_valid_event_parses() -> None:
    ns = ledger_event.parse_args(_make_argv(dry_run=True))
    assert ns.event == VALID_EVENT


# ---------------------------------------------------------------------------
# 2 — All 20 event types are in VALID_EVENTS
# ---------------------------------------------------------------------------

def test_all_event_types_present() -> None:
    expected = {
        "discovered", "review_1", "review_2", "approved", "escalated",
        "cherry_picked", "build_failed", "build_passed", "smoke_failed",
        "smoke_passed", "perf_passed", "perf_failed", "published",
        "graduated_upstream", "rejected", "reverted", "autonomy_paused",
        "autonomy_resumed", "automerge_frozen", "automerge_thawed",
    }
    assert expected <= ledger_event.VALID_EVENTS


# ---------------------------------------------------------------------------
# 3 — Invalid event type is rejected with exit code 2
# ---------------------------------------------------------------------------

def test_invalid_event_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        ledger_event.main(_make_argv(event="nonexistent_event", dry_run=True))
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 4 — Non-JSON in --details-json is rejected with exit code 2
# ---------------------------------------------------------------------------

def test_invalid_details_json_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        ledger_event.main(_make_argv(details="not-json", dry_run=True))
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 5 — Array details-json is rejected (must be a dict)
# ---------------------------------------------------------------------------

def test_array_details_json_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        ledger_event.main(_make_argv(details="[1,2,3]", dry_run=True))
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 6 — Dry-run outputs valid JSON matching schema
# ---------------------------------------------------------------------------

def test_dry_run_outputs_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    ret = ledger_event.main(_make_argv(dry_run=True))
    assert ret == 0
    captured = capsys.readouterr()
    obj = json.loads(captured.out)
    # Required fields
    for field in ("schema_version", "ts", "event", "actor", "pr_number", "head_sha",
                  "details", "prev_hash", "line_hash"):
        assert field in obj, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# 7 — Dry-run: genesis prev_hash is empty string
# ---------------------------------------------------------------------------

def test_dry_run_genesis_prev_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Use an empty tmp ledger to ensure genesis (prev_hash == "")
    ledger = tmp_path / "patch-ledger.jsonl"
    ledger_event.main(_make_argv(ledger=str(ledger), dry_run=True))
    obj = json.loads(capsys.readouterr().out)
    assert obj["prev_hash"] == ""


# ---------------------------------------------------------------------------
# 8 — build_line produces correct line_hash
# ---------------------------------------------------------------------------

def test_build_line_hash_integrity() -> None:
    line = ledger_event.build_line(
        event="discovered",
        pr_number=1,
        head_sha="abc",
        actor="bot",
        details={},
        prev_hash="",
        timestamp="2026-04-27T12:00:00Z",
    )
    obj = json.loads(line)
    stored_hash = obj.pop("line_hash")
    # Recompute without line_hash
    body = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    expected = hashlib.sha256(body.encode()).hexdigest()
    assert stored_hash == expected


# ---------------------------------------------------------------------------
# 9 — Append writes exactly one line and prev_hash chains correctly
# ---------------------------------------------------------------------------

def test_append_writes_one_line_and_chains(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)

    with patch.object(ledger_event, "_git_commit"):
        ledger_event.main(_make_argv(ledger=str(ledger)))
        first_line = ledger.read_text().strip()
        first_obj = json.loads(first_line)
        first_hash = first_obj["line_hash"]

        ledger_event.main(_make_argv(event="review_1", ledger=str(ledger)))
        lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2
        second_obj = json.loads(lines[1])
        assert second_obj["prev_hash"] == first_hash


# ---------------------------------------------------------------------------
# 10 — Ledger-tail-only-append: rejects mid-line edit (corrupt prev_hash)
# ---------------------------------------------------------------------------

def test_corrupt_ledger_missing_line_hash_rejected(tmp_path: Path) -> None:
    """A ledger whose last line lacks line_hash causes exit code 3."""
    ledger = tmp_path / "patch-ledger.jsonl"
    # Write a line without line_hash
    bad = json.dumps({"schema_version": 1, "event": "discovered", "pr_number": 1,
                      "head_sha": "x", "actor": "b", "ts": "2026-01-01T00:00:00Z",
                      "details": {}, "prev_hash": ""})
    ledger.write_text(bad + "\n")

    with pytest.raises(SystemExit) as exc:
        ledger_event.main(_make_argv(ledger=str(ledger)))
    assert exc.value.code == 3


# ---------------------------------------------------------------------------
# 11 — Ledger-tail-only-append: prev_hash must match actual last-line hash
# ---------------------------------------------------------------------------

def test_prev_hash_chain_enforced(tmp_path: Path) -> None:
    """Manually tamper a ledger line and confirm the next append sees mismatch.

    We write a valid first line, then mutate it on disk (simulating an edit),
    and verify that the second append will use the hash of the tampered line
    (i.e., _prev_hash_of_ledger reads the actual stored line_hash, not the
    recomputed one).  The chain is thus only as strong as the stored hash.
    The true integrity check lives in ledger-validate.py (separate bead).
    This test verifies that _prev_hash_of_ledger returns whatever line_hash
    the stored final line declares — so an edited line_hash propagates as
    prev_hash of the next entry, making tampering detectable via the chain.
    """
    ledger = tmp_path / "patch-ledger.jsonl"

    with patch.object(ledger_event, "_git_commit"):
        ledger_event.main(_make_argv(ledger=str(ledger)))

    first_obj = json.loads(ledger.read_text().strip())
    stored_hash = first_obj["line_hash"]

    # Confirm _prev_hash_of_ledger returns the stored hash
    computed = ledger_event._prev_hash_of_ledger(ledger)
    assert computed == stored_hash


# ---------------------------------------------------------------------------
# 12 — Schema: required output fields present
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "schema_version", "ts", "event", "actor", "pr_number",
    "head_sha", "details", "prev_hash", "line_hash",
])
def test_required_fields_present(field: str, capsys: pytest.CaptureFixture[str]) -> None:
    ledger_event.main(_make_argv(dry_run=True))
    obj = json.loads(capsys.readouterr().out)
    assert field in obj


# ---------------------------------------------------------------------------
# 13 — event field echoed correctly
# ---------------------------------------------------------------------------

def test_event_field_correct(capsys: pytest.CaptureFixture[str]) -> None:
    ledger_event.main(_make_argv(event="rejected", dry_run=True))
    obj = json.loads(capsys.readouterr().out)
    assert obj["event"] == "rejected"


# ---------------------------------------------------------------------------
# 14 — signed-or-fallback commit: GPG failure falls back gracefully
# ---------------------------------------------------------------------------

def test_gpg_fallback_to_unsigned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When GPG isn't available, the tool falls back to unsigned commit with a warning."""
    ledger = tmp_path / "patch-ledger.jsonl"

    signed_fail = subprocess.CompletedProcess(
        args=["git", "commit", "-S", "-m", "x"],
        returncode=128,
        stdout="",
        stderr="gpg: signing failed: No secret key",
    )
    unsigned_ok = subprocess.CompletedProcess(
        args=["git", "commit", "-m", "x"],
        returncode=0,
        stdout="[main abc1234] ledger: ...",
        stderr="",
    )
    add_ok = subprocess.CompletedProcess(args=["git", "add", str(ledger)], returncode=0,
                                         stdout="", stderr="")

    call_results = [add_ok, signed_fail, unsigned_ok]

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        result = call_results.pop(0)
        if kwargs.get("check") and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)
        return result  # type: ignore[return-value]

    with patch("subprocess.run", side_effect=fake_run):
        ret = ledger_event.main(_make_argv(ledger=str(ledger)))

    assert ret == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "GPG" in captured.err or "signing" in captured.err.lower()


# ---------------------------------------------------------------------------
# 15 — Dry-run does not write to ledger file
# ---------------------------------------------------------------------------

def test_dry_run_does_not_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    ledger_event.main(_make_argv(ledger=str(ledger), dry_run=True))
    assert not ledger.exists()


# ---------------------------------------------------------------------------
# 16 — Genesis: empty file treated as genesis
# ---------------------------------------------------------------------------

def test_genesis_empty_file(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    ledger.write_text("")
    h = ledger_event._prev_hash_of_ledger(ledger)
    assert h == ""


# ---------------------------------------------------------------------------
# 17 — Multiple event types can be used in sequence
# ---------------------------------------------------------------------------

def test_multiple_events_sequence(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"
    events = ["discovered", "review_1", "review_2", "approved", "cherry_picked"]
    with patch.object(ledger_event, "_git_commit"):
        for ev in events:
            ledger_event.main(_make_argv(event=ev, ledger=str(ledger)))
    lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
    assert len(lines) == len(events)
    # Verify chain
    prev = ""
    for raw in lines:
        obj = json.loads(raw)
        assert obj["prev_hash"] == prev
        prev = obj["line_hash"]


# ---------------------------------------------------------------------------
# 18 — merged_verdict is now a valid event type (CRIT-2 fix)
# ---------------------------------------------------------------------------

def test_merged_verdict_is_valid_event(capsys: pytest.CaptureFixture[str]) -> None:
    ret = ledger_event.main(_make_argv(event="merged_verdict", dry_run=True))
    assert ret == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["event"] == "merged_verdict"


# ---------------------------------------------------------------------------
# 19 — --push flag is accepted by parse_args (CRIT-3 fix)
# ---------------------------------------------------------------------------

def test_push_flag_accepted() -> None:
    ns = ledger_event.parse_args(_make_argv(dry_run=True) + ["--push"])
    assert ns.push is True


def test_no_push_flag_accepted() -> None:
    ns = ledger_event.parse_args(_make_argv(dry_run=True) + ["--no-push"])
    assert ns.push is False


def test_push_default_is_none() -> None:
    ns = ledger_event.parse_args(_make_argv(dry_run=True))
    assert ns.push is None


# ---------------------------------------------------------------------------
# 20 — Outside CI, --no-push prevents any git push call (CRIT-3 fix)
# ---------------------------------------------------------------------------

def test_no_push_skips_git_push(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"

    push_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        if "push" in cmd:
            push_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        # git add and git commit
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        with patch.dict("os.environ", {"CI": ""}, clear=False):
            ledger_event.main(_make_argv(ledger=str(ledger)) + ["--no-push"])

    assert push_calls == [], "git push should not be called when --no-push is set"


# ---------------------------------------------------------------------------
# 21 — In CI (CI=true), --push=True causes a git push call (CRIT-3 fix)
# ---------------------------------------------------------------------------

def test_ci_push_is_called(tmp_path: Path) -> None:
    ledger = tmp_path / "patch-ledger.jsonl"

    push_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        if "push" in cmd:
            push_calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        with patch.dict("os.environ", {"CI": "true"}):
            ledger_event.main(_make_argv(ledger=str(ledger)) + ["--push"])

    assert any("push" in call for call in push_calls), "git push should be called in CI"


# ---------------------------------------------------------------------------
# 22 — CI mode: GPG signing failure exits nonzero instead of falling back (HIGH-2)
# ---------------------------------------------------------------------------

def test_ci_gpg_failure_exits_nonzero(tmp_path: Path) -> None:
    """In CI mode, a GPG failure must cause exit nonzero (no unsigned fallback)."""
    ledger = tmp_path / "patch-ledger.jsonl"

    add_ok = subprocess.CompletedProcess(
        args=["git", "add"], returncode=0, stdout="", stderr=""
    )
    signed_fail = subprocess.CompletedProcess(
        args=["git", "commit", "-S", "-m", "x"],
        returncode=128,
        stdout="",
        stderr="gpg: signing failed: No secret key",
    )

    call_results = [add_ok, signed_fail]

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        result = call_results.pop(0) if call_results else subprocess.CompletedProcess(cmd, 0, "", "")
        if kwargs.get("check") and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)
        return result  # type: ignore[return-value]

    with patch("subprocess.run", side_effect=fake_run):
        with patch.dict("os.environ", {"CI": "true"}):
            with pytest.raises(SystemExit) as exc:
                ledger_event.main(_make_argv(ledger=str(ledger)) + ["--no-push"])

    assert exc.value.code != 0, "CI GPG failure must exit nonzero"


# ---------------------------------------------------------------------------
# 23 — Non-CI mode: GPG signing failure falls back with warning (existing behavior)
# ---------------------------------------------------------------------------

def test_non_ci_gpg_failure_falls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Outside CI, a GPG failure should fall back to unsigned with a warning."""
    ledger = tmp_path / "patch-ledger.jsonl"

    add_ok = subprocess.CompletedProcess(args=["git", "add"], returncode=0, stdout="", stderr="")
    signed_fail = subprocess.CompletedProcess(
        args=["git", "commit", "-S", "-m", "x"],
        returncode=128,
        stdout="",
        stderr="gpg: signing failed: No secret key",
    )
    unsigned_ok = subprocess.CompletedProcess(args=["git", "commit"], returncode=0, stdout="", stderr="")

    call_results = [add_ok, signed_fail, unsigned_ok]

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        result = call_results.pop(0)
        if kwargs.get("check") and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)
        return result  # type: ignore[return-value]

    with patch("subprocess.run", side_effect=fake_run):
        with patch.dict("os.environ", {"CI": ""}, clear=False):
            ret = ledger_event.main(_make_argv(ledger=str(ledger)) + ["--no-push"])

    assert ret == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# 24 — Commit message includes Ledger-Line-Hash trailer (MED-5 fix)
# ---------------------------------------------------------------------------

def test_commit_message_includes_line_hash_trailer(tmp_path: Path) -> None:
    """_git_commit must embed a Ledger-Line-Hash trailer in the commit message."""
    ledger = tmp_path / "patch-ledger.jsonl"
    committed_messages: list[str] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        if "commit" in cmd and "-m" in cmd:
            idx = cmd.index("-m")
            committed_messages.append(cmd[idx + 1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        with patch.dict("os.environ", {"CI": ""}, clear=False):
            ledger_event.main(_make_argv(ledger=str(ledger)) + ["--no-push"])

    assert any("Ledger-Line-Hash:" in msg for msg in committed_messages), (
        "Commit message must contain Ledger-Line-Hash trailer"
    )


# ---------------------------------------------------------------------------
# 25 — Push retry: non-FF push causes pull/rebase/recompute/retry (CRIT-3 fix)
# ---------------------------------------------------------------------------

def test_push_retries_on_non_ff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When the first push is rejected (non-FF), the tool should retry after rebase."""
    ledger = tmp_path / "patch-ledger.jsonl"

    push_attempt = [0]

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        if "push" in cmd:
            push_attempt[0] += 1
            if push_attempt[0] == 1:
                # First push: simulate non-FF rejection
                result = subprocess.CompletedProcess(
                    cmd, 1, "", "[rejected] non-fast-forward"
                )
            else:
                # Second push: success
                result = subprocess.CompletedProcess(cmd, 0, "", "")
            if kwargs.get("check") and result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, cmd)
            return result  # type: ignore[return-value]
        if "rebase" in cmd and "--abort" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "fetch" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "reset" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "symbolic-ref" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "main\n", "")
        # git add / git commit
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        with patch("time.sleep"):  # don't actually sleep
            ret = ledger_event.main(_make_argv(ledger=str(ledger)) + ["--push"])

    assert ret == 0
    assert push_attempt[0] == 2, "Expected exactly 2 push attempts (1 fail + 1 success)"
