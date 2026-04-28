#!/usr/bin/env python3
"""check-graduated.py — Detect whether a carry-patch has been absorbed into upstream.

For each hunk in a unified-diff patch, extract the post-image content (added lines +
context lines, ignoring removed lines) and search for it verbatim (or near-verbatim with
whitespace tolerance) in the target tree.

Exit codes:
  0 — not-graduated  (patch is still novel; safe to continue applying)
  1 — graduated      (all hunks already present in target; skip this patch)
  2 — partial        (some hunks present, some missing; escalate to human)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Unified diff parsing
# ---------------------------------------------------------------------------

# Regex for the "@@" hunk header: @@ -a,b +c,d @@ optional context
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
# Regex for diff --git a/... b/... file header
_DIFF_FILE_RE = re.compile(r"^diff --git a/.+ b/(.+)$")
# Also handle --- a/path and +++ b/path headers
_MINUS_FILE_RE = re.compile(r"^--- (?:a/)?(.+)$")
_PLUS_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")


@dataclass
class Hunk:
    """One hunk extracted from a unified diff."""

    file_path: str
    """Relative path of the target file (from the +++ header)."""

    new_start: int
    """Line number in the post-image where this hunk begins (1-based)."""

    new_count: int
    """Number of lines in the post-image (added + context)."""

    post_image_lines: list[str]
    """Lines that should exist in the file after applying the patch.

    Each entry is the raw line text *without* the leading diff sigil and
    without the trailing newline.
    """


def parse_hunks(patch_text: str) -> list[Hunk]:
    """Parse *patch_text* (unified diff) and return a list of :class:`Hunk` objects."""
    hunks: list[Hunk] = []
    current_file: str = "/dev/null"
    current_hunk: Hunk | None = None
    hunk_lines_remaining: int = 0

    lines = patch_text.splitlines()

    for raw_line in lines:
        # ---- file header (diff --git) ----
        m = _DIFF_FILE_RE.match(raw_line)
        if m:
            current_file = m.group(1)
            current_hunk = None
            hunk_lines_remaining = 0
            continue

        # ---- +++ b/path header ----
        m_plus = _PLUS_FILE_RE.match(raw_line)
        if m_plus and not raw_line.startswith("+++  "):
            path = m_plus.group(1)
            if path != "/dev/null":
                current_file = path
            current_hunk = None
            hunk_lines_remaining = 0
            continue

        # ---- hunk header @@...@@ ----
        m_hunk = _HUNK_HEADER_RE.match(raw_line)
        if m_hunk:
            new_start = int(m_hunk.group(3))
            # new_count defaults to 1 when omitted in "@@ -a +b @@" form
            new_count_str = m_hunk.group(4)
            new_count = int(new_count_str) if new_count_str is not None else 1

            current_hunk = Hunk(
                file_path=current_file,
                new_start=new_start,
                new_count=new_count,
                post_image_lines=[],
            )
            hunks.append(current_hunk)
            # We count down new_count lines (context + added) for the post-image
            hunk_lines_remaining = new_count
            continue

        # ---- diff body lines ----
        if current_hunk is None:
            continue

        if raw_line.startswith("-"):
            # Removed line — not part of post-image; skip
            continue
        elif raw_line.startswith("+") or raw_line.startswith(" "):
            # Added line (+) or context line ( ) — both appear in the post-image
            current_hunk.post_image_lines.append(raw_line[1:])
            hunk_lines_remaining -= 1
        elif raw_line == "" and hunk_lines_remaining > 0:
            # Blank line inside a hunk body: a context line whose trailing
            # space was stripped by the diff generator.  Treat as empty context.
            current_hunk.post_image_lines.append("")
            hunk_lines_remaining -= 1
        # Lines starting with \ (e.g. "\ No newline at end of file") are ignored

    return hunks


# ---------------------------------------------------------------------------
# Content search
# ---------------------------------------------------------------------------


def _normalize_whitespace(line: str) -> str:
    """Collapse runs of whitespace (tabs, spaces) to a single space and strip."""
    return re.sub(r"\s+", " ", line).strip()


def _lines_match(a: str, b: str, *, strict_whitespace: bool) -> bool:
    if strict_whitespace:
        return a == b
    return _normalize_whitespace(a) == _normalize_whitespace(b)


def _file_contains_sequence(
    file_lines: list[str],
    needle: list[str],
    *,
    strict_whitespace: bool,
) -> tuple[bool, float]:
    """Search *file_lines* for *needle* as a contiguous subsequence.

    Returns *(found, match_score)* where *match_score* is 1.0 if found, or
    the best partial-overlap score if not found.
    """
    if not needle:
        # Empty hunks are trivially present
        return True, 1.0

    n = len(needle)
    best_score = 0.0

    for start in range(len(file_lines) - n + 1):
        matches = 0
        for i, needle_line in enumerate(needle):
            if _lines_match(
                file_lines[start + i], needle_line, strict_whitespace=strict_whitespace
            ):
                matches += 1
        score = matches / n
        if score > best_score:
            best_score = score
        if score == 1.0:
            return True, 1.0

    # Even if not fully matched, check partial overlaps at the boundary
    # (for short needles where file is shorter than needle)
    for start in range(max(0, len(file_lines) - n + 1), len(file_lines)):
        avail = len(file_lines) - start
        matches = 0
        for i in range(avail):
            if i < n and _lines_match(
                file_lines[start + i], needle[i], strict_whitespace=strict_whitespace
            ):
                matches += 1
        score = matches / n
        if score > best_score:
            best_score = score

    return False, best_score


def _search_tree(
    target_root: Path,
    hunk: Hunk,
    *,
    strict_whitespace: bool,
) -> tuple[bool, float]:
    """Find whether *hunk*'s post-image is present in the target tree.

    First looks for the exact file path relative to *target_root*; if not
    found, searches all files with the same basename in the tree.

    Returns *(found, match_score)*.
    """
    # Skip empty hunks
    if not hunk.post_image_lines:
        return True, 1.0

    # Strip leading "b/" that some diff tools include in +++ paths
    rel_path = hunk.file_path
    if rel_path.startswith("b/"):
        rel_path = rel_path[2:]

    candidates: list[Path] = []

    exact = target_root / rel_path
    if exact.is_file():
        candidates.append(exact)
    else:
        # Fall back: all files with the same name anywhere under target_root
        basename = Path(rel_path).name
        candidates = list(target_root.rglob(basename))

    best_found = False
    best_score = 0.0

    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_lines = text.splitlines()
        found, score = _file_contains_sequence(
            file_lines, hunk.post_image_lines, strict_whitespace=strict_whitespace
        )
        if found:
            return True, 1.0
        if score > best_score:
            best_score = score

    return best_found, best_score


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

Verdict = str  # "graduated" | "partial" | "not-graduated"


@dataclass
class HunkResult:
    file: str
    line_range: list[int]
    found: bool
    match_score: float


@dataclass
class CheckResult:
    patch: str
    verdict: Verdict
    hunks: list[HunkResult] = field(default_factory=list)


def compute_verdict(patch_path: str, hunk_results: list[HunkResult]) -> Verdict:
    if not hunk_results:
        # No hunks in patch → nothing to check → treat as not-graduated (safe default)
        return "not-graduated"
    found_count = sum(1 for h in hunk_results if h.found)
    if found_count == len(hunk_results):
        return "graduated"
    if found_count == 0:
        return "not-graduated"
    return "partial"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect whether a carry-patch has been absorbed by upstream. "
            "Exit 0 = not-graduated, 1 = graduated, 2 = partial."
        )
    )
    parser.add_argument(
        "--patch",
        metavar="PATH",
        help="Path to unified diff patch file. Reads from stdin if omitted.",
    )
    parser.add_argument(
        "--target-root",
        metavar="PATH",
        default=".",
        help="Root of the target source tree to search (default: current directory).",
    )
    parser.add_argument(
        "--strict-whitespace",
        action="store_true",
        default=False,
        help=(
            "Require exact whitespace match. "
            "By default whitespace is normalized before comparison."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="Write JSON output to this file instead of stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # ---- Read patch ----
    if args.patch:
        patch_path = Path(args.patch)
        try:
            patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(json.dumps({"error": f"Cannot read patch file: {exc}"}), file=sys.stderr)
            return 3
        patch_label = str(patch_path)
    else:
        patch_text = sys.stdin.read()
        patch_label = "<stdin>"

    # ---- Parse hunks ----
    hunks = parse_hunks(patch_text)

    # ---- Search target tree ----
    target_root = Path(args.target_root)
    if not target_root.is_dir():
        print(
            json.dumps({"error": f"--target-root is not a directory: {target_root}"}),
            file=sys.stderr,
        )
        return 3

    hunk_results: list[HunkResult] = []
    for hunk in hunks:
        found, score = _search_tree(
            target_root, hunk, strict_whitespace=args.strict_whitespace
        )
        hunk_results.append(
            HunkResult(
                file=hunk.file_path,
                line_range=[hunk.new_start, hunk.new_start + hunk.new_count - 1],
                found=found,
                match_score=round(score, 4),
            )
        )

    # ---- Compute verdict ----
    verdict = compute_verdict(patch_label, hunk_results)

    result = CheckResult(patch=patch_label, verdict=verdict, hunks=hunk_results)
    output_obj: dict[str, object] = {
        "patch": result.patch,
        "verdict": result.verdict,
        "hunks": [
            {
                "file": h.file,
                "line_range": h.line_range,
                "found": h.found,
                "match_score": h.match_score,
            }
            for h in result.hunks
        ],
    }
    output_json = json.dumps(output_obj, indent=2)

    if args.output:
        Path(args.output).write_text(output_json + "\n", encoding="utf-8")
    else:
        print(output_json)

    # ---- Exit code ----
    exit_codes: dict[str, int] = {
        "not-graduated": 0,
        "graduated": 1,
        "partial": 2,
    }
    return exit_codes[verdict]


if __name__ == "__main__":
    sys.exit(main())
