"""Tests for tools/novel-lines.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "tools" / "novel-lines.py"

_spec = importlib.util.spec_from_file_location("novel_lines", SCRIPT)
assert _spec is not None and _spec.loader is not None
novel_lines: Any = importlib.util.module_from_spec(_spec)
sys.modules["novel_lines"] = novel_lines  # dataclasses look the module up
_spec.loader.exec_module(novel_lines)


def diff(path: str, old: int, new: int, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -{old},3 +{new},3 @@\n" + body
    )


UPSTREAM = diff("src/a.cs", 10, 10, " ctx\n-    int x = 1;\n+    int x = 2;\n")


def report(tmp_path: Path, final: str, base: dict[str, str], head: dict[str, str]) -> Any:
    for root, files in (("base", base), ("head", head)):
        for rel, text in files.items():
            p = tmp_path / root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    return novel_lines.find_novel(final, UPSTREAM, tmp_path / "base", tmp_path / "head")


def test_lines_from_upstream_or_fork_are_not_novel(tmp_path: Path) -> None:
    final = diff(
        "src/a.cs", 20, 20,
        " ctx\n-    int x = 1;\n+    int x = 2;\n+  ForkOnly();\n+\n",
    )
    r = report(tmp_path, final, {"src/a.cs": "ctx\n  ForkOnly();\n"}, {})
    assert r["novel_count"] == 0 and r["novel_lines"] == []


def test_line_from_upstream_head_file_is_not_novel(tmp_path: Path) -> None:
    final = diff("src/a.cs", 1, 1, "+    UpstreamContext();\n")
    r = report(tmp_path, final, {}, {"src/a.cs": "    UpstreamContext();\n"})
    assert r["novel_count"] == 0


def test_whitespace_is_ignored(tmp_path: Path) -> None:
    final = diff("src/a.cs", 1, 1, "+\t\tint x = 2;   \n")
    assert report(tmp_path, final, {}, {})["novel_count"] == 0


def test_invented_line_is_novel_with_its_line_number(tmp_path: Path) -> None:
    final = diff("src/a.cs", 5, 7, " ctx\n+    int x = 2;\n+    Process.Start(\"x\");\n")
    r = report(tmp_path, final, {"src/a.cs": "ctx\n"}, {"src/a.cs": "ctx\n"})
    assert r["novel_count"] == 1
    assert r["novel_lines"] == [{"file": "src/a.cs", "line": 9, "text": '    Process.Start("x");'}]


def test_fork_line_of_another_file_does_not_excuse(tmp_path: Path) -> None:
    final = diff("src/b.cs", 1, 1, "+  Secret();\n")
    r = report(tmp_path, final, {"src/a.cs": "  Secret();\n"}, {})
    assert r["novel_count"] == 1


def test_new_file_and_missing_sides(tmp_path: Path) -> None:
    final = (
        "diff --git a/src/new.cs b/src/new.cs\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/src/new.cs\n@@ -0,0 +1,2 @@\n+int x = 2;\n+Other();\n"
    )
    r = report(tmp_path, final, {}, {})
    assert r["files"] == ["src/new.cs"]
    assert [n["text"] for n in r["novel_lines"]] == ["Other();"]


def test_deleted_file_counts_as_dropped(tmp_path: Path) -> None:
    final = (
        "diff --git a/src/gone.cs b/src/gone.cs\ndeleted file mode 100644\n"
        "--- a/src/gone.cs\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-ForkCode();\n-    int x = 1;\n"
    )
    r = report(tmp_path, final, {}, {})
    assert r["files"] == ["src/gone.cs"]
    assert r["novel_count"] == 0
    # Upstream removed "int x = 1;", so only the fork line is dropped.
    assert r["dropped_lines"] == [{"file": "src/gone.cs", "old_line": 1, "text": "ForkCode();"}]


def test_path_outside_the_side_dir_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "secret.cs").write_text("Hidden();\n")
    final = diff("../secret.cs", 1, 1, "+Hidden();\n")
    assert report(tmp_path, final, {}, {})["novel_count"] == 1


def test_quoted_path() -> None:
    assert novel_lines.unquote_path('"src/a\\tb.cs"') == "src/a\tb.cs"
    assert novel_lines.unquote_path('"src/\\303\\251.cs"') == "src/é.cs"
    assert novel_lines.unquote_path("src/plain.cs") == "src/plain.cs"


def test_no_newline_marker_and_multiple_hunks(tmp_path: Path) -> None:
    final = (
        "diff --git a/src/a.cs b/src/a.cs\n--- a/src/a.cs\n+++ b/src/a.cs\n"
        "@@ -1,1 +1,1 @@\n-old\n\\ No newline at end of file\n+int x = 2;\n"
        "\\ No newline at end of file\n"
        "@@ -50,2 +50,3 @@\n ctx\n+Novel();\n ctx2\n"
    )
    r = report(tmp_path, final, {}, {})
    assert r["novel_lines"] == [{"file": "src/a.cs", "line": 51, "text": "Novel();"}]


def test_list_is_capped_but_count_is_exact(tmp_path: Path) -> None:
    body = "".join(f"+Novel{i}();\n" for i in range(novel_lines.MAX_LISTED + 5))
    r = report(tmp_path, diff("src/a.cs", 1, 1, body), {}, {})
    assert r["novel_count"] == novel_lines.MAX_LISTED + 5
    assert len(r["novel_lines"]) == novel_lines.MAX_LISTED and r["truncated"] is True


def test_cli_writes_report(tmp_path: Path) -> None:
    (tmp_path / "final.diff").write_text(diff("src/a.cs", 1, 1, "+Novel();\n"))
    (tmp_path / "pr.diff").write_text(UPSTREAM)
    out = tmp_path / "out" / "novel-lines.json"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--final-diff", str(tmp_path / "final.diff"),
         "--upstream-diff", str(tmp_path / "pr.diff"), "--base-dir", str(tmp_path / "b"),
         "--upstream-head-dir", str(tmp_path / "h"), "--output", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text())["novel_count"] == 1


def test_cli_refuses_empty_final_diff(tmp_path: Path) -> None:
    (tmp_path / "final.diff").write_text("")
    (tmp_path / "pr.diff").write_text(UPSTREAM)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--final-diff", str(tmp_path / "final.diff"),
         "--upstream-diff", str(tmp_path / "pr.diff"), "--base-dir", str(tmp_path),
         "--upstream-head-dir", str(tmp_path), "--output", str(tmp_path / "o.json")],
        capture_output=True, text=True,
    )
    assert r.returncode == 1 and not (tmp_path / "o.json").exists()
