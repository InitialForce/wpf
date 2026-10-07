"""
Tests for .github/workflows/release.yml (and release-record.yml)

Trust model being checked:
- No tag-push trigger. release.yml runs only through workflow_dispatch on
  if/main with a `tag` input, so its workflow and tools come from if/main.
- verify-tag treats the tag as data: strict name check before use, signature
  verified with the public key variable only (no private key, no secrets),
  tag commit reachable from if/main.
- build calls build.yml on the verified commit with no secrets.
- publish is the only job with NUGET_ORG_API_KEY and needs every gate.
- No ${{ }} expression is expanded inside any run: script.
- The ledger "published" event is written by release-record.yml.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW_PATH = WORKFLOWS / "release.yml"
RECORD_PATH = WORKFLOWS / "release-record.yml"

REQUIRED_JOBS = {
    "gate",
    "verify-tag",
    "build",
    "perf-on-pack",
    "release-notes",
    "publish",
}


def _load(path: Path) -> dict:
    with path.open() as fh:
        doc = yaml.safe_load(fh)
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def _normalize_on_key(doc: dict) -> dict:
    if True in doc and "on" not in doc:
        doc = dict(doc)
        doc["on"] = doc.pop(True)
    return doc


def _steps_combined_run(job: dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps", []) if isinstance(s, dict))


def _job_needs_list(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    return list(needs) if isinstance(needs, list) else []


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def record() -> dict:
    return _load(RECORD_PATH)


# ---------------------------------------------------------------------------
# Triggers and top level
# ---------------------------------------------------------------------------
def test_all_required_jobs_present(workflow: dict) -> None:
    missing = REQUIRED_JOBS - set(workflow["jobs"])
    assert not missing, f"Missing required jobs: {missing}"


def test_no_tag_push_trigger(workflow: dict) -> None:
    """A tag push would run the workflow file and tools from the tag itself."""
    on = workflow["on"]
    assert set(on) == {"workflow_dispatch"}, f"only workflow_dispatch allowed; got {set(on)}"


def test_dispatch_has_tag_input(workflow: dict) -> None:
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["tag"]["required"] is True


def test_run_name_carries_tag(workflow: dict) -> None:
    """release-record.yml reads the tag from the run title."""
    assert workflow["run-name"] == "Release ${{ inputs.tag }}"


def test_concurrency_never_cancels(workflow: dict) -> None:
    assert workflow["concurrency"]["group"].startswith("release")
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_top_level_permissions_read_only(workflow: dict) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_no_expression_expanded_inside_run_scripts() -> None:
    """Tag names and other inputs may contain $(...); scripts must read them
    from environment variables, never from ${{ }} expansion."""
    for path in (WORKFLOW_PATH, RECORD_PATH, WORKFLOWS / "auto-release.yml"):
        doc = _load(path)
        for job_name, job in doc["jobs"].items():
            for step in job.get("steps", []):
                run = str(step.get("run", ""))
                assert "${{" not in run, f"{path.name}::{job_name}::{step.get('name')}"


def test_no_private_key_or_app_token_in_release(workflow: dict) -> None:
    text = WORKFLOW_PATH.read_text()
    for forbidden in ("GH_BOT_GPG_KEY", "GH_APP_PRIVATE_KEY", "import-bot-gpg", "github-app-token"):
        assert forbidden not in text, f"release.yml must not use {forbidden}"


def test_no_checkout_persists_credentials(workflow: dict) -> None:
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert (step.get("with") or {}).get("persist-credentials") is False, job_name


# ---------------------------------------------------------------------------
# gate and verify-tag
# ---------------------------------------------------------------------------
def test_gate_passes_requested_action_release_publish(workflow: dict) -> None:
    gate = workflow["jobs"]["gate"]
    assert gate["uses"] == "./.github/workflows/autonomy-check.yml"
    assert gate["with"]["requested_action"] == "release-publish"


def test_verify_tag_gated_on_proceed(workflow: dict) -> None:
    job = workflow["jobs"]["verify-tag"]
    assert "gate" in _job_needs_list(job)
    assert "needs.gate.outputs.proceed == 'true'" in job["if"]


def test_verify_tag_has_no_secrets(workflow: dict) -> None:
    assert "secrets." not in str(workflow["jobs"]["verify-tag"])
    assert workflow["jobs"]["verify-tag"]["permissions"] == {"contents": "read"}


def test_verify_tag_requires_if_main(workflow: dict) -> None:
    combined = _steps_combined_run(workflow["jobs"]["verify-tag"])
    assert '"$GITHUB_REF" != "refs/heads/if/main"' in combined


def test_verify_tag_validates_name_before_use(workflow: dict) -> None:
    combined = _steps_combined_run(workflow["jobs"]["verify-tag"])
    check = combined.index('=~ ^if-[0-9]+')
    for use in ("release-version.py", "git fetch", "echo \"Verifying tag"):
        assert combined.index(use) > check, f"{use} runs before the tag name check"


def test_tag_regex_rejects_command_substitution() -> None:
    """The bash regex used in verify-tag must reject shell metacharacters."""
    combined = _steps_combined_run(_load(WORKFLOW_PATH)["jobs"]["verify-tag"])
    m = re.search(r'=~ (\^if-\S+\$) \]\]', combined)
    assert m, "tag regex not found"
    pattern = re.compile(m.group(1))
    assert pattern.match("if-10.0.8-perf.20261008")
    assert pattern.match("if-10.0.8-perf.20261008.2")
    for bad in ("if-10.0.$(id)", "if-10.0.8-perf.20261008;id", "if-10.0.8-perf.2026100`id`"):
        assert not pattern.match(bad), bad


def test_verify_tag_uses_public_key_variable(workflow: dict) -> None:
    job = workflow["jobs"]["verify-tag"]
    step = next(s for s in job["steps"] if s.get("id") == "verify")
    assert step["env"]["SIGNING_PUBKEY"] == "${{ vars.IF_RELEASE_SIGNING_PUBKEY }}"
    combined = _steps_combined_run(job)
    assert "git verify-tag" in combined
    assert "VALIDSIG" in combined
    assert "GNUPGHOME=$(mktemp -d)" in combined


def test_verify_tag_errors_on_unsigned(workflow: dict) -> None:
    combined = _steps_combined_run(workflow["jobs"]["verify-tag"])
    assert "Unsigned or invalid tag" in combined
    assert "::warning::Tag" not in combined


def test_verify_tag_reachability_uses_if_main(workflow: dict) -> None:
    combined = _steps_combined_run(workflow["jobs"]["verify-tag"])
    assert 'git merge-base --is-ancestor "$TAG_COMMIT" "$GITHUB_SHA"' in combined


def test_version_derived_from_tag(workflow: dict) -> None:
    job = workflow["jobs"]["verify-tag"]
    assert "tools/release-version.py version --tag" in _steps_combined_run(job)
    assert {"tag", "tag_sha", "nuget_version"} <= set(job["outputs"])


# ---------------------------------------------------------------------------
# build, perf
# ---------------------------------------------------------------------------
def test_build_calls_build_workflow_on_verified_commit(workflow: dict) -> None:
    build = workflow["jobs"]["build"]
    assert build["uses"] == "./.github/workflows/build.yml"
    assert "needs.verify-tag.outputs.nuget_version" in build["with"]["package_version"]
    assert "needs.verify-tag.outputs.tag_sha" in build["with"]["ref"]
    assert "secrets" not in build, "build must not receive secrets"


def _called_build_workflow() -> dict:
    return _normalize_on_key(_load(WORKFLOWS / "build.yml"))


def test_build_packs_both_packages() -> None:
    combined = _steps_combined_run(_called_build_workflow()["jobs"]["build-wpf"])
    assert "InitialForce.WPF.csproj" in combined
    assert "InitialForce.WPF.RuntimeOverride.csproj" in combined


def test_called_build_runs_smoke_on_packed_nupkg() -> None:
    jobs = _called_build_workflow()["jobs"]
    assert "build-wpf" in _job_needs_list(jobs["smoke"])
    assert "InitialForceWpfVersion" in _steps_combined_run(jobs["smoke"])


def test_perf_uses_trusted_tools_and_candidate_harness(workflow: dict) -> None:
    job = workflow["jobs"]["perf-on-pack"]
    assert "secrets." not in str(job)
    checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
    assert any("ref" not in (s.get("with") or {}) for s in checkouts), "if/main checkout"
    assert any((s.get("with") or {}).get("path") == "candidate" for s in checkouts)
    combined = _steps_combined_run(job)
    assert "check-regression.py" in combined
    assert "--current-sha" in combined
    assert "--baseline perf/baseline.json" in combined


def test_release_notes_needs_build(workflow: dict) -> None:
    job = workflow["jobs"]["release-notes"]
    assert "build" in _job_needs_list(job)
    assert job.get("if")


def test_release_notes_uses_release_notes_prompt(workflow: dict) -> None:
    assert "release-notes.md" in _steps_combined_run(workflow["jobs"]["release-notes"])


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------
def test_publish_needs_every_gate(workflow: dict) -> None:
    needs = _job_needs_list(workflow["jobs"]["publish"])
    for required in ("verify-tag", "build", "perf-on-pack", "release-notes"):
        assert required in needs


def test_publish_requires_build_and_perf_success(workflow: dict) -> None:
    cond = str(workflow["jobs"]["publish"]["if"])
    assert "needs.verify-tag.result == 'success'" in cond
    assert "needs.build.result == 'success'" in cond
    assert "needs.perf-on-pack.result == 'success'" in cond


def test_publish_job_has_environment_gate(workflow: dict) -> None:
    assert workflow["jobs"]["publish"]["environment"] == "wpf-nuget-publish"


def test_only_publish_gets_nuget_key(workflow: dict) -> None:
    for job_name, job in workflow["jobs"].items():
        if job_name != "publish":
            assert "NUGET_ORG_API_KEY" not in str(job), job_name
    assert "secrets.NUGET_ORG_API_KEY" in str(workflow["jobs"]["publish"])


def test_publish_targets_nuget_org_and_github_packages(workflow: dict) -> None:
    combined = _steps_combined_run(workflow["jobs"]["publish"])
    assert "https://api.nuget.org/v3/index.json" in combined
    assert "nuget.pkg.github.com/initialforce" in combined.lower()


def test_publish_has_sha256_check(workflow: dict) -> None:
    assert "sha256" in _steps_combined_run(workflow["jobs"]["publish"]).lower()


def test_publish_permissions(workflow: dict) -> None:
    perms = workflow["jobs"]["publish"]["permissions"]
    assert perms.get("contents") == "write"
    assert perms.get("packages") == "write"


# ---------------------------------------------------------------------------
# release-record.yml
# ---------------------------------------------------------------------------
def test_record_runs_after_release(record: dict) -> None:
    run = record["on"]["workflow_run"]
    assert run["workflows"] == ["Release"]
    assert run["types"] == ["completed"]
    cond = record["jobs"]["record"]["if"]
    assert "conclusion == 'success'" in cond


def test_record_emits_published_event(record: dict) -> None:
    combined = _steps_combined_run(record["jobs"]["record"])
    assert "ledger-event.py" in combined
    for flag in ("--event published", "--pr-number", "--head-sha", "--actor", "--push"):
        assert flag in combined


def test_record_validates_tag_and_requires_github_release(record: dict) -> None:
    combined = _steps_combined_run(record["jobs"]["record"])
    assert "=~ ^if-[0-9]+" in combined
    assert "gh release view" in combined


def test_record_checks_out_if_main(record: dict) -> None:
    steps = record["jobs"]["record"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "if/main"


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not available")
def test_actionlint_release_yml() -> None:
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH), str(RECORD_PATH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_notes_agent_has_no_bash_and_read_only_token(workflow: dict) -> None:
    """The notes agent reads community PR titles: Read tool only, no secrets
    beyond the Anthropic key, read-only GITHUB_TOKEN, no environment."""
    job = workflow["jobs"]["release-notes"]
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job
    claude = next(s for s in job["steps"] if "claude-code-action" in str(s.get("uses", "")))
    args = claude["with"]["claude_args"]
    assert "--allowedTools Read" in args
    assert "Bash" not in args.split("--disallowedTools")[0]
    assert "Bash" in args.split("--disallowedTools")[1]
    assert claude["with"]["github_token"] == "${{ github.token }}"
    combined = _steps_combined_run(job)
    assert "git log" in combined, "the commit list is prepared before the agent runs"
