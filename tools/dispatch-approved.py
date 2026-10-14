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

- ``verdict == "escalated"``  → One open issue per upstream PR. If an open
                                 issue with the PR's marker exists, add a comment;
                                 otherwise POST /repos/<repo>/issues
                                 title: "review-disagreement: PR #<N>"
                                 labels: ["review-disagreement", "needs-human",
                                          "automated"]

- ``verdict == "review_error"`` → Nothing. pr-discovery retries the review.

- ``verdict == "rejected"``   → Nothing to dispatch. pr-review.yml records the
                                 ``rejected`` ledger event itself (one commit with
                                 the review events). With ``--record-rejected`` the
                                 tool appends it via ledger-event.py (manual use).

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


def _github_get(*, url: str, token: str) -> Any:
    """GET *url* and return the parsed JSON body."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Verdict handlers
# ---------------------------------------------------------------------------

ESCALATION_LABELS = ["review-disagreement", "needs-human", "automated"]


def _escalation_marker(pr_number: int) -> str:
    """Hidden marker that identifies the one escalation issue per upstream PR."""
    return f"<!-- if-fork:review-escalation pr={pr_number} -->"


def _find_open_escalation(repo: str, token: str, pr_number: int) -> int | None:
    """Return the number of the open escalation issue for *pr_number*, if any."""
    marker = _escalation_marker(pr_number)
    url = (
        f"https://api.github.com/repos/{repo}/issues"
        "?state=open&labels=review-disagreement&per_page=100"
    )
    for issue in _github_get(url=url, token=token):
        if marker in (issue.get("body") or ""):
            return int(issue["number"])
    return None


def _summary(verdict: Verdict, n: int) -> str:
    """Return reviewer *n*'s summary: an explicit summary field, else its reasoning."""
    explicit = verdict.get(f"review_{n}_summary")
    if isinstance(explicit, str) and explicit:
        return explicit
    review = verdict.get(f"review_{n}")
    if isinstance(review, dict):
        for key in ("reasoning", "rationale"):
            text = review.get(key)
            if isinstance(text, str) and text:
                return f"{review.get('verdict', '?')}: {text}"[:1500]
    return ""


def _handle_approved(
    verdict: Verdict,
    repo: str,
    token: str,
    dry_run: bool,
) -> int:
    """Fire a pr-ingestion-requested repository_dispatch event."""
    pr_number: int = verdict["pr_number"]
    head_sha: str = verdict["head_sha"]
    review_1_summary = _summary(verdict, 1)
    review_2_summary = _summary(verdict, 2)

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
    """Open the escalation issue for this PR, or comment on the open one."""
    pr_number: int = verdict["pr_number"]
    head_sha: str = verdict.get("head_sha", "")
    review_1_summary = _summary(verdict, 1)
    review_2_summary = _summary(verdict, 2)
    details_lines = [
        f"**Head SHA:** `{head_sha}`",
        "",
        f"**Reason:** {str(verdict.get('reasoning', ''))[:3000]}",
        "",
        "### Reviewer 1 summary",
        review_1_summary,
        "",
        "### Reviewer 2 summary",
        review_2_summary,
    ]

    existing = None if dry_run else _find_open_escalation(repo, token, pr_number)
    if existing is not None:
        url = f"https://api.github.com/repos/{repo}/issues/{existing}/comments"
        comment = "\n".join(["Escalated again.", "", *details_lines])
        _github_post(url=url, payload={"body": comment}, token=token, dry_run=dry_run)
        print(json.dumps({"status": "issue_commented", "issue": existing}))
        return 0

    title = f"review-disagreement: PR #{pr_number}"
    body_lines = [
        _escalation_marker(pr_number),
        f"## Upstream PR #{pr_number} needs a human decision",
        "",
        f"https://github.com/dotnet/wpf/pull/{pr_number}",
        "",
        "The automatic review did not approve or reject this PR. The reviewers",
        "disagreed, one asked for a human, a safe verdict was below the tier's",
        "confidence threshold, a deterministic check (hard-fail pattern or",
        "denylisted file) fired, or the review failed three times (a reviewer",
        "left no valid verdict, or the review input could not be prepared).",
        "The reason below says which. Nothing is applied until a human decides.",
        "",
        *details_lines,
    ]
    issue_body = "\n".join(body_lines)

    url = f"https://api.github.com/repos/{repo}/issues"
    payload: dict[str, Any] = {
        "title": title,
        "body": issue_body,
        "labels": ESCALATION_LABELS,
    }
    _github_post(url=url, payload=payload, token=token, dry_run=dry_run)
    if not dry_run:
        print(json.dumps({"status": "issue_opened", "title": title}))
    return 0


def _handle_review_error(
    verdict: Verdict,
    repo: str,  # noqa: ARG001
    token: str,  # noqa: ARG001
    dry_run: bool,  # noqa: ARG001
) -> int:
    """A reviewer left no verdict: nothing to dispatch; pr-discovery retries."""
    print(json.dumps({"status": "review_error", "pr_number": verdict["pr_number"],
                      "note": "no dispatch; pr-discovery retries the review"}))
    return 0


def _handle_rejected(
    verdict: Verdict,
    repo: str,  # noqa: ARG001  (kept for consistent signature)
    token: str,  # noqa: ARG001
    dry_run: bool,
    record: bool = False,
) -> int:
    """Rejected: nothing to dispatch; optionally record the ledger event."""
    pr_number: int = verdict["pr_number"]
    if not record:
        print(json.dumps({"status": "rejected", "pr_number": pr_number,
                          "note": "no dispatch; ledger event recorded by caller"}))
        return 0
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
        "--record-rejected",
        action="store_true",
        help="On a rejected verdict, append the 'rejected' ledger event "
             "(pr-review.yml records it itself, so it does not pass this).",
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
        "review_error": _handle_review_error,
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

    if handler is _handle_rejected:
        return _handle_rejected(verdict, args.repo, token, args.dry_run, args.record_rejected)
    return handler(verdict, args.repo, token, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
