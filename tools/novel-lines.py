#!/usr/bin/env python3
"""List the lines a conflict resolution added that come from neither side.

pr-ingestion.yml runs this before the resolution review. A cherry-pick that
Claude resolved should only hold lines from the upstream change or from the
fork. Any other added line is "novel": the reviewer must justify each one,
and the report gives it a list that does not depend on a model.

All checks are per file, and lines are compared by line_key: surrounding
whitespace removed, except leading whitespace in files where it has meaning
(YAML, Python, makefiles). An added line of the final diff
(BASE_SHA..BRANCH_SHA) is novel when its text is not empty and appears in
none of:
  - the added lines of the upstream diff for the same path
  - the BASE_SHA version of the same file (<base dir>/<path>)
  - the upstream head version of the same file (<upstream head dir>/<path>)
A missing file counts as empty, so its lines cannot excuse anything.

Known text in a new place is not novel, but it can still change behaviour
(a copied "return;", a guard moved below the call it guards). Two more
lists cover that, using the final file (<final dir>/<path>):
  - "reordered": an added line that is not novel but sits where neither
    side has it. The final file is aligned line by line with the base file
    and with the upstream head file (difflib, whitespace trimmed); the line
    matches a position in neither alignment.
  - "duplicated": a line the final file holds more often than expected:
    more than its count in the base file plus the upstream diff's net
    additions of it for that path, and more than its count in the upstream
    head file.

The report also lists "dropped" lines: lines the final diff removes that the
upstream diff does not remove for that path, counted, so one upstream
removal excuses one removal of the same text. These lists are for the
reviewer; only novel_count has a code cap. Zero novel lines does not mean a
resolution is safe.

Usage:
  python tools/novel-lines.py --final-diff final.diff --upstream-diff pr.diff \\
      --base-dir base --upstream-head-dir upstream-head --final-dir final \\
      --output novel-lines.json

Exit codes: 0 report written, 1 bad input.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
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


# Leading whitespace has meaning in these files (YAML and Python nesting,
# make recipes), so only trailing whitespace is ignored there.
_WHITESPACE_SENSITIVE_SUFFIXES = (".py", ".yml", ".yaml", ".mk", ".make", ".tsv")
_WHITESPACE_SENSITIVE_NAMES = ("Makefile", "GNUmakefile")


def line_key(path: str, text: str) -> str:
    """The text a line is compared by: trimmed, or right-trimmed only when
    leading whitespace matters in that kind of file."""
    name = path.rsplit("/", 1)[-1]
    if name in _WHITESPACE_SENSITIVE_NAMES or name.endswith(_WHITESPACE_SENSITIVE_SUFFIXES):
        return text.rstrip()
    return text.strip()


def _read_lines(root: Path | None, rel: str) -> list[str]:
    """Compared lines (line_key) of root/rel, or [] when missing or outside root."""
    if root is None:
        return []
    try:
        base = root.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return []
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line_key(rel, line) for line in text.splitlines()]


def _matched(final: list[str], side: list[str]) -> set[int]:
    """0-based indexes of final lines that align with a line of side."""
    matcher = difflib.SequenceMatcher(None, side, final, autojunk=False)
    out: set[int] = set()
    for block in matcher.get_matching_blocks():
        out.update(range(block.b, block.b + block.size))
    return out


def find_novel(
    final_diff: str,
    upstream_diff: str,
    base_dir: Path,
    head_dir: Path,
    final_dir: Path | None = None,
) -> dict[str, object]:
    """Build the novel-lines report (see the module docstring)."""
    up_added: dict[str, Counter[str]] = {}
    up_removed: dict[str, Counter[str]] = {}
    for f in parse_diff(upstream_diff):
        up_added.setdefault(f.path, Counter()).update(line_key(f.path, t) for _, t in f.added)
        up_removed.setdefault(f.path, Counter()).update(
            line_key(f.path, t) for _, t in f.removed
        )

    novel: list[dict[str, object]] = []
    reordered: list[dict[str, object]] = []
    duplicated: list[dict[str, object]] = []
    dropped: list[dict[str, object]] = []
    final_files = parse_diff(final_diff)
    for f in final_files:
        added_here = up_added.get(f.path, Counter())
        removed_here = up_removed.get(f.path, Counter())
        base = _read_lines(base_dir, f.path)
        head = _read_lines(head_dir, f.path)
        final = _read_lines(final_dir, f.path)
        known = set(added_here) | set(base) | set(head)
        aligned = _matched(final, base) | _matched(final, head) if final else set()
        for number, text in f.added:
            key = line_key(f.path, text)
            if not key:
                continue
            if key not in known:
                novel.append({"file": f.path, "line": number, "text": text})
            elif final and number - 1 not in aligned:
                reordered.append({"file": f.path, "line": number, "text": text})
        if final:
            base_count, head_count = Counter(base), Counter(head)
            for key, count in sorted(Counter(k for k in final if k).items()):
                expected = base_count[key] + added_here[key] - removed_here[key]
                if count > max(expected, head_count[key]) and key in known:
                    duplicated.append({"file": f.path, "text": key, "final_count": count,
                                       "expected_count": max(expected, head_count[key])})
        # Counted: upstream removing a line once from this file excuses one
        # removal of it, not every copy.
        excused = Counter(removed_here)
        for number, text in f.removed:
            key = line_key(f.path, text)
            if not key:
                continue
            if excused[key] > 0:
                excused[key] -= 1
            else:
                dropped.append({"file": f.path, "old_line": number, "text": text})

    lists = (novel, reordered, duplicated, dropped)
    return {
        "novel_count": len(novel),
        "novel_lines": novel[:MAX_LISTED],
        "reordered_count": len(reordered),
        "reordered_lines": reordered[:MAX_LISTED],
        "duplicated_count": len(duplicated),
        "duplicated_lines": duplicated[:MAX_LISTED],
        "dropped_count": len(dropped),
        "dropped_lines": dropped[:MAX_LISTED],
        "truncated": any(len(x) > MAX_LISTED for x in lists),
        "files": [f.path for f in final_files],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--final-diff", type=Path, required=True)
    parser.add_argument("--upstream-diff", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--upstream-head-dir", type=Path, required=True)
    parser.add_argument("--final-dir", type=Path,
                        help="BRANCH_SHA version of each changed file; without it the "
                             "reordered and duplicated lists stay empty")
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

    report = find_novel(
        final_diff, upstream_diff, args.base_dir, args.upstream_head_dir, args.final_dir
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    keys = ("novel_count", "reordered_count", "duplicated_count", "dropped_count", "truncated")
    print(json.dumps({k: report[k] for k in keys}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
