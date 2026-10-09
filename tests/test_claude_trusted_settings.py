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
NAMES = ["pr-ingestion.yml", "nightly-rebase.yml", "claude-on-failure.yml"]


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
