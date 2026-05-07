#!/usr/bin/env python3
"""Tier A scenario profile capture.

Three modes:
  --run        Drive spike-9 with EventPipe sampling, then analyze its trace.
  --trace      Skip spike; analyze an existing .nettrace file.
  --run-multi  Run all three scenario scripts (startup, take-open, playback),
               aggregate the union of top-10 per scenario into profile.json.

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
  python3 profile.py --run                              # full pipeline (single scenario)
  python3 profile.py --trace path/to/spike.nettrace     # analyze existing
  python3 profile.py --run --top-k 50                   # broader ranking
  python3 profile.py --run-multi                        # union of 3 scenarios
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

# Scenario scripts for --run-multi mode.
SCENARIO_SCRIPTS: dict[str, Path] = {
    "startup":   WPF_REPO / "tools" / "poc" / "scenario-startup.py",
    "take-open": WPF_REPO / "tools" / "poc" / "scenario-take-open.py",
    "playback":  WPF_REPO / "tools" / "poc" / "scenario-playback.py",
}

DEFAULT_TOP_K = 30
# Number of top methods to take from each scenario for the union.
MULTI_PER_SCENARIO_K = 10
# Maximum total entries in the unified profile.json.
MULTI_MAX_ENTRIES = 20

ALLOC_PARSER_EXE = (
    WPF_REPO / "tools" / "alloc-parser" / "bin" / "Release"
    / "net10.0-windows" / "win-x64" / "publish" / "AllocParser.exe"
)

SPIKE_TIMEOUT_S = int(os.environ.get("WPF_AR_PROFILE_SPIKE_TIMEOUT", "300"))
SCENARIO_TIMEOUT_S = int(os.environ.get("WPF_AR_SCENARIO_TIMEOUT", "600"))
CONVERT_TIMEOUT_S = 120
ALLOC_TIMEOUT_S = 180

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


# ─── Scenario runner (--run-multi) ───────────────────────────────────────────


def run_scenario(slug: str, script_path: Path) -> Path:
    """Run an arbitrary scenario script and return the .nettrace it produced.

    Sets SCENARIO_RESULT_DIR to PROFILE_OUTPUT_DIR/<slug>/ and SCENARIO_NAME
    to <slug>.  The scenario script must produce exactly one .nettrace in that
    directory.  Returns path to the newest .nettrace after the script exits.

    Raises RuntimeError on non-zero exit or missing output.  profile.py does
    NOT taskkill MC — each scenario script owns its own MC PID lifecycle.
    """
    result_dir = PROFILE_OUTPUT_DIR / slug
    result_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SCENARIO_RESULT_DIR"] = str(result_dir)
    env["SCENARIO_NAME"] = slug
    env["MC_PERF_MODE"] = "1"
    log(f"running scenario '{slug}' → {result_dir}")
    p = subprocess.run(
        ["python3", str(script_path)],
        cwd=str(WPF_REPO),
        env=env,
        timeout=SCENARIO_TIMEOUT_S,
    )
    if p.returncode != 0:
        raise RuntimeError(f"scenario '{slug}' failed (rc={p.returncode})")
    candidates = sorted(
        result_dir.glob("*.nettrace"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"no .nettrace produced in {result_dir}")
    log(f"  captured {candidates[0].name} ({candidates[0].stat().st_size // 1024} KB)")
    return candidates[0]


# ─── AllocParser integration ──────────────────────────────────────────────────


def build_alloc_parser_cmd(nettrace: Path, output_json: Path) -> list[str]:
    """Return argv for invoking AllocParser.exe via cmd.exe /c."""
    return [
        "cmd.exe", "/c",
        to_winpath(ALLOC_PARSER_EXE),
        to_winpath(nettrace),
        "--output", to_winpath(output_json),
    ]


def aggregate_alloc(alloc_json: Path) -> Counter:
    """Read AllocParser JSON output; return Counter[frame_name → alloc_bytes].

    Returns an empty Counter if the file is missing, empty, or unparseable
    (graceful degradation — alloc columns remain zero rather than crashing).
    The JSON schema is: [{"frame": "...", "alloc_bytes": N}, ...]
    """
    if not alloc_json.exists():
        return Counter()
    try:
        raw = alloc_json.read_text(encoding="utf-8").strip()
        if not raw:
            return Counter()
        entries = json.loads(raw)
        return Counter({e["frame"]: e["alloc_bytes"] for e in entries if "frame" in e and "alloc_bytes" in e})
    except Exception as exc:  # noqa: BLE001
        log(f"  WARNING: could not parse alloc JSON ({alloc_json}): {exc}")
        return Counter()


def run_alloc_parser(nettrace: Path, slug: str) -> Counter:
    """Run AllocParser.exe on *nettrace*; return alloc counter.

    If the binary is absent, logs a warning and returns an empty Counter.
    If the binary is present but fails, logs the error and returns empty Counter.
    """
    if not ALLOC_PARSER_EXE.exists():
        log(f"  WARNING: AllocParser binary absent ({ALLOC_PARSER_EXE}); alloc columns will be 0")
        return Counter()

    alloc_json = nettrace.with_suffix(".alloc.json")
    # Cache: skip if JSON is newer than nettrace.
    if alloc_json.exists() and alloc_json.stat().st_mtime > nettrace.stat().st_mtime:
        log(f"  alloc cached: {alloc_json.name}")
        return aggregate_alloc(alloc_json)

    log(f"  running AllocParser on {nettrace.name} …")
    rc, output = cmd(
        build_alloc_parser_cmd(nettrace, alloc_json),
        timeout=ALLOC_TIMEOUT_S,
    )
    if rc != 0:
        log(f"  WARNING: AllocParser failed (rc={rc}); alloc columns will be 0\n{output[:400]}")
        return Counter()

    log(f"  {output.strip().splitlines()[-1] if output.strip() else 'AllocParser ok'}")
    return aggregate_alloc(alloc_json)


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


def aggregate_multi_scenario(
    scenario_traces: dict[str, Path],
) -> tuple[list[tuple[str, float, float, list[str], dict[str, float]]], Counter]:
    """Union-of-top-K aggregation across multiple scenario traces.

    For each scenario:
      1. Convert to speedscope + aggregate → (counter, total_ms).
      2. Filter to WPF methods.
      3. Take top MULTI_PER_SCENARIO_K methods.
      4. Run AllocParser on the same .nettrace → alloc counter.
    Build the union set (dedup by exact method string), then for every
    surviving method record per-scenario inclusive ms and pct.
    Rank by max(cpu_pct_total across scenarios), truncate to MULTI_MAX_ENTRIES.

    Returns (entries, total_alloc) where:
      entries — list of (method, samples_sum_ms, cpu_pct_max, scenarios_list,
                          scenario_cpu_pct_dict)
      total_alloc — Counter[frame_name → alloc_bytes] summed across all scenarios
    """
    # Per-scenario data: {scenario → (wpf_counter, total_ms)}
    per_scenario: dict[str, tuple[Counter, float]] = {}
    # Alloc counters per scenario; summed below into total_alloc.
    total_alloc: Counter = Counter()
    _warned_alloc_absent = False

    for slug, nettrace in scenario_traces.items():
        log(f"  aggregating scenario '{slug}' …")
        speedscope = convert_to_speedscope(nettrace)
        counts, total_ms = aggregate_speedscope(speedscope)
        wpf_counts: Counter = Counter(
            {k: v for k, v in counts.items() if is_wpf_method(k)}
        )
        per_scenario[slug] = (wpf_counts, total_ms)
        log(f"    {slug}: total={total_ms:.0f}ms WPF_frames={len(wpf_counts)}")

        # AllocParser: run once per scenario, accumulate into total_alloc.
        alloc = run_alloc_parser(nettrace, slug)
        if not alloc and not _warned_alloc_absent:
            _warned_alloc_absent = True  # warning already emitted by run_alloc_parser
        total_alloc += alloc
        log(f"    {slug}: alloc frames={len(alloc)} "
            f"total_alloc_bytes={sum(alloc.values()):,}")

    # Build the union of top-K per scenario.
    union_methods: set[str] = set()
    for slug, (wpf_counts, _total_ms) in per_scenario.items():
        for method, _ms in wpf_counts.most_common(MULTI_PER_SCENARIO_K):
            union_methods.add(method)
    log(f"  union has {len(union_methods)} methods before dedup "
        f"({len(scenario_traces)} scenarios × top-{MULTI_PER_SCENARIO_K})")

    slugs = list(scenario_traces.keys())

    # For each method in the union, compute per-scenario ms and pct.
    entries: list[tuple[str, float, float, list[str], dict[str, float]]] = []
    for method in union_methods:
        scenario_cpu_pct: dict[str, float] = {}
        appearing_in: list[str] = []
        samples_sum = 0.0
        for slug in slugs:
            wpf_counts, total_ms = per_scenario[slug]
            ms = wpf_counts.get(method, 0.0)
            pct = 100.0 * ms / total_ms if total_ms > 0 else 0.0
            scenario_cpu_pct[slug] = round(pct, 2)
            samples_sum += ms
            if ms > 0:
                appearing_in.append(slug)
        cpu_pct_max = max(scenario_cpu_pct.values()) if scenario_cpu_pct else 0.0
        entries.append((method, round(samples_sum, 2), round(cpu_pct_max, 2),
                        appearing_in, scenario_cpu_pct))

    # Rank by max cpu% descending; truncate to MULTI_MAX_ENTRIES.
    entries.sort(key=lambda e: e[2], reverse=True)
    if len(entries) > MULTI_MAX_ENTRIES:
        log(f"  truncating from {len(entries)} to top-{MULTI_MAX_ENTRIES}")
        entries = entries[:MULTI_MAX_ENTRIES]
    return entries, total_alloc


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


def write_profile_json(
    ranked: list[tuple],
    benchmarks: dict[str, str],
    source: object,
    *,
    multi: bool = False,
    scenario_name: str | None = None,
    alloc_counter: Counter | None = None,
) -> None:
    """Write profile.json from ranked entries.

    `ranked` entries are either:
      - (method, samples_ms, pct)              — single-scenario (--run / --trace)
      - (method, samples_ms, pct_max,
         scenarios_list, scenario_cpu_pct_dict) — multi-scenario (--run-multi)

    Both shapes emit the same hot_paths schema (schema_version=1) so consumers
    always see the same fields.  The new fields `scenarios`, `scenario_cpu_pct`,
    `alloc_bytes`, and `alloc_pct_total` are populated from `alloc_counter` when
    provided; zero for missing frames or when the AllocParser binary is absent.
    """
    resolved_alloc: Counter = alloc_counter if alloc_counter is not None else Counter()
    # AllocParser emits frames without a "Module!" prefix (e.g. "System.Windows.Foo.Bar()"),
    # but speedscope CPU entries have the form "Module!System.Windows.Foo.Bar()".
    # Build a secondary lookup keyed on the post-"!" portion so we can match both forms.
    _alloc_no_module: dict[str, int] = {}
    for frame, ab in resolved_alloc.items():
        key = frame.split("!", 1)[1] if "!" in frame else frame
        _alloc_no_module[key] = _alloc_no_module.get(key, 0) + ab
    total_alloc_bytes = sum(resolved_alloc.values())

    if multi:
        notes = [
            "Tier A profile: union of top-10 WPF methods per scenario from",
            "{startup, take-open, playback}, deduped to ≤20 entries.",
            "cpu_pct_total = max across scenarios; scenario_cpu_pct shows breakdown.",
            "alloc_bytes uses AllocationTick events (sampled ~100 KB; absent frames may be undersampled).",
            "AllocationTick noise floor ≈ 100 KB per stack path (CLR sampling interval).",
            "Entries with bdn_filter:null have no covering microbenchmark.",
        ]
    else:
        notes = [
            "Tier A profile: top-K WPF methods by CPU sample-count from a",
            f"single scenario ({scenario_name or 'spike-9'}) run.",
            "alloc_bytes uses AllocationTick events (sampled ~100 KB; absent frames may be undersampled).",
            "Entries with bdn_filter:null have no covering microbenchmark.",
            "The orchestrator's benchmark-writer pass scaffolds new ones.",
        ]

    hot_paths = []
    for entry in ranked:
        if multi:
            method, samples, pct_max, scenarios_list, scenario_cpu_pct = entry
        else:
            method, samples, pct = entry
            pct_max = pct
            scenarios_list = [scenario_name] if scenario_name else []
            scenario_cpu_pct = {scenario_name: round(pct, 2)} if scenario_name else {}

        # Match alloc by bare name (strip "Module!" prefix if present).
        method_bare = method.split("!", 1)[1] if "!" in method else method
        ab = resolved_alloc.get(method, 0) or _alloc_no_module.get(method_bare, 0)
        apct = 100.0 * ab / total_alloc_bytes if total_alloc_bytes > 0 else 0.0

        bf = find_bench_filter(method, benchmarks)
        hot_paths.append({
            "method": method,
            "samples": round(samples, 2),
            "cpu_pct_total": round(pct_max, 2),
            "bdn_filter": bf,
            "needs_benchmark": bf is None,
            "scenarios": scenarios_list,
            "scenario_cpu_pct": scenario_cpu_pct,
            "alloc_bytes": ab,
            "alloc_pct_total": round(apct, 4),
        })

    PROFILE_JSON.write_text(json.dumps({
        "schema_version": 1,
        "phase": 1,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "notes": notes,
        "hot_paths": hot_paths,
    }, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {PROFILE_JSON} ({len(ranked)} entries)")


# ─── Main ─────────────────────────────────────────────────────────────────────


def _inject_synthetics(
    ranked: list[tuple],
    benchmarks: dict[str, str],
    *,
    multi: bool,
) -> list[tuple]:
    """Inject synthetic (benchmarked) entries for any bench not yet in ranked.

    Synthetic entries have cpu_pct_total=0.0 and zeros for all numeric fields.
    They are appended (not ranked) so inner Claude always has a testable entry.
    The extra fields (scenarios, scenario_cpu_pct, alloc_*) are zero / empty
    for synthetics regardless of mode.
    """
    covered_in_ranked = {
        m for entry in ranked
        for m in [entry[0]]
        if find_bench_filter(m, benchmarks) is not None
    }
    for type_method in benchmarks:
        if any(f".{type_method}(" in m for m in covered_in_ranked):
            continue
        synthetic = f"(benchmarked) {type_method}()"
        if multi:
            ranked.append((synthetic, 0.0, 0.0, [], {}))
        else:
            ranked.append((synthetic, 0.0, 0.0))
    return ranked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", action="store_true",
                   help="Run spike-9 first, then analyze its trace.")
    g.add_argument("--trace", type=Path,
                   help="Path to existing .nettrace; skip spike.")
    g.add_argument("--run-multi", action="store_true",
                   help="Run all 3 scenario scripts and aggregate results.")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                    help=f"Number of hot paths to retain per single-scenario run "
                         f"(default {DEFAULT_TOP_K}; --run-multi uses {MULTI_PER_SCENARIO_K}/scenario)")
    args = ap.parse_args()

    benchmarks = existing_benchmarks()
    log(f"  existing benchmarks cover {len(benchmarks)} method(s): "
        f"{list(benchmarks.values())[:5]}")

    # ── --run-multi path ──────────────────────────────────────────────────────
    if args.run_multi:
        log("=== --run-multi: running all 3 scenario scripts ===")
        scenario_traces: dict[str, Path] = {}
        for slug, script in SCENARIO_SCRIPTS.items():
            if not script.exists():
                log(f"FATAL: scenario script not found: {script}")
                return 1
            log(f"--- scenario: {slug} ---")
            trace = run_scenario(slug, script)
            scenario_traces[slug] = trace

        log("=== aggregating multi-scenario results ===")
        ranked, total_alloc = aggregate_multi_scenario(scenario_traces)
        ranked = _inject_synthetics(ranked, benchmarks, multi=True)
        # Final cap: total entries (real + synthetic) must not exceed MULTI_MAX_ENTRIES.
        # Synthetics are appended last, so they are trimmed first if necessary.
        if len(ranked) > MULTI_MAX_ENTRIES:
            log(f"  capping total (incl. synthetics) from {len(ranked)} to {MULTI_MAX_ENTRIES}")
            ranked = ranked[:MULTI_MAX_ENTRIES]

        source = [
            {
                "scenario": slug,
                "trace": trace.name,
                "size_mb": round(trace.stat().st_size / 1024 / 1024, 1),
            }
            for slug, trace in scenario_traces.items()
        ]
        write_profile_json(
            ranked, benchmarks, source=source,
            multi=True, alloc_counter=total_alloc,
        )

        log("Top hot paths (multi-scenario):")
        log(f"  {'samples_ms':>10s}  {'cpu%_max':>8s}  {'alloc%':>7s}  {'scenarios':<22s}  method")
        for entry in ranked[:15]:
            method, samples, pct_max, scenarios_list, _scpct = entry
            # Retrieve alloc_pct_total from the written JSON for display consistency.
            total_alloc_bytes = sum(total_alloc.values())
            method_bare = method.split("!", 1)[1] if "!" in method else method
            ab = total_alloc.get(method, 0) or total_alloc.get(method_bare, 0)
            apct = 100.0 * ab / total_alloc_bytes if total_alloc_bytes > 0 else 0.0
            log(f"  {samples:>10.1f}  {pct_max:>8.2f}  {apct:>6.2f}%  "
                f"{','.join(scenarios_list):<22s}  {method}")

        # Alloc top-5 summary.
        total_alloc_bytes = sum(total_alloc.values())
        if total_alloc_bytes > 0:
            from_profile = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
            hp = from_profile["hot_paths"]
            top_alloc = sorted(hp, key=lambda x: -x["alloc_pct_total"])[:5]
            log("Top 5 by alloc_pct_total:")
            log(f"  {'alloc%':>7s}  {'alloc_bytes':>12s}  method")
            for e in top_alloc:
                log(f"  {e['alloc_pct_total']:>6.2f}%  {e['alloc_bytes']:>12,}  {e['method'][:80]}")
        else:
            log("  alloc data unavailable (AllocParser absent or no WPF AllocationTick events)")
        return 0

    # ── --run / --trace path (single-scenario, backward compat) ──────────────
    if args.run:
        nettrace = run_spike()
        scenario_name = "spike-9"
    else:
        nettrace = args.trace.resolve()
        if not nettrace.exists():
            log(f"FATAL: trace not found: {nettrace}")
            return 1
        scenario_name = nettrace.stem

    speedscope = convert_to_speedscope(nettrace)
    counts, total = aggregate_speedscope(speedscope)
    log(f"  total time: {total:.1f}ms; unique frames: {len(counts)}")

    wpf_counts = Counter({k: v for k, v in counts.items() if is_wpf_method(k)})
    wpf_total = sum(wpf_counts.values())
    log(f"  WPF time: {wpf_total:.1f}ms ({100*wpf_total/total:.1f}% of total)" if total else "  no time captured")

    # AllocParser: run on the same nettrace if binary is available.
    single_alloc = run_alloc_parser(nettrace, scenario_name)
    log(f"  alloc frames={len(single_alloc)} total_alloc_bytes={sum(single_alloc.values()):,}")

    ranked_single = []
    for method, samples in wpf_counts.most_common(args.top_k):
        pct = 100 * samples / total if total > 0 else 0
        ranked_single.append((method, samples, pct))

    # Ensure any benchmarked Type.Method appears in the output even if it
    # wasn't a leaf in this trace — inner Claude needs at least one testable
    # entry to make progress, and the bench tells us this method matters
    # somewhere even if not in the play-take scenario.
    ranked_single = _inject_synthetics(ranked_single, benchmarks, multi=False)

    write_profile_json(
        ranked_single, benchmarks,
        source=f"profile.py from {nettrace.name} ({nettrace.stat().st_size//1024}KB)",
        multi=False,
        scenario_name=scenario_name,
        alloc_counter=single_alloc,
    )

    # Print summary table
    log("Top hot paths:")
    log(f"  {'time_ms':>9s}  {'cpu%':>6s}  {'alloc%':>7s}  {'bench':>5s}  method")
    total_alloc_bytes_single = sum(single_alloc.values())
    for method, samples, pct in ranked_single[:15]:
        bench = "✓" if method in benchmarks else "—"
        method_bare = method.split("!", 1)[1] if "!" in method else method
        ab = single_alloc.get(method, 0) or single_alloc.get(method_bare, 0)
        apct = 100.0 * ab / total_alloc_bytes_single if total_alloc_bytes_single > 0 else 0.0
        log(f"  {samples:>9.1f}  {pct:>6.2f}  {apct:>6.2f}%  {bench:>5s}  {method}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
