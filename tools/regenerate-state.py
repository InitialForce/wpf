#!/usr/bin/env python3
"""Replay .if-fork/patch-ledger.jsonl events to derive .if-fork/patch-state.json.

Usage::

    python tools/regenerate-state.py [--ledger-path PATH] [--state-path PATH] [--dry-run] [--check]

The script is idempotent — same input always produces the same output
(sorted JSON keys, deterministic timestamp formatting).

Exit codes:
    0  Success (or --check and state matches)
    1  --check failed (committed state does not match replay)
    2  Invalid arguments / ledger parse error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LEDGER_PATH: str = ".if-fork/patch-ledger.jsonl"
DEFAULT_STATE_PATH: str = ".if-fork/patch-state.json"

# Events that directly set the top-level "state" field of a PR record.
# Every event listed here is valid per §10.5.
_STATE_EVENTS: frozenset[str] = frozenset(
    [
        "discovered",
        "review_1",
        "review_2",
        "approved",
        "escalated",
        "cherry_picked",
        "build_failed",
        "build_passed",
        "smoke_failed",
        "smoke_passed",
        "perf_failed",
        "perf_passed",
        "published",
        "graduated_upstream",
        "rejected",
        "reverted",
        "autonomy_paused",
        "autonomy_resumed",
        "automerge_frozen",
        "automerge_thawed",
    ]
)

# ---------------------------------------------------------------------------
# Error helper
# ---------------------------------------------------------------------------


def _die(code: int, message: str, **extra: object) -> None:
    payload: dict[str, object] = {"error": message, **extra}
    print(json.dumps(payload), file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Core replay logic
# ---------------------------------------------------------------------------


def replay_ledger(ledger_text: str) -> dict[int, dict[str, Any]]:
    """Replay ledger lines forward and return a per-PR state dict.

    Lines that are blank or cannot be parsed as JSON are silently skipped
    (malformed-line tolerance).  Unknown event types are also tolerated so that
    older scripts can still replay ledgers produced by newer tool versions.

    Returns a dict mapping ``pr_number`` (int) to a state record.
    """
    state: dict[int, dict[str, Any]] = {}

    for raw in ledger_text.splitlines():
        line = raw.strip()
        if not line:
            continue

        try:
            event_obj: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            # Malformed line — skip, do not abort
            continue

        # Must be a dict with at least 'event' and 'pr_number'
        if not isinstance(event_obj, dict):
            continue
        event_type = event_obj.get("event")
        pr_number = event_obj.get("pr_number")
        if not isinstance(event_type, str) or not isinstance(pr_number, int):
            continue

        ts: str = event_obj.get("ts", "")
        head_sha: str | None = event_obj.get("head_sha") or None
        details: dict[str, Any] = event_obj.get("details") or {}

        if pr_number not in state:
            # Initialise the record on first-seen event (typically 'discovered')
            state[pr_number] = {
                "pr_number": pr_number,
                "state": event_type,
                "head_sha": head_sha,
                "applied_commit": None,
                "applied_branch": None,
                "tier": event_obj.get("if_tier"),
                "review_1_verdict": None,
                "review_2_verdict": None,
                "review_verdicts": [],
                "review_disagreement": False,
                "graduated_upstream": False,
                "first_seen": ts,
                "last_event": ts,
                "last_timestamp": ts,
                "first_published_in": None,
            }
        else:
            rec = state[pr_number]
            rec["last_event"] = event_type
            rec["last_timestamp"] = ts
            if ts:
                rec["last_event"] = event_type

        rec = state[pr_number]

        # Update state field — every event type advances (or sets) the state
        rec["state"] = event_type

        # Capture head_sha from 'discovered' (locks SHA for downstream steps)
        # but also accept updates from later events if explicitly provided
        if head_sha is not None and (
            event_type == "discovered" or rec["head_sha"] is None
        ):
            rec["head_sha"] = head_sha

        # Tier may be set on any event; prefer the one from 'discovered'
        if_tier = event_obj.get("if_tier")
        if if_tier is not None and rec["tier"] is None:
            rec["tier"] = if_tier

        # review_1 / review_2 verdicts
        if event_type == "review_1":
            verdict = details.get("verdict")
            rec["review_1_verdict"] = verdict
            _append_verdict(rec, "review_1", verdict, ts, details)
        elif event_type == "review_2":
            verdict = details.get("verdict")
            rec["review_2_verdict"] = verdict
            _append_verdict(rec, "review_2", verdict, ts, details)
            # Check for disagreement
            r1 = rec.get("review_1_verdict")
            r2 = rec.get("review_2_verdict")
            if r1 is not None and r2 is not None and r1 != r2:
                rec["review_disagreement"] = True

        # cherry_picked — capture applied_commit and applied_branch
        elif event_type == "cherry_picked":
            rec["applied_commit"] = details.get("applied_commit") or rec.get(
                "applied_commit"
            )
            rec["applied_branch"] = details.get("applied_branch") or rec.get(
                "applied_branch"
            )

        # published — capture nuget version
        elif event_type == "published":
            if rec.get("first_published_in") is None:
                rec["first_published_in"] = details.get("nuget_version")

        # graduated_upstream
        elif event_type == "graduated_upstream":
            rec["graduated_upstream"] = True

    return state


def _append_verdict(
    rec: dict[str, Any],
    review_key: str,
    verdict: str | None,
    ts: str,
    details: dict[str, Any],
) -> None:
    """Append a review verdict entry to rec['review_verdicts']."""
    entry: dict[str, Any] = {
        "review": review_key,
        "verdict": verdict,
        "ts": ts,
    }
    confidence = details.get("confidence")
    if confidence is not None:
        entry["confidence"] = confidence
    model = details.get("model")
    if model is not None:
        entry["model"] = model
    # Replace any existing entry for the same review key (idempotent replay)
    verdicts: list[dict[str, Any]] = rec.setdefault("review_verdicts", [])
    rec["review_verdicts"] = [v for v in verdicts if v.get("review") != review_key]
    rec["review_verdicts"].append(entry)
    # Keep verdicts in deterministic order
    rec["review_verdicts"].sort(key=lambda v: v.get("review", ""))


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def build_state_json(state: dict[int, dict[str, Any]]) -> str:
    """Serialise state dict to a deterministic JSON string.

    Keys are sorted; top-level keys are pr_number (as string, for JSON
    compatibility).  Produces a trailing newline.
    """
    # Top-level dict keyed by str(pr_number) for JSON spec compliance.
    output: dict[str, Any] = {
        str(pr): _normalise_record(rec) for pr, rec in sorted(state.items())
    }
    return json.dumps(output, indent=2, sort_keys=True) + "\n"


def _normalise_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Return a normalised copy of a PR record with canonical field ordering."""
    # Clone so we don't mutate the replay state
    out = dict(rec)
    # Ensure review_verdicts is sorted and each entry sorted
    verdicts = out.get("review_verdicts", [])
    out["review_verdicts"] = sorted(
        (dict(sorted(entry.items())) for entry in verdicts),
        key=lambda e: e.get("review", ""),
    )
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay patch-ledger.jsonl to derive patch-state.json"
    )
    parser.add_argument(
        "--ledger-path",
        default=DEFAULT_LEDGER_PATH,
        help=f"Path to ledger file (default: {DEFAULT_LEDGER_PATH})",
    )
    parser.add_argument(
        "--state-path",
        default=DEFAULT_STATE_PATH,
        help=f"Path to output state file (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print derived state to stdout instead of writing to --state-path",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Derive state and compare against committed --state-path. "
            "Exit 0 if identical, exit 1 if different."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    ledger_path = Path(args.ledger_path)
    state_path = Path(args.state_path)

    # Read ledger
    if not ledger_path.exists():
        _die(2, f"Ledger file not found: {ledger_path}")
    try:
        ledger_text = ledger_path.read_text(encoding="utf-8")
    except OSError as exc:
        _die(2, f"Cannot read ledger: {exc}")

    # Replay
    state = replay_ledger(ledger_text)

    # Serialise
    derived_json = build_state_json(state)

    if args.dry_run:
        sys.stdout.write(derived_json)
        return 0

    if args.check:
        if not state_path.exists():
            print(
                json.dumps(
                    {"check": "fail", "reason": f"state file not found: {state_path}"}
                ),
                file=sys.stderr,
            )
            return 1
        committed_json = state_path.read_text(encoding="utf-8")
        if derived_json == committed_json:
            print(json.dumps({"check": "pass"}))
            return 0
        else:
            print(
                json.dumps({"check": "fail", "reason": "state does not match ledger"}),
                file=sys.stderr,
            )
            return 1

    # Write output
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(derived_json, encoding="utf-8")
    print(
        json.dumps(
            {"status": "ok", "pr_count": len(state), "state_path": str(state_path)}
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
