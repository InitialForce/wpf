"""Tests for tools/upstream-release-poll.py and .github/workflows/upstream-release-poll.yml."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
TOOL = REPO_ROOT / "tools" / "upstream-release-poll.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "upstream-release-poll.yml"
ADOPTION = REPO_ROOT / ".github" / "workflows" / "upstream-stable-adoption.yml"

_spec = importlib.util.spec_from_file_location("upstream_release_poll", TOOL)
assert _spec and _spec.loader
poll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poll)

# Shape of real `git ls-remote --tags` output for dotnet/wpf (2026-10).
LS_REMOTE = """\
cdaabfcfe0331b6e8d7fd2e42a9ca40007b797af\trefs/tags/v10.0.0-rc.1.25451.107
2a8aedc74281421f143b60c2cefd27eced989c27\trefs/tags/v10.0.1
174c8764338348faa85a4ba35cdc7583d21e9da9\trefs/tags/v10.0.10
41784a3ec5f721e18a7799f8399fa4e7fb48d129\trefs/tags/v10.0.104
41784a3ec5f721e18a7799f8399fa4e7fb48d129\trefs/tags/v10.0.105
6e295c0c870baf4c02897e89d53e2f3e4a6aeba5\trefs/tags/v10.0.11
1111111111111111111111111111111111111111\trefs/tags/v10.0.12
3bbc551873ff9afc718eec20077a9118c15b0688\trefs/tags/v10.0.12^{}
2db0df9ea7848f6255f64ee9816ec7efbf16227d\trefs/tags/v10.0.2
44615ed4b9f033922b3361ea02c02f173b8bf82e\trefs/tags/v10.0.9
"""


def test_latest_tag_is_numeric_max_and_ignores_sdk_bands() -> None:
    assert poll.latest_tag(LS_REMOTE, "v10.0.") == (
        "v10.0.12",
        "3bbc551873ff9afc718eec20077a9118c15b0688",  # peeled commit, not the tag object
    )


def test_latest_tag_ignores_previews_and_other_majors() -> None:
    text = "aaa\trefs/tags/v10.0.0-preview.3\nbbb\trefs/tags/v9.0.20\n"
    assert poll.latest_tag(text, "v10.0.") is None


def test_lightweight_tag_uses_its_own_sha() -> None:
    assert poll.latest_tag("abc\trefs/tags/v10.0.3\n", "v10.0.") == ("v10.0.3", "abc")


def test_decide_adopted() -> None:
    assert poll.decide("v10.0.12", True, []) == "skip-adopted"


def test_decide_dispatch_when_no_runs() -> None:
    assert poll.decide("v10.0.12", False, []) == "dispatch"


def test_decide_skips_while_running() -> None:
    runs = [{"displayTitle": "Adopt upstream v10.0.12", "status": "in_progress", "conclusion": ""}]
    assert poll.decide("v10.0.12", False, runs) == "skip-running"


def test_decide_retries_then_gives_up() -> None:
    failed = {"displayTitle": "Adopt upstream v10.0.12", "status": "completed",
              "conclusion": "failure"}
    assert poll.decide("v10.0.12", False, [failed] * 2) == "dispatch"
    assert poll.decide("v10.0.12", False, [failed] * 3) == "skip-gave-up"


def test_decide_ignores_runs_for_other_tags() -> None:
    other = {"displayTitle": "Adopt upstream v10.0.120", "status": "in_progress"}
    assert poll.decide("v10.0.12", False, [other]) == "dispatch"


def test_cli_round_trip() -> None:
    out = subprocess.run(
        [sys.executable, str(TOOL), "latest-tag", "--prefix", "v10.0."],
        input=LS_REMOTE, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out[0] == "v10.0.12"
    out2 = subprocess.run(
        [sys.executable, str(TOOL), "decide", "--tag", "v10.0.12", "--adopted", "false"],
        input=json.dumps([]), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out2 == "dispatch"


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text())
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def test_workflow_daily_and_dispatches_with_app_token() -> None:
    wf = _load(WORKFLOW)
    assert wf["on"]["schedule"][0]["cron"].endswith("* * *")
    raw = WORKFLOW.read_text()
    assert "event_type=upstream-stable-released" in raw
    assert "client_payload[tag]=$TAG" in raw
    steps = {s.get("name"): s for s in wf["jobs"]["poll"]["steps"]}
    assert steps["Dispatch upstream-stable-released"]["env"]["GH_TOKEN"] == (
        "${{ steps.app-token.outputs.token }}"
    )
    assert "needs.gate.outputs.proceed == 'true'" in wf["jobs"]["poll"]["if"]


def test_adoption_run_name_carries_tag_for_dedup() -> None:
    """decide() matches runs by tag in the display title, so the run-name must contain it."""
    wf = _load(ADOPTION)
    assert "Adopt upstream" in wf["run-name"]
    assert "client_payload.tag" in wf["run-name"]
    assert wf["on"]["repository_dispatch"]["types"] == ["upstream-stable-released"]
