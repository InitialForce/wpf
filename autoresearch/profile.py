#!/usr/bin/env python3
"""Tier A scenario profile capture.

Two modes:
  --run     Drive spike-9 with EventPipe sampling, then analyze its trace.
  --trace   Skip spike; analyze an existing .nettrace file.

Output: ranked /c/work/wpf-perf/autoresearch/profile.json with the top-K
WPF methods by CPU sample count. Allocation attribution is Phase 2.

The CLR sample profiler runs at ~1 kHz so a 10-second spike yields ~10 000
samples. Filtering for WPF-source-tree namespaces (System.Windows, MS.Internal,
PresentationCore, PresentationFramework, WindowsBase, System.Xaml) typically
captures 30–60% of total samples in a layout/render-bound spike.

The output ranking is the menu the inner loop picks from. Each entry includes
a `bdn_filter` field IF a microbenchmark for that method already exists; if
not, the entry is marked needs_benchmark=true and the orchestrator's
benchmark-writer pass will scaffold one.

Usage:
  python3 profile.py --run                              # full pipeline
  python3 profile.py --trace path/to/spike.nettrace     # analyze existing
  python3 profile.py --run --top-k 50                   # broader ranking
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.resolve()
WPF_REPO = Path("/c/work/wpf-perf")
SPIKE = WPF_REPO / "tools" / "poc" / "spike-9-play-take.py"
PROFILE_JSON = ROOT / "profile.json"
PROFILE_OUTPUT_DIR = ROOT / "profile-output"
MICROBENCH_DIR = WPF_REPO / "microbench" / "Benchmarks"

DEFAULT_TOP_K = 30
SPIKE_TIMEOUT_S = int(os.environ.get("WPF_AR_PROFILE_SPIKE_TIMEOUT", "300"))
CONVERT_TIMEOUT_S = 120

# Methods we consider "hot path" candidates for optimization. Anything else
# (BCL, 3rd party, BDN harness internals) is filtered out.
WPF_NAMESPACE_PATTERNS = (
    "System.Windows.",
    "MS.Internal.",
    "System.Xaml.",
)

# Modules — fallback when the frame name lacks a namespace prefix.
WPF_MODULE_NAMES = (
    "PresentationCore",
    "PresentationFramework",
    "WindowsBase",
    "System.Xaml",
    "PresentationUI",
    "ReachFramework",
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[profile] {msg}", flush=True)


def to_winpath(p: Path) -> str:
    s = str(p)
    if s.startswith("/c/"):
        return "C:\\" + s[3:].replace("/", "\\")
    return s


def cmd(argv: list[str], cwd: Path | None = None, timeout: int | None = None) -> tuple[int, str]:
    p = subprocess.run(
        argv, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def find_dotnet_trace() -> str:
    """Locate dotnet-trace.exe (installed as a global tool)."""
    home = Path(os.environ.get("USERPROFILE", "/c/users/oystein"))
    candidates = [
        home / ".dotnet" / "tools" / "dotnet-trace.exe",
        Path("/c/Users/oystein/.dotnet/tools/dotnet-trace.exe"),
    ]
    for c in candidates:
        if c.exists():
            return to_winpath(c)
    # Try PATH
    rc, out = cmd(["cmd.exe", "/c", "where", "dotnet-trace"])
    if rc == 0 and out.strip():
        return out.strip().splitlines()[0]
    raise FileNotFoundError("dotnet-trace not found; install via "
                            "`dotnet tool install -g dotnet-trace`")


# ─── Spike runner ─────────────────────────────────────────────────────────────


def run_spike() -> Path:
    """Drive spike-9 once, return the path to its .nettrace output."""
    PROFILE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SPIKE9_RESULT_DIR"] = str(PROFILE_OUTPUT_DIR)
    env["SPIKE9_NAME"] = "profile"
    env["MC_PERF_MODE"] = "1"
    log(f"running spike-9 → {PROFILE_OUTPUT_DIR}")
    p = subprocess.run(
        ["python3", str(SPIKE)], cwd=str(WPF_REPO),
        env=env, timeout=SPIKE_TIMEOUT_S,
    )
    if p.returncode != 0:
        raise RuntimeError(f"spike-9 failed (rc={p.returncode})")
    candidates = sorted(PROFILE_OUTPUT_DIR.glob("*.nettrace"),
                        key=lambda f: f.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no .nettrace produced in {PROFILE_OUTPUT_DIR}")
    log(f"  captured {candidates[0].name}")
    return candidates[0]


# ─── Speedscope conversion + aggregation ──────────────────────────────────────


def convert_to_speedscope(nettrace: Path) -> Path:
    """`dotnet-trace convert --format speedscope` → JSON path.

    `dotnet-trace convert` always appends `.speedscope.json` to whatever you
    pass via --output. To get `<stem>.speedscope.json`, we pass the bare stem
    (without extension) and let the tool add the suffix itself.
    """
    out = nettrace.with_suffix(".speedscope.json")
    if out.exists() and out.stat().st_mtime > nettrace.stat().st_mtime:
        log(f"  speedscope cached: {out.name}")
        return out
    trace_exe = find_dotnet_trace()
    log(f"  converting {nettrace.name} → speedscope …")
    out_arg = nettrace.with_suffix("")  # tool appends .speedscope.json
    rc, output = cmd(
        ["cmd.exe", "/c", trace_exe, "convert",
         to_winpath(nettrace),
         "--format", "speedscope",
         "--output", to_winpath(out_arg)],
        timeout=CONVERT_TIMEOUT_S,
    )
    if rc != 0 or not out.exists():
        raise RuntimeError(f"dotnet-trace convert failed:\n{output}")
    return out


def is_wpf_method(frame_name: str) -> bool:
    """True if the frame is a method we care about optimizing."""
    if any(frame_name.startswith(ns) for ns in WPF_NAMESPACE_PATTERNS):
        return True
    # Some frames have form `Module!Method` — check module
    if "!" in frame_name:
        module, _ = frame_name.split("!", 1)
        if module in WPF_MODULE_NAMES:
            return True
    return False


def aggregate_speedscope(speedscope_path: Path) -> tuple[Counter, float]:
    """Return (Counter[frame_name] → inclusive time in ms, total wall time).

    dotnet-trace's TraceEvent speedscope exporter emits **evented** profiles
    (`type: "evented"`, with `events` of `{type:'O'|'C', frame:idx, at:ms}`),
    not sampled ones. The leaf frames are dominated by synthetic
    UNMANAGED_CODE_TIME / CPU_TIME pseudo-frames, which is useless for
    ranking managed WPF methods. Instead we charge every dt to **every**
    frame on the stack — this gives inclusive time (how long was the method
    on the call path), which is what you want when picking optimization
    targets in a managed framework.

    `total` is wall-clock time of the trace (sum of dt regardless of stack
    depth), so cpu_pct = inclusive_ms / total_ms can exceed 100% for
    intermediate frames — that's expected and fine for ranking purposes.
    """
    data = json.loads(speedscope_path.read_text(encoding="utf-8"))
    shared = data.get("shared", {})
    frames = shared.get("frames", [])
    profiles = data.get("profiles", [])

    counts: Counter = Counter()
    total = 0.0
    for prof in profiles:
        events = prof.get("events", [])
        if not events:
            continue
        stack: list[int] = []
        last_t = events[0].get("at", 0.0)
        for ev in events:
            now = ev.get("at", last_t)
            dt = now - last_t
            if dt > 0 and stack:
                total += dt
                # Charge dt to every frame currently on the stack.
                # Avoid double-charging if the same frame appears twice
                # (recursion) — count it once per stack-occurrence.
                seen: set[int] = set()
                for fidx in stack:
                    if fidx in seen:
                        continue
                    seen.add(fidx)
                    if 0 <= fidx < len(frames):
                        name = frames[fidx].get("name", "")
                        counts[name] += dt
            etype = ev.get("type")
            fidx = ev.get("frame")
            if etype == "O":
                stack.append(fidx)
            elif etype == "C":
                # Pop until we find the matching frame (defensive — rare in
                # well-formed speedscope output but cheap to tolerate).
                while stack and stack[-1] != fidx:
                    stack.pop()
                if stack:
                    stack.pop()
            last_t = now
    return counts, total


# ─── Benchmark mapping ────────────────────────────────────────────────────────


def existing_benchmarks() -> dict[str, str]:
    """Scan microbench/Benchmarks/*.cs and return {Type.Method: bdn_glob}.

    For each benchmark file `<Stem>Benchmark.cs`, harvest every
    `Type.Method(` call that looks like a WPF API (matches a known WPF
    namespace prefix in surrounding `using` lines, OR ends in a recognized
    Type-name pattern). The map key is the bare `Type.Method` string;
    callers use substring containment to match against profile entries
    that arrive in `Module!FullyQualifiedName(...)` form.
    """
    out: dict[str, str] = {}
    if not MICROBENCH_DIR.exists():
        return out
    # Common WPF-API class names whose appearance in benchmark code we can
    # safely treat as covered. Extend this list as new benchmarks land.
    KNOWN_WPF_TYPES = (
        "Geometry", "PathGeometry", "StreamGeometry",
        "Brush", "SolidColorBrush", "LinearGradientBrush",
        "Transform", "MatrixTransform", "TransformGroup",
        "FrameworkElement", "UIElement", "Visual",
        "DependencyObject", "DependencyProperty",
        "Dispatcher", "DispatcherOperation",
        "Color", "Matrix", "Rect", "Size", "Point", "Vector",
        "XamlReader", "XamlWriter",
        "Border", "Panel", "Grid", "StackPanel",
    )
    for f in MICROBENCH_DIR.glob("*.cs"):
        text = f.read_text(encoding="utf-8")
        glob = f"*{f.stem.replace('Benchmark', '')}*"
        for m in re.finditer(r"\b([A-Z]\w+)\.([A-Z]\w+)\s*\(", text):
            type_name, method_name = m.group(1), m.group(2)
            if type_name not in KNOWN_WPF_TYPES:
                continue
            key = f"{type_name}.{method_name}"
            if key not in out:
                out[key] = glob
    return out


def find_bench_filter(method: str, benchmarks: dict[str, str]) -> str | None:
    """Return the bdn_filter glob if any known `Type.Method` substring
    appears in the profile method name, else None.

    Profile entries arrive as `Module!FullyQualifiedName(args)`, e.g.
    `PresentationCore!System.Windows.Media.Geometry.Parse(class System.String)`.
    A benchmark covers the entry if the entry's name contains the
    benchmark's `Type.Method` followed by `(` — accept either a leading
    dot (real profile entries with namespace) or whitespace/start
    (synthetic always-include entries).
    """
    for type_method, glob in benchmarks.items():
        for prefix in (".", " ", "!"):
            if f"{prefix}{type_method}(" in method:
                return glob
        if method.startswith(f"{type_method}("):
            return glob
    return None


# ─── Output ───────────────────────────────────────────────────────────────────


def write_profile_json(ranked: list[tuple[str, int, float]],
                       benchmarks: dict[str, str],
                       source: str) -> None:
    PROFILE_JSON.write_text(json.dumps({
        "schema_version": 1,
        "phase": 1,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "notes": [
            "Tier A profile: top-K WPF methods by CPU sample-count from a",
            "single spike-9 run. CPU only — allocation attribution Phase 2.",
            "Entries with bdn_filter:null have no covering microbenchmark.",
            "The orchestrator's benchmark-writer pass scaffolds new ones.",
        ],
        "hot_paths": [
            {
                "method": method,
                "samples": round(samples, 2),
                "cpu_pct_total": round(pct, 2),
                "bdn_filter": find_bench_filter(method, benchmarks),
                "needs_benchmark": find_bench_filter(method, benchmarks) is None,
            }
            for method, samples, pct in ranked
        ],
    }, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {PROFILE_JSON} ({len(ranked)} entries)")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", action="store_true",
                   help="Run spike-9 first, then analyze its trace.")
    g.add_argument("--trace", type=Path,
                   help="Path to existing .nettrace; skip spike.")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                    help=f"Number of hot paths to retain (default {DEFAULT_TOP_K})")
    args = ap.parse_args()

    if args.run:
        nettrace = run_spike()
    else:
        nettrace = args.trace.resolve()
        if not nettrace.exists():
            log(f"FATAL: trace not found: {nettrace}")
            return 1

    speedscope = convert_to_speedscope(nettrace)
    counts, total = aggregate_speedscope(speedscope)
    log(f"  total time: {total:.1f}ms; unique frames: {len(counts)}")

    wpf_counts = Counter({k: v for k, v in counts.items() if is_wpf_method(k)})
    wpf_total = sum(wpf_counts.values())
    log(f"  WPF time: {wpf_total:.1f}ms ({100*wpf_total/total:.1f}% of total)" if total else "  no time captured")

    ranked = []
    for method, samples in wpf_counts.most_common(args.top_k):
        pct = 100 * samples / total if total > 0 else 0
        ranked.append((method, samples, pct))

    benchmarks = existing_benchmarks()
    log(f"  existing benchmarks cover {len(benchmarks)} method(s): "
        f"{list(benchmarks.values())[:5]}")

    # Ensure any benchmarked Type.Method appears in the output even if it
    # wasn't a leaf in this trace — inner Claude needs at least one testable
    # entry to make progress, and the bench tells us this method matters
    # somewhere even if not in the play-take scenario.
    covered_in_ranked = {
        m for m, _, _ in ranked if find_bench_filter(m, benchmarks) is not None
    }
    for type_method in benchmarks:
        if any(f".{type_method}(" in m for m in covered_in_ranked):
            continue
        # Synthesize a method label from the Type.Method key. cpu_pct_total
        # left as 0.0 — bench is included for testability, not impact.
        # Append `()` so find_bench_filter's `Type.Method(` substring match
        # treats the synthetic entry as covered.
        synthetic = f"(benchmarked) {type_method}()"
        ranked.append((synthetic, 0.0, 0.0))

    write_profile_json(
        ranked, benchmarks,
        source=f"profile.py from {nettrace.name} ({nettrace.stat().st_size//1024}KB)",
    )

    # Print summary table
    log("Top hot paths:")
    log(f"  {'time_ms':>9s}  {'cpu%':>6s}  {'bench':>5s}  method")
    for method, samples, pct in ranked[:15]:
        bench = "✓" if method in benchmarks else "—"
        log(f"  {samples:>9.1f}  {pct:>6.2f}  {bench:>5s}  {method}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
