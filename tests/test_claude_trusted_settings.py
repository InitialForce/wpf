"""Every Claude step in the ingestion, sync and failure workflows loads only
trusted settings and keeps secrets out of reach of its tools.

Candidate content (an upstream change, a failed run's log) must not be able
to configure Claude Code: no project or local settings, no hooks, no MCP
servers from the work tree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
NAMES = [
    "pr-ingestion.yml", "nightly-rebase.yml", "claude-on-failure.yml", "resolution-reviewer.yml",
]


def _claude_steps(name: str) -> list[tuple[str, dict]]:
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    found = []
    for job_name, job in doc["jobs"].items():
        for step in job.get("steps", []):
            if "claude-code-action" in str(step.get("uses", "")):
                found.append((job_name, step))
    return found


@pytest.mark.parametrize("name", NAMES)
def test_claude_steps_load_only_trusted_settings(name: str) -> None:
    steps = _claude_steps(name)
    assert steps, name
    for job_name, step in steps:
        args = step["with"]["claude_args"]
        assert "--setting-sources user" in args, job_name
        assert "--strict-mcp-config" in args, job_name
        assert "--mcp-config" not in args, job_name
        assert json.loads(step["with"]["settings"]) == {"disableAllHooks": True}, job_name


@pytest.mark.parametrize("name", NAMES)
def test_claude_steps_deny_secrets_and_shell(name: str) -> None:
    for job_name, step in _claude_steps(name):
        args = step["with"]["claude_args"]
        deny = re.search(r'--disallowedTools "([^"]+)"', args)
        assert deny, job_name
        rules = deny.group(1).split(",")
        for rule in (
            "Bash",
            "WebFetch",
            "Read(//proc/**)",
            "Read(//home/runner/work/_temp/**)",
            "Edit(//home/runner/work/_temp/**)",
        ):
            assert rule in rules, f"{job_name}: missing deny {rule}"
        allow = re.search(r'--allowedTools "([^"]+)"', args)
        assert allow, job_name
        bare = {"Read", "Write", "Edit", "Grep", "Glob", "Bash"}
        assert not bare & set(allow.group(1).split(",")), f"{job_name}: unscoped tool rule"


@pytest.mark.parametrize(
    ("name", "job", "key", "prompt"),
    [
        ("pr-ingestion.yml", "resolve-conflicts", "cherry_pick", "cherry-pick.md"),
        ("nightly-rebase.yml", "resolve-conflicts", "conflict_resolve",
         "resolve-rebase-conflict.md"),
    ],
)
def test_resolver_reads_model_from_config_and_retries_once(
    name: str, job: str, key: str, prompt: str
) -> None:
    """The resolver model comes from config.yaml (read before the upstream
    change touches the tree), its tool calls are logged, and a run that leaves
    markers without an escalation file gets exactly one more try."""
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    steps = doc["jobs"][job]["steps"]
    names = [s.get("name", "") for s in steps]
    claude = [s for s in steps if "claude-code-action" in str(s.get("uses", ""))]
    assert len(claude) == 2
    first, second = claude
    for step in claude:
        assert "--model ${{ steps." in step["with"]["claude_args"]
        assert "claude-sonnet" not in step["with"]["claude_args"]
        assert step["with"]["show_full_output"] is True
        assert step["continue-on-error"] is True
    setup = next(s for s in steps if key in s.get("run", ""))
    assert prompt in setup["run"]
    recreate = next(i for i, n in enumerate(names) if n.startswith("Recreate the conflicted"))
    assert steps.index(setup) < recreate
    left = next(s for s in steps if s.get("id") == "left")
    assert "conflict-handoff.sh\" markers" in left["run"]
    assert "[ -f .escalation.md ]" in left["run"]
    assert second["if"] == "steps.left.outputs.retry == 'true'"
    assert steps.index(first) < steps.index(left) < steps.index(second)
    config = yaml.safe_load((REPO_ROOT / ".if-fork" / "config.yaml").read_text())
    assert config["claude_models"][key] == "claude-sonnet-5-5"


@pytest.mark.parametrize("prompt", ["cherry-pick.md", "resolve-rebase-conflict.md"])
def test_resolver_prompt_says_keeping_a_side_is_an_edit(prompt: str) -> None:
    text = (REPO_ROOT / ".if-fork" / "prompts" / prompt).read_text()
    assert "Keeping one side is also an edit" in text
    assert "`^(<<<<<<<|=======|>>>>>>>)` (no quotes around it)" in text
