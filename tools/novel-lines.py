#!/usr/bin/env python3
"""List the lines a conflict resolution added that come from neither side.

pr-ingestion.yml runs this before the resolution review. A cherry-pick that
Claude resolved should only hold lines from the upstream change or from the
fork. Any other added line is "novel": the reviewer must justify each one,
and the report gives it a list that does not depend on a model.

An added line of the final diff (BASE_SHA..BRANCH_SHA) is novel when its
text, with surrounding whitespace removed, is not empty and appears in none of:
  - the added lines of the upstream PR diff (any file)
  - the BASE_SHA version of the same file (<base dir>/<path>)
  - the upstream PR head version of the same file (<upstream head dir>/<path>)
A missing file counts as empty, so its lines cannot excuse anything.

The report also lists "dropped" lines: lines the final diff removes that the
upstream PR diff does not remove. They are for the reviewer only; a
resolution that drops fork code must explain why.

Usage:
  python tools/novel-lines.py --final-diff final.diff --upstream-diff pr.diff \\
      --base-dir base --upstream-head-dir upstream-head --output novel-lines.json

Exit codes: 0 report written, 1 bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The report lists at most this many lines of each kind; the counts are exact.
MAX_LISTED = 500

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


@dataclass
class FileDiff:
    """Added and removed lines of one file in a unified diff, with line numbers."""

    path: str
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)


def unquote_path(raw: str) -> str:
    """Undo git's C-style quoting of a path ("a\\tb" -> a<TAB>b)."""
    if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 == len(body):
            out += ch.encode()
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in "01234567" and re.fullmatch(r"[0-7]{3}", body[i + 1 : i + 4]):
            out.append(int(body[i + 1 : i + 4], 8))
            i += 4
        else:
            out += _ESCAPES.get(nxt, nxt).encode()
            i += 2
    return out.decode("utf-8", errors="replace")


def _strip_prefix(raw: str, prefix: str) -> str | None:
    path = unquote_path(raw.rstrip("\t"))
    if path == "/dev/null":
        return None
    return path[len(prefix):] if path.startswith(prefix) else path


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a git unified diff into per-file added and removed lines.

    Added lines carry their line number in the new file, removed lines their
    number in the old file. A deleted file is listed under its old path.
    """
    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_path: str | None = None
    old_no = new_no = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current, old_path, in_hunk = None, None, False
            continue
        if not in_hunk and line.startswith("--- "):
            old_path = _strip_prefix(line[4:], "a/")
            continue
        if not in_hunk and line.startswith("+++ "):
            new_path = _strip_prefix(line[4:], "b/")
            path = new_path if new_path is not None else old_path
            if path is None:
                current = None
                continue
            current = FileDiff(path)
            files.append(current)
            continue
        match = _HUNK_RE.match(line)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(2))
            in_hunk = current is not None
            continue
        if not in_hunk or current is None:
            continue
        if line.startswith("+"):
            current.added.append((new_no, line[1:]))
            new_no += 1
        elif line.startswith("-"):
            current.removed.append((old_no, line[1:]))
            old_no += 1
        elif line.startswith(" ") or line == "":
            old_no += 1
            new_no += 1
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            in_hunk = False  # extended header of the next file
    return files


def _file_lines(root: Path, rel: str) -> set[str]:
    """Trimmed lines of root/rel, or an empty set when it is missing or outside root."""
    try:
        base = root.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return set()
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines()}


def find_novel(
    final_diff: str, upstream_diff: str, base_dir: Path, head_dir: Path
) -> dict[str, object]:
    """Build the novel-lines report (see the module docstring)."""
    upstream = parse_diff(upstream_diff)
    upstream_added = {text.strip() for f in upstream for _, text in f.added}
    upstream_removed = {text.strip() for f in upstream for _, text in f.removed}

    novel: list[dict[str, object]] = []
    dropped: list[dict[str, object]] = []
    final_files = parse_diff(final_diff)
    for f in final_files:
        known = upstream_added | _file_lines(base_dir, f.path) | _file_lines(head_dir, f.path)
        for number, text in f.added:
            key = text.strip()
            if key and key not in known:
                novel.append({"file": f.path, "line": number, "text": text})
        for number, text in f.removed:
            key = text.strip()
            if key and key not in upstream_removed:
                dropped.append({"file": f.path, "old_line": number, "text": text})

    return {
        "novel_count": len(novel),
        "novel_lines": novel[:MAX_LISTED],
        "dropped_count": len(dropped),
        "dropped_lines": dropped[:MAX_LISTED],
        "truncated": len(novel) > MAX_LISTED or len(dropped) > MAX_LISTED,
        "files": [f.path for f in final_files],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--final-diff", type=Path, required=True)
    parser.add_argument("--upstream-diff", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--upstream-head-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        final_diff = args.final_diff.read_text(encoding="utf-8", errors="replace")
        upstream_diff = args.upstream_diff.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(json.dumps({"error": f"cannot read diff: {exc}"}), file=sys.stderr)
        return 1
    if not final_diff.strip():
        print(json.dumps({"error": "final diff is empty"}), file=sys.stderr)
        return 1

    report = find_novel(final_diff, upstream_diff, args.base_dir, args.upstream_head_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("novel_count", "dropped_count", "truncated")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
