"""Run-block hygiene for the pipeline workflows.

Event, dispatch, tag and ref values must reach shell through env, never
through ${{ }} inside run:, where they would be pasted into the script as
code. Every run block must also parse as bash (or be PowerShell).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
PIPELINE = [
    "automerge.yml",
    "autonomy-check.yml",
    "claude-on-failure.yml",
    "nightly-rebase.yml",
    "pr-ingestion.yml",
    "upstream-release-poll.yml",
    "upstream-stable-adoption.yml",
    "weekly-differential.yml",
]


def _steps(name: str):
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    for job_name, job in doc["jobs"].items():
        defaults = ((job.get("defaults") or {}).get("run") or {}).get("shell")
        for step in job.get("steps") or []:
            if "run" in step:
                yield job_name, step, step.get("shell", defaults)


@pytest.mark.parametrize("name", PIPELINE)
def test_no_expressions_inside_run(name: str) -> None:
    for job_name, step, _ in _steps(name):
        assert "${{" not in step["run"], f"{name} {job_name}: {step.get('name')}"


@pytest.mark.parametrize("name", PIPELINE)
def test_bash_run_blocks_parse(name: str) -> None:
    for job_name, step, shell in _steps(name):
        if shell in ("pwsh", "powershell", "cmd"):
            continue
        r = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True)
        assert r.returncode == 0, f"{name} {job_name}: {step.get('name')}: {r.stderr}"
