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
    """`dotnet-trace convert --format speedscope` → JSON path."""
    out = nettrace.with_suffix(".speedscope.json")
    if out.exists() and out.stat().st_mtime > nettrace.stat().st_mtime:
        log(f"  speedscope cached: {out.name}")
        return out
    trace_exe = find_dotnet_trace()
    log(f"  converting {nettrace.name} → speedscope …")
    rc, output = cmd(
        ["cmd.exe", "/c", trace_exe, "convert",
         to_winpath(nettrace),
         "--format", "speedscope",
         "--output", to_winpath(out)],
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


def aggregate_speedscope(speedscope_path: Path) -> tuple[Counter, int]:
    """Return (Counter[frame_name] → leaf_sample_count, total_samples)."""
    data = json.loads(speedscope_path.read_text(encoding="utf-8"))

    # Speedscope schema: shared.frames[i].name; profiles[].samples[][] is a
    # list of stacks (each stack is a list of frame indices, root → leaf);
    # weights[] aligns with samples[] (default 1 if absent).
    shared = data.get("shared", {})
    frames = shared.get("frames", [])
    profiles = data.get("profiles", [])

    counts: Counter = Counter()
    total = 0
    for prof in profiles:
        samples = prof.get("samples", [])
        weights = prof.get("weights", [1] * len(samples))
        for stack, weight in zip(samples, weights):
            if not stack:
                continue
            # Innermost frame = leaf = where time was spent.
            leaf_idx = stack[-1]
            if leaf_idx >= len(frames):
                continue
            name = frames[leaf_idx].get("name", "")
            counts[name] += weight
            total += weight
    return counts, total


# ─── Benchmark mapping ────────────────────────────────────────────────────────


def existing_benchmarks() -> dict[str, str]:
    """Scan microbench/Benchmarks/*.cs for `[Benchmark(...)]` attributes and
    map known method patterns → bdn_filter glob.

    Heuristic: a benchmark file whose name matches `<Type>Benchmark.cs` and
    contains a comment block referencing a hot-path method is treated as
    covering that method. Inner Claude can refer to the bench filter directly;
    the orchestrator's benchmark-writer pass adds new entries as needed.
    """
    out: dict[str, str] = {}
    if not MICROBENCH_DIR.exists():
        return out
    for f in MICROBENCH_DIR.glob("*.cs"):
        text = f.read_text(encoding="utf-8")
        # Look for "Geometry.Parse" / "LayoutManager.UpdateLayout" comments.
        for m in re.finditer(r"([A-Z][\w.]+\.[A-Z]\w+)\s*\(", text):
            method = m.group(1)
            if is_wpf_method(method) and method not in out:
                out[method] = f"*{f.stem.replace('Benchmark', '')}*"
    return out


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
                "samples": int(samples),
                "cpu_pct_total": round(pct, 2),
                "bdn_filter": benchmarks.get(method),
                "needs_benchmark": method not in benchmarks,
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
    log(f"  total samples: {total}; unique frames: {len(counts)}")

    wpf_counts = Counter({k: v for k, v in counts.items() if is_wpf_method(k)})
    wpf_total = sum(wpf_counts.values())
    log(f"  WPF samples: {wpf_total} ({100*wpf_total/total:.1f}% of total)")

    ranked = []
    for method, samples in wpf_counts.most_common(args.top_k):
        pct = 100 * samples / total if total > 0 else 0
        ranked.append((method, samples, pct))

    benchmarks = existing_benchmarks()
    log(f"  existing benchmarks cover {len(benchmarks)} method(s): "
        f"{list(benchmarks.values())[:5]}")

    write_profile_json(
        ranked, benchmarks,
        source=f"profile.py from {nettrace.name} ({nettrace.stat().st_size//1024}KB)",
    )

    # Print summary table
    log("Top hot paths:")
    log(f"  {'samples':>7s}  {'cpu%':>6s}  {'bench':>5s}  method")
    for method, samples, pct in ranked[:15]:
        bench = "✓" if method in benchmarks else "—"
        log(f"  {samples:>7d}  {pct:>6.2f}  {bench:>5s}  {method}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
