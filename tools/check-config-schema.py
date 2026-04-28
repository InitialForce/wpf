#!/usr/bin/env python3
"""Validate .if-fork/config.yaml against the JSON Schema.

Usage:
    python tools/check-config-schema.py \\
        --config .if-fork/config.yaml \\
        --schema tools/config-schema.json \\
        [--output report.json]

Output: JSON validation report →
    { "valid": bool, "config_path": str, "schema_path": str, "errors": [...] }

Exit 0 if valid, 2 if invalid, 1 on internal error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Optional jsonschema import — degrade gracefully to minimal stdlib validator
# ---------------------------------------------------------------------------

try:
    import jsonschema  # type: ignore[import-untyped]
    import jsonschema.validators  # type: ignore[import-untyped]

    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _JSONSCHEMA_AVAILABLE = False


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    """Load a YAML file, returning the parsed Python object."""
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(f"ERROR: PyYAML is required but not installed: {exc}") from exc
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# jsonschema-based validation
# ---------------------------------------------------------------------------


def _validate_with_jsonschema(
    config: Any,
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a list of error dicts using the jsonschema library."""
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)
    errors: list[dict[str, Any]] = []
    for error in sorted(validator.iter_errors(config), key=lambda e: list(e.path)):
        path_str = (
            "/" + "/".join(str(p) for p in error.absolute_path)
            if error.absolute_path
            else "/"
        )
        errors.append(
            {
                "path": path_str,
                "message": error.message,
                "validator": error.validator,
            }
        )
    return errors


# ---------------------------------------------------------------------------
# Minimal stdlib-based validator (degraded mode — no jsonschema)
# ---------------------------------------------------------------------------


def _check_type(
    value: Any,
    type_spec: str | list[str],
    path: str,
    errors: list[dict[str, Any]],
) -> bool:
    """Check that *value* matches *type_spec*; append error and return False on mismatch."""
    type_map: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list,),
        "object": (dict,),
        "null": (type(None),),
    }
    types: list[str] = [type_spec] if isinstance(type_spec, str) else type_spec
    expected: tuple[type, ...] = tuple(
        t for name in types for t in type_map.get(name, (object,))
    )
    if not isinstance(value, expected):
        errors.append(
            {
                "path": path,
                "message": f"Expected type {types!r}, got {type(value).__name__!r}",
                "validator": "type",
            }
        )
        return False
    return True


def _validate_required(
    obj: dict[str, Any],
    required: list[str],
    path: str,
    errors: list[dict[str, Any]],
) -> None:
    for field in required:
        if field not in obj:
            errors.append(
                {
                    "path": path,
                    "message": f"Required property '{field}' is missing",
                    "validator": "required",
                }
            )


def _validate_with_stdlib(
    config: Any,
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    """Minimal structural validator using only the Python stdlib.

    WARNING: This is a degraded fallback. It checks required fields and top-level
    types only. Install the 'jsonschema' package for full draft 2020-12 validation.
    """
    errors: list[dict[str, Any]] = []

    if not _check_type(config, "object", "/", errors):
        return errors

    required: list[str] = schema.get("required", [])
    _validate_required(config, required, "/", errors)

    properties: dict[str, Any] = schema.get("properties", {})
    for key, prop_schema in properties.items():
        if key not in config:
            continue
        value = config[key]
        path = f"/{key}"
        expected_type = prop_schema.get("type")
        if expected_type:
            if not _check_type(value, expected_type, path, errors):
                continue

        # Recurse one level for object properties
        if expected_type == "object" and isinstance(value, dict):
            sub_required: list[str] = prop_schema.get("required", [])
            _validate_required(value, sub_required, path, errors)
            sub_props: dict[str, Any] = prop_schema.get("properties", {})
            for sub_key, sub_schema in sub_props.items():
                if sub_key not in value:
                    continue
                sub_value = value[sub_key]
                sub_path = f"{path}/{sub_key}"
                sub_type = sub_schema.get("type")
                if sub_type:
                    _check_type(sub_value, sub_type, sub_path, errors)

        # Basic array item type check
        if expected_type == "array" and isinstance(value, list):
            items_schema = prop_schema.get("items", {})
            item_type = items_schema.get("type")
            if item_type:
                for idx, item in enumerate(value):
                    _check_type(item, item_type, f"{path}/{idx}", errors)

    return errors


# ---------------------------------------------------------------------------
# Public validation entry point
# ---------------------------------------------------------------------------


def validate(
    config_path: Path,
    schema_path: Path,
) -> dict[str, Any]:
    """Load and validate *config_path* against *schema_path*.

    Returns a result dict with keys: valid, config_path, schema_path, errors.
    """
    # Load schema
    with schema_path.open(encoding="utf-8") as fh:
        schema: dict[str, Any] = json.load(fh)

    # Load config
    config = _load_yaml(config_path)

    warnings: list[str] = []

    if _JSONSCHEMA_AVAILABLE:
        errors = _validate_with_jsonschema(config, schema)
    else:
        warnings.append(
            "jsonschema package not installed — running degraded validation (required "
            "fields and top-level types only). Install jsonschema for full draft 2020-12 "
            "validation."
        )
        errors = _validate_with_stdlib(config, schema)

    result: dict[str, Any] = {
        "valid": len(errors) == 0,
        "config_path": str(config_path),
        "schema_path": str(schema_path),
        "errors": errors,
    }
    if warnings:
        result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate .if-fork/config.yaml against the JSON Schema"
    )
    parser.add_argument(
        "--config",
        default=".if-fork/config.yaml",
        help="Path to config.yaml (default: .if-fork/config.yaml)",
    )
    parser.add_argument(
        "--schema",
        default="tools/config-schema.json",
        help="Path to the JSON Schema file (default: tools/config-schema.json)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write the JSON validation report to",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config)
    schema_path = Path(args.schema)

    if not config_path.exists():
        print(
            json.dumps(
                {
                    "valid": False,
                    "config_path": str(config_path),
                    "schema_path": str(schema_path),
                    "errors": [
                        {
                            "path": "/",
                            "message": f"Config file not found: {config_path}",
                            "validator": "file_exists",
                        }
                    ],
                },
                indent=2,
            )
        )
        return 2

    if not schema_path.exists():
        print(
            json.dumps(
                {
                    "valid": False,
                    "config_path": str(config_path),
                    "schema_path": str(schema_path),
                    "errors": [
                        {
                            "path": "/",
                            "message": f"Schema file not found: {schema_path}",
                            "validator": "file_exists",
                        }
                    ],
                },
                indent=2,
            )
        )
        return 2

    try:
        result = validate(config_path, schema_path)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = json.dumps(result, indent=2)
    print(report)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report + "\n", encoding="utf-8")

    # Print warnings to stderr so they are visible but don't pollute stdout
    if "warnings" in result:
        for w in result["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)

    return 0 if result["valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
