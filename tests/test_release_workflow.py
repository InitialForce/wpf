"""
Tests for .github/workflows/release.yml (and release-record.yml)

Trust model being checked:
- No tag-push trigger. release.yml runs only through workflow_dispatch on
  if/main with a `tag` input, so its workflow and tools come from if/main.
- verify-tag treats the tag as data: strict name check before use, signature
  verified with if/main's docs/bot-gpg-pubkey.asc pinned to the
  GH_BOT_GPG_FINGERPRINT fingerprint (no private key), tag commit reachable
  from if/main.
- build calls build.yml on the verified commit with no secrets.
- publish is the only job with NUGET_ORG_API_KEY and needs every gate.
- No ${{ }} expression is expanded inside any run: script.
- The ledger "published" event is written by release-record.yml.
"""

from __future__ import annotations

import json
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


def test_verify_tag_has_only_the_fingerprint_secret(workflow: dict) -> None:
    job = workflow["jobs"]["verify-tag"]
    assert re.findall(r"secrets\.\w+", str(job)) == ["secrets.GH_BOT_GPG_FINGERPRINT"]
    assert job["permissions"] == {"contents": "read"}


def test_verify_tag_requires_if_main(workflow: dict) -> None:
    combined = _steps_combined_run(workflow["jobs"]["verify-tag"])
    assert '"$GITHUB_REF" != "refs/heads/if/main"' in combined


def test_verify_tag_validates_name_before_use(workflow: dict) -> None:
    """The tag name reaches nothing but verify-release-tag.sh, which checks it
    first (tests/test_verify_release_tag.py), before release-version.py."""
    combined = _steps_combined_run(workflow["jobs"]["verify-tag"])
    check = combined.index("tools/verify-release-tag.sh")
    assert combined.index("release-version.py") > check
    assert "git fetch" not in combined
    assert 'echo "Verifying tag' not in combined


def test_tag_regex_rejects_command_substitution() -> None:
    """The bash regex in verify-release-tag.sh must reject shell metacharacters."""
    text = (REPO_ROOT / "tools" / "verify-release-tag.sh").read_text()
    m = re.search(r'=~ (\^if-\S+\$) \]\]', text)
    assert m, "tag regex not found"
    pattern = re.compile(m.group(1))
    assert pattern.match("if-10.0.8-perf.20261008")
    assert pattern.match("if-10.0.8-perf.20261008.2")
    for bad in ("if-10.0.$(id)", "if-10.0.8-perf.20261008;id", "if-10.0.8-perf.2026100`id`"):
        assert not pattern.match(bad), bad


def test_verify_tag_uses_if_main_key_file_and_fingerprint(workflow: dict) -> None:
    """No repository variable: the key comes from the if/main checkout and is
    pinned to the GH_BOT_GPG_FINGERPRINT secret."""
    job = workflow["jobs"]["verify-tag"]
    step = next(s for s in job["steps"] if s.get("id") == "verify")
    assert step["env"] == {"BOT_FPR": "${{ secrets.GH_BOT_GPG_FINGERPRINT }}"}
    combined = _steps_combined_run(job)
    assert "PUBKEY_FILE=docs/bot-gpg-pubkey.asc" in combined
    assert 'MAIN_REF="$GITHUB_SHA"' in combined
    assert "FETCH=1" in combined
    assert "IF_RELEASE_SIGNING_PUBKEY" not in WORKFLOW_PATH.read_text()
    checkout = next(s for s in job["steps"] if "actions/checkout" in str(s.get("uses", "")))
    assert "ref" not in checkout.get("with", {}), "the key file must come from if/main"


def test_verify_tag_errors_on_refusal(workflow: dict) -> None:
    combined = _steps_combined_run(workflow["jobs"]["verify-tag"])
    assert "::error::Release tag refused" in combined
    assert "::warning::Tag" not in combined


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


def test_release_notes_agent_has_no_tools_and_read_only_token(workflow: dict) -> None:
    """The notes agent reads community PR titles: no tools at all, no secrets
    beyond the Anthropic key, read-only GITHUB_TOKEN, no environment."""
    job = workflow["jobs"]["release-notes"]
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job
    claude = next(s for s in job["steps"] if "claude-code-action" in str(s.get("uses", "")))
    args = claude["with"]["claude_args"]
    assert "--allowedTools" not in args
    denied = set(args.split("--disallowedTools")[1].split()[0].split(","))
    assert {"Bash", "Read", "Glob", "Grep", "Write", "Edit", "WebFetch"} <= denied
    assert claude["with"]["github_token"] == "${{ github.token }}"
    combined = _steps_combined_run(job)
    assert "git log" in combined, "the commit list is prepared before the agent runs"
    assert "<untrusted_input>" in combined


def test_release_notes_agent_loads_only_trusted_settings(workflow: dict) -> None:
    """Shared Claude rule (round 2, Astra HIGH): no project or local settings,
    hooks or MCP servers from the checkout, and /proc and the runner's _temp
    folder denied explicitly."""
    job = workflow["jobs"]["release-notes"]
    claude = next(s for s in job["steps"] if "claude-code-action" in str(s.get("uses", "")))
    args = claude["with"]["claude_args"].split()
    assert args[args.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in args
    assert "--mcp-config" not in args
    denied = args[args.index("--disallowedTools") + 1].split(",")
    assert "Read(//proc/**)" in denied
    assert "Read(//home/runner/work/_temp/**)" in denied
    settings = json.loads(claude["with"]["settings"])
    assert settings["disableAllHooks"] is True
    assert "Read(//proc/**)" in settings["permissions"]["deny"]


def test_release_notes_prompt_delimiter_is_random(workflow: dict) -> None:
    """A commit subject equal to a fixed heredoc delimiter could end the
    prompt output early and add other step outputs."""
    step = next(s for s in workflow["jobs"]["release-notes"]["steps"]
                if s.get("id") == "load-prompt")
    assert "openssl rand -hex" in step["run"]
    assert "__EOF_PROMPT__" not in step["run"]


def test_perf_gate_passes_every_report(workflow: dict) -> None:
    """BenchmarkDotNet writes one report per class; all must reach the gate."""
    combined = _steps_combined_run(workflow["jobs"]["perf-on-pack"])
    assert "Select-Object -First 1" not in combined
    assert "--current @($reports.FullName)" in combined


def test_baseline_regeneration_merges_every_report(workflow: dict) -> None:
    combined = _steps_combined_run(workflow["jobs"]["regenerate-perf-baseline"])
    assert "Select-Object -First 1" not in combined
    assert "--merge-only" in combined
    assert "--merged-output perf/baseline.json" in combined
