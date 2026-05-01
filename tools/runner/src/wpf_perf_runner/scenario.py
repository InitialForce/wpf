"""Scenario loading, validation, and step parsing.

Loads a scenario JSON file, validates it against scenarios/schema.json,
and returns a typed Scenario dataclass ready for the runner.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

# ---------------------------------------------------------------------------
# Schema location — relative to the wpf-perf repo root.
# The runner resolves this at import time by walking up from this file.
# ---------------------------------------------------------------------------

def _find_schema() -> Path:
    """Locate scenarios/schema.json by walking up from this module's location."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "scenarios" / "schema.json"
        if candidate.exists():
            return candidate
    # Fallback: look relative to CWD.
    cwd_candidate = Path.cwd() / "scenarios" / "schema.json"
    if cwd_candidate.exists():
        return cwd_candidate
    raise FileNotFoundError(
        "Cannot locate scenarios/schema.json. "
        "Run from the wpf-perf repo root or set WPFPERF_SCHEMA_PATH."
    )


def _load_schema() -> dict[str, Any]:
    schema_path_env = os.environ.get("WPFPERF_SCHEMA_PATH")
    if schema_path_env:
        schema_path = Path(schema_path_env)
    else:
        schema_path = _find_schema()
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Step dataclasses
# ---------------------------------------------------------------------------

@dataclass
class StepBase:
    type: str
    comment: str = ""


@dataclass
class WaitReadyStep(StepBase):
    timeout_ms: int = 60_000


@dataclass
class ToolStep(StepBase):
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 60_000
    name: str = ""


@dataclass
class WaitIdleStep(StepBase):
    name: str = ""
    max_timeout_ms: int = 30_000
    wait_for_layout_quiescence: bool = False


@dataclass
class WaitMsStep(StepBase):
    ms: int = 0
    name: str = ""


@dataclass
class PerfEventStep(StepBase):
    event: str = ""
    label: str = ""


@dataclass
class ShutdownStep(StepBase):
    timeout_ms: int = 30_000


Step = WaitReadyStep | ToolStep | WaitIdleStep | WaitMsStep | PerfEventStep | ShutdownStep


# ---------------------------------------------------------------------------
# Scenario dataclass
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    description: str
    scenario_version: str
    warmup_steps: list[Step]
    steps: list[Step]
    shutdown_timeout_ms: int = 30_000

    @property
    def all_steps(self) -> list[Step]:
        return self.warmup_steps + self.steps


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_step(raw: dict[str, Any]) -> Step:
    step_type = raw["type"]
    comment = raw.get("comment", "")

    if step_type == "wait-ready":
        return WaitReadyStep(
            type=step_type,
            comment=comment,
            timeout_ms=raw.get("timeoutMs", 60_000),
        )

    if step_type == "tool":
        return ToolStep(
            type=step_type,
            comment=comment,
            tool=raw["tool"],
            args=raw.get("args", {}),
            timeout_ms=raw.get("timeoutMs", 60_000),
            name=raw.get("name", ""),
        )

    if step_type == "wait-idle":
        return WaitIdleStep(
            type=step_type,
            comment=comment,
            name=raw.get("name", ""),
            max_timeout_ms=raw.get("maxTimeoutMs", 30_000),
            wait_for_layout_quiescence=raw.get("waitForLayoutQuiescence", False),
        )

    if step_type == "wait-ms":
        return WaitMsStep(
            type=step_type,
            comment=comment,
            ms=raw["ms"],
            name=raw.get("name", ""),
        )

    if step_type == "perf-event":
        return PerfEventStep(
            type=step_type,
            comment=comment,
            event=raw["event"],
            label=raw.get("label", ""),
        )

    if step_type == "shutdown":
        return ShutdownStep(
            type=step_type,
            comment=comment,
            timeout_ms=raw.get("timeoutMs", 30_000),
        )

    raise ValueError(f"Unknown step type: {step_type!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ScenarioValidationError(Exception):
    """Raised when a scenario JSON file fails schema validation."""


def load_scenario(path: str | Path, *, schema: dict[str, Any] | None = None) -> Scenario:
    """Load and validate a scenario JSON file.

    Args:
        path: Path to the scenario .json file.
        schema: Optional pre-loaded JSON Schema dict. If None, auto-locates
                scenarios/schema.json relative to the repo root.

    Returns:
        Validated Scenario instance.

    Raises:
        ScenarioValidationError: If the JSON is invalid or schema violations found.
        FileNotFoundError: If the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")

    with open(path, encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as exc:
            raise ScenarioValidationError(f"Scenario JSON parse error: {exc}") from exc

    if schema is None:
        schema = _load_schema()

    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as exc:
        raise ScenarioValidationError(
            f"Scenario validation failed at {list(exc.absolute_path)}: {exc.message}"
        ) from exc
    except jsonschema.SchemaError as exc:
        raise ScenarioValidationError(f"Internal schema error: {exc.message}") from exc

    warmup_steps = [_parse_step(s) for s in raw.get("warmupSteps", [])]
    steps = [_parse_step(s) for s in raw["steps"]]

    shutdown_raw = raw.get("shutdown", {})
    shutdown_timeout_ms = shutdown_raw.get("timeoutMs", 30_000) if shutdown_raw else 30_000

    return Scenario(
        name=raw["name"],
        description=raw.get("description", ""),
        scenario_version=raw.get("scenarioVersion", ""),
        warmup_steps=warmup_steps,
        steps=steps,
        shutdown_timeout_ms=shutdown_timeout_ms,
    )
