"""
Tests for .github/workflows/auto-release.yml

The workflow only decides and tags; release.yml publishes. Checks:
- daily schedule + workflow_dispatch with a force input
- autonomy gate (release-publish) and gate in needs
- picks the newest green Build run on if/main
- decision delegated to tools/release-version.py plan, fed with published tags
- tag is signed with the bot key and pushed with the App token
- never pushes packages itself
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    with WORKFLOW_PATH.open() as fh:
        doc = yaml.safe_load(fh)
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def _runs(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps", []))


def test_daily_schedule_and_dispatch(workflow: dict) -> None:
    on = workflow["on"]
    crons = [entry["cron"] for entry in on["schedule"]]
    assert len(crons) == 1, "exactly one daily run"
    minute, hour, dom, month, dow = crons[0].split()
    assert (dom, month, dow) == ("*", "*", "*")
    assert hour.isdigit() and minute.isdigit()
    assert on["workflow_dispatch"]["inputs"]["force"]["type"] == "boolean"


def test_no_overlapping_runs(workflow: dict) -> None:
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_gate_release_publish(workflow: dict) -> None:
    jobs = workflow["jobs"]
    assert jobs["gate"]["uses"] == "./.github/workflows/autonomy-check.yml"
    assert jobs["gate"]["with"]["requested_action"] == "release-publish"
    assert "gate" in jobs["tag"]["needs"]
    assert "needs.gate.outputs.proceed == 'true'" in jobs["tag"]["if"]


def test_targets_newest_green_build_on_if_main(workflow: dict) -> None:
    runs = _runs(workflow["jobs"]["tag"])
    assert "--workflow build.yml" in runs
    assert "--branch if/main" in runs
    assert "--status success" in runs


def test_plan_uses_release_version_tool_with_published_tags(workflow: dict) -> None:
    runs = _runs(workflow["jobs"]["tag"])
    assert "tools/release-version.py" in runs
    assert "gh release list" in runs
    assert "--published-tags-file" in runs


def test_tag_is_signed_and_pushed_with_app_token(workflow: dict) -> None:
    steps = workflow["jobs"]["tag"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert "app-token" in checkout["with"]["token"], (
        "the tag push must use the App token, or release.yml will not start"
    )
    assert any(s.get("uses") == "./.github/actions/import-bot-gpg" for s in steps)
    runs = _runs(workflow["jobs"]["tag"])
    assert "git tag -s" in runs
    assert 'git push origin "refs/tags/$TAG"' in runs


def test_never_publishes(workflow: dict) -> None:
    text = WORKFLOW_PATH.read_text()
    assert "nuget push" not in text
    assert "NUGET_ORG_API_KEY" not in text


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not available")
def test_actionlint() -> None:
    result = subprocess.run(["actionlint", str(WORKFLOW_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_dispatches_release_on_if_main(workflow: dict) -> None:
    """release.yml has no tag trigger; auto-release dispatches it on if/main."""
    runs = _runs(workflow["jobs"]["tag"])
    assert "gh workflow run release.yml" in runs
    assert "--ref if/main" in runs
    assert '-f tag="$TAG"' in runs
    assert workflow["permissions"].get("actions") == "write"


def test_runs_release_tag_check_before_pushing(workflow: dict) -> None:
    """The tag is checked with release.yml's own script before the push, and
    no repository variable holds the key."""
    steps = workflow["jobs"]["tag"]["steps"]
    step = next(s for s in steps if s.get("name") == "Create and push signed release tag")
    run = step["run"]
    assert run.index("tools/verify-release-tag.sh") < run.index("git push origin")
    assert "PUBKEY_FILE=docs/bot-gpg-pubkey.asc" in run
    assert step["env"]["BOT_FPR"] == "${{ secrets.GH_BOT_GPG_FINGERPRINT }}"
    assert "IF_RELEASE_SIGNING_PUBKEY" not in WORKFLOW_PATH.read_text()


def test_reconciles_unpublished_tag_before_planning(workflow: dict) -> None:
    """Regression: a lost dispatch left a tag that was never released. The
    reconcile step must run before plan, and plan must not run while a tag
    is being re-dispatched or is still running."""
    steps = workflow["jobs"]["tag"]["steps"]
    ids = [s.get("id") for s in steps]
    assert ids.index("reconcile") < ids.index("redispatch") < ids.index("plan")
    rec = steps[ids.index("reconcile")]
    assert "release-version.py reconcile" in rec["run"]
    assert "--workflow release.yml" in rec["run"]
    plan_if = steps[ids.index("plan")]["if"]
    assert "steps.reconcile.outputs.action != 'wait'" in plan_if
    assert "steps.redispatch.outputs.tag == ''" in plan_if
    redispatch = steps[ids.index("redispatch")]
    assert "tools/verify-release-tag.sh" in redispatch["run"]
    name = "Dispatch release.yml on if/main for the tag"
    dispatch = next(s for s in steps if s.get("name") == name)
    assert "steps.redispatch.outputs.tag" in dispatch["if"]
    assert "for attempt in 1 2 3" in dispatch["run"]
