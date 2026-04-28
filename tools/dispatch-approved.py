#!/usr/bin/env python3
"""Fire GitHub repository_dispatch (or open an issue) based on a merged-verdict JSON.

Usage
-----
  python tools/dispatch-approved.py \\
      --verdict /tmp/merged-verdict.json \\
      --repo InitialForce/wpf \\
      [--token-env GITHUB_TOKEN] \\
      [--dry-run]

Behaviour
---------
- ``verdict == "approved"``   → POST /repos/<repo>/dispatches
                                 event_type: pr-ingestion-requested
                                 client_payload: {pr_number, head_sha,
                                                  review_1_summary, review_2_summary}

- ``verdict == "escalated"``  → POST /repos/<repo>/issues
                                 title: "review-disagreement: PR #<N>"
                                 labels: ["review-disagreement", "needs-human"]

- ``verdict == "rejected"``   → Print the equivalent ledger-event.py invocation
                                 (dry-run) or exec it (live mode).  Exit 0.

- Any other value             → Exit code 2.

Only stdlib is used for HTTP (urllib.request); no third-party packages required.
Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Verdict = dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers — HTTP via urllib.request
# ---------------------------------------------------------------------------

def _github_post(
    *,
    url: str,
    payload: dict[str, Any],
    token: str,
    dry_run: bool,
) -> None:
    """POST *payload* as JSON to *url* using *token* for auth.

    In dry-run mode prints the intended call and body instead of executing it.
    """
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if dry_run:
        print(f"[dry-run] POST {url}")
        print(f"[dry-run] body: {json.dumps(payload, indent=2, ensure_ascii=False)}")
        return

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        status = resp.status
        if status not in (200, 201, 204):
            raise RuntimeError(f"Unexpected HTTP {status} from {url}")


# ---------------------------------------------------------------------------
# Verdict handlers
# ---------------------------------------------------------------------------

def _handle_approved(
    verdict: Verdict,
    repo: str,
    token: str,
    dry_run: bool,
) -> int:
    """Fire a pr-ingestion-requested repository_dispatch event."""
    pr_number: int = verdict["pr_number"]
    head_sha: str = verdict["head_sha"]
    review_1_summary: str = verdict.get("review_1_summary", "")
    review_2_summary: str = verdict.get("review_2_summary", "")

    url = f"https://api.github.com/repos/{repo}/dispatches"
    payload: dict[str, Any] = {
        "event_type": "pr-ingestion-requested",
        "client_payload": {
            "pr_number": pr_number,
            "head_sha": head_sha,
            "review_1_summary": review_1_summary,
            "review_2_summary": review_2_summary,
        },
    }
    _github_post(url=url, payload=payload, token=token, dry_run=dry_run)
    if not dry_run:
        print(
            json.dumps(
                {"status": "dispatched", "event_type": "pr-ingestion-requested",
                 "pr_number": pr_number}
            )
        )
    return 0


def _handle_escalated(
    verdict: Verdict,
    repo: str,
    token: str,
    dry_run: bool,
) -> int:
    """Open a review-disagreement issue on GitHub."""
    pr_number: int = verdict["pr_number"]
    review_1_summary: str = verdict.get("review_1_summary", "")
    review_2_summary: str = verdict.get("review_2_summary", "")

    title = f"review-disagreement: PR #{pr_number}"
    body_lines = [
        f"## Review disagreement on upstream PR #{pr_number}",
        "",
        "The two independent reviewers could not reach a unanimous decision.",
        "Human review is required before this PR can be ingested.",
        "",
        "### Reviewer 1 summary",
        review_1_summary,
        "",
        "### Reviewer 2 summary",
        review_2_summary,
    ]
    issue_body = "\n".join(body_lines)

    url = f"https://api.github.com/repos/{repo}/issues"
    payload: dict[str, Any] = {
        "title": title,
        "body": issue_body,
        "labels": ["review-disagreement", "needs-human"],
    }
    _github_post(url=url, payload=payload, token=token, dry_run=dry_run)
    if not dry_run:
        print(json.dumps({"status": "issue_opened", "title": title}))
    return 0


def _handle_rejected(
    verdict: Verdict,
    repo: str,  # noqa: ARG001  (kept for consistent signature)
    token: str,  # noqa: ARG001
    dry_run: bool,
) -> int:
    """Record a rejected ledger event via tools/ledger-event.py."""
    pr_number: int = verdict["pr_number"]
    head_sha: str = verdict["head_sha"]

    cmd = [
        sys.executable,
        str(Path(__file__).parent / "ledger-event.py"),
        "--event", "rejected",
        "--pr-number", str(pr_number),
        "--head-sha", head_sha,
        "--actor", "dispatch-approved",
        "--details-json", "{}",
    ]

    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return 0

    result = subprocess.run(cmd, capture_output=False)
    return result.returncode


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fire GitHub repository_dispatch on approved PRs."
    )
    parser.add_argument(
        "--verdict",
        required=True,
        metavar="PATH",
        help="Path to merged-verdict JSON file (output of merge-verdicts.py).",
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/NAME",
        help="GitHub repository in owner/name format (e.g. InitialForce/wpf).",
    )
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        metavar="VAR",
        help="Name of environment variable containing the GitHub token "
             "(default: GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the intended API call / command without executing it.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # --- Load verdict ---
    verdict_path = Path(args.verdict)
    if not verdict_path.exists():
        print(
            json.dumps({"error": f"Verdict file not found: {args.verdict}"}),
            file=sys.stderr,
        )
        return 2

    try:
        verdict: Verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(
            json.dumps({"error": f"Invalid JSON in verdict file: {exc}"}),
            file=sys.stderr,
        )
        return 2

    if not isinstance(verdict, dict):
        print(
            json.dumps({"error": "Verdict file must contain a JSON object."}),
            file=sys.stderr,
        )
        return 2

    verdict_value: str = verdict.get("verdict", "")
    if not isinstance(verdict_value, str):
        print(
            json.dumps({"error": "Field 'verdict' must be a string."}),
            file=sys.stderr,
        )
        return 2

    # --- Resolve token (not needed for dry-run) ---
    token = ""
    if not args.dry_run:
        token = os.environ.get(args.token_env, "")
        if not token:
            print(
                json.dumps(
                    {"error": f"Environment variable {args.token_env!r} is not set "
                               "or empty."}
                ),
                file=sys.stderr,
            )
            return 2

    # --- Dispatch based on verdict ---
    handlers = {
        "approved": _handle_approved,
        "escalated": _handle_escalated,
        "rejected": _handle_rejected,
    }

    handler = handlers.get(verdict_value)
    if handler is None:
        print(
            json.dumps(
                {"error": f"Unrecognised verdict: {verdict_value!r}",
                 "valid_verdicts": list(handlers.keys())}
            ),
            file=sys.stderr,
        )
        return 2

    return handler(verdict, args.repo, token, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
