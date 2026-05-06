#!/usr/bin/env python3
"""Tier B microbenchmark eval — same-session A/B with statistical decision.

Per autoresearch iteration:
  1. Build PresentationCore.dll for HEAD~1 (baseline)  → save as baseline_pc
  2. Build PresentationCore.dll for HEAD   (candidate) → save as candidate_pc
  3. Publish microbench project once (consumer of the swapped DLL)
  4. Swap baseline_pc into publish dir → run BDN with --filter → save JSON
  5. Swap candidate_pc into publish dir → run BDN with --filter → save JSON
  6. Apply decision rule:
     - non-overlapping confidence intervals → significant
     - direction = sign(candidate.mean − baseline.mean)
     - absolute floors guard against trivial wins (alloc < 64 B/op,
       time < 5 ns/op)
     - KEEP requires SIGNIFICANT win on alloc OR time, AND
       no SIGNIFICANT regression on the other axis
     - else REJECT-UNCLEAR (conservative default per Oystein's spec)

Exit codes:
  0  KEEP            — commit stays
  1  REJECT          — eval calls `git revert --no-edit HEAD`
  2  REJECT-UNCLEAR  — eval calls `git revert --no-edit HEAD` (sub-noise)
  3  BUILD-FAIL      — could not build either side; reverted
  4  BENCH-FAIL      — BDN crashed / no JSON output; reverted

Usage:
  python3 microbench.py --filter '*LayoutManager*' --bench-name 'layout-update'
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.resolve()
WPF_REPO = Path("/c/work/wpf-perf")
PC_BUILD_OUT = WPF_REPO / "artifacts" / "bin" / "PresentationCore" / "x64" / "Release" / "net10.0"
BUILD_PC_SCRIPT = WPF_REPO / "build-pc-perf.ps1"

MICROBENCH_PROJ = WPF_REPO / "microbench"
MICROBENCH_PUBLISH = MICROBENCH_PROJ / "bin" / "Release" / "net10.0-windows" / "win-x64" / "publish"
MICROBENCH_EXE = MICROBENCH_PUBLISH / "Microbenchmarks.exe"
MICROBENCH_RESULTS = MICROBENCH_PUBLISH / "BenchmarkDotNet.Artifacts" / "results"

STAGING = ROOT / "microbench-staging"
RESULTS_TSV = ROOT / "results.tsv"
RESULTS_JSONL = ROOT / "results.jsonl"

BUILD_TIMEOUT_S = int(os.environ.get("WPF_AR_BUILD_TIMEOUT", "600"))
BENCH_TIMEOUT_S = int(os.environ.get("WPF_AR_BENCH_TIMEOUT", "300"))

# Absolute floors to avoid trivial wins. Alloc: 64 bytes/op = 1 reference + 1
# header. Time: 5 ns/op ≈ 16 cycles on a 3.4 GHz CPU. Below these, even a
# statistically significant change isn't worth keeping.
MIN_ALLOC_BYTES_PER_OP = float(os.environ.get("WPF_AR_MIN_ALLOC_BYTES", "64"))
MIN_TIME_NS_PER_OP = float(os.environ.get("WPF_AR_MIN_TIME_NS", "5"))

# Inner-loop commits may ONLY touch product code under these prefixes (relative
# to WPF_REPO). Prevents the agent from editing benchmarks, baselines, harness
# scripts, or profile manifests — the consensus models flagged this as the
# single biggest stop-the-line risk ("Goodhart's Law on benchmark code").
ALLOWED_PATH_PREFIXES = (
    "src/Microsoft.DotNet.Wpf/src/PresentationCore/",
    "src/Microsoft.DotNet.Wpf/src/PresentationFramework/",
    "src/Microsoft.DotNet.Wpf/src/WindowsBase/",
    "src/Microsoft.DotNet.Wpf/src/System.Xaml/",
    "src/Microsoft.DotNet.Wpf/src/Shared/",
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[microbench] {msg}", flush=True)


def cmd(argv: list[str], cwd: Path | None = None, timeout: int | None = None,
        env: dict | None = None) -> tuple[int, str]:
    p = subprocess.run(
        argv, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def to_winpath(p: Path) -> str:
    out = subprocess.run(["cygpath", "-w", str(p)], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def git(*args: str, cwd: Path = WPF_REPO) -> tuple[int, str]:
    return cmd(["git", *args], cwd=cwd)


def git_sha(ref: str = "HEAD", cwd: Path = WPF_REPO) -> str:
    rc, out = git("rev-parse", ref, cwd=cwd)
    if rc != 0:
        raise RuntimeError(f"git rev-parse {ref} failed: {out}")
    return out.strip()


def working_tree_clean() -> bool:
    rc, out = git("status", "--porcelain")
    return rc == 0 and out.strip() == ""


def files_touched_by(sha: str) -> list[str]:
    rc, out = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    if rc != 0:
        raise RuntimeError(f"git diff-tree {sha} failed: {out}")
    return [line for line in out.splitlines() if line.strip()]


def check_path_allowlist(sha: str) -> tuple[bool, list[str]]:
    """Return (ok, violations). Inner-loop commits may only touch allowed paths."""
    files = files_touched_by(sha)
    violations = [f for f in files if not any(f.startswith(p) for p in ALLOWED_PATH_PREFIXES)]
    return len(violations) == 0, violations


def build_pc_release() -> bool:
    """Build PresentationCore (Release, x64) into PC_BUILD_OUT. Returns True on success."""
    log(f"  building PresentationCore (Release, x64) at {git_sha()[:8]} …")
    if PC_BUILD_OUT.exists():
        for f in PC_BUILD_OUT.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except Exception:
                    pass
    rc, out = cmd(
        ["cmd.exe", "/c", "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", to_winpath(BUILD_PC_SCRIPT)],
        cwd=WPF_REPO, timeout=BUILD_TIMEOUT_S,
    )
    if rc != 0:
        log(f"  build failed (rc={rc}); tail:")
        for line in out.splitlines()[-15:]:
            log(f"    {line}")
        return False
    if not (PC_BUILD_OUT / "PresentationCore.dll").exists():
        log(f"  build succeeded but PresentationCore.dll missing at {PC_BUILD_OUT}")
        return False
    return True


def stage_pc_dll(side: str) -> Path | None:
    """Copy the just-built PresentationCore.dll into a side-specific staging path."""
    src = PC_BUILD_OUT / "PresentationCore.dll"
    if not src.exists():
        return None
    STAGING.mkdir(exist_ok=True, parents=True)
    dst = STAGING / f"PresentationCore.{side}.dll"
    shutil.copy2(src, dst)
    return dst


def publish_microbench() -> bool:
    log("  publishing microbench project (Release, win-x64, self-contained) …")
    rc, out = cmd(
        ["cmd.exe", "/c", "dotnet", "publish",
         to_winpath(MICROBENCH_PROJ / "Microbenchmarks.csproj"),
         "-c", "Release", "-r", "win-x64", "--self-contained"],
        cwd=MICROBENCH_PROJ, timeout=BUILD_TIMEOUT_S,
    )
    if rc != 0:
        log(f"  publish failed (rc={rc}); tail:")
        for line in out.splitlines()[-15:]:
            log(f"    {line}")
        return False
    return MICROBENCH_EXE.exists()


def swap_pc_into_publish(staged_dll: Path) -> bool:
    target = MICROBENCH_PUBLISH / "PresentationCore.dll"
    if not target.exists():
        log(f"  target {target} missing — publish did not produce expected layout")
        return False
    shutil.copy2(staged_dll, target)
    return True


def clear_bdn_artifacts() -> None:
    if MICROBENCH_RESULTS.exists():
        shutil.rmtree(MICROBENCH_RESULTS, ignore_errors=True)


def run_bdn(filter_pattern: str) -> tuple[int, str]:
    log(f"  running BDN with --filter '{filter_pattern}' …")
    return cmd(
        ["cmd.exe", "/c", to_winpath(MICROBENCH_EXE),
         "--filter", filter_pattern,
         "--exporters", "json"],
        cwd=MICROBENCH_PUBLISH, timeout=BENCH_TIMEOUT_S,
    )


def parse_bdn_results() -> list[dict] | None:
    """Read every *-report-full.json in results dir; return the Benchmarks list flat."""
    if not MICROBENCH_RESULTS.exists():
        return None
    out: list[dict] = []
    for f in sorted(MICROBENCH_RESULTS.glob("*-report-full.json")):
        try:
            data = json.loads(f.read_text())
            for b in data.get("Benchmarks", []):
                out.append({
                    "fullName": b["FullName"],
                    "stats": b.get("Statistics", {}),
                    "memory": b.get("Memory", {}),
                })
        except Exception as e:
            log(f"  failed to parse {f.name}: {e}")
    return out or None


# ─── Decision rule ────────────────────────────────────────────────────────────


def ci_for_mean(stats: dict) -> tuple[float, float]:
    """Return (lower, upper) confidence interval for the mean."""
    ci = stats.get("ConfidenceInterval", {})
    if "Lower" in ci and "Upper" in ci:
        return float(ci["Lower"]), float(ci["Upper"])
    # Fallback: approximate via 1.96 * StandardError
    mean = float(stats.get("Mean", 0.0))
    se = float(stats.get("StandardError", 0.0))
    return mean - 1.96 * se, mean + 1.96 * se


def cis_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return not (a[1] < b[0] or b[1] < a[0])


def decide(baseline: dict, candidate: dict) -> tuple[str, str]:
    """Compare baseline vs candidate stats; return (verdict, reason)."""

    b_time = baseline["stats"]
    c_time = candidate["stats"]
    b_alloc_per_op = float(baseline.get("memory", {}).get("BytesAllocatedPerOperation", 0))
    c_alloc_per_op = float(candidate.get("memory", {}).get("BytesAllocatedPerOperation", 0))

    b_time_mean = float(b_time.get("Mean", 0.0))
    c_time_mean = float(c_time.get("Mean", 0.0))
    b_time_ci = ci_for_mean(b_time)
    c_time_ci = ci_for_mean(c_time)

    time_delta = c_time_mean - b_time_mean
    alloc_delta = c_alloc_per_op - b_alloc_per_op

    time_significant = not cis_overlap(b_time_ci, c_time_ci)
    # Alloc is deterministic; any non-zero delta is significant
    alloc_significant = alloc_delta != 0.0

    time_meaningful = abs(time_delta) >= MIN_TIME_NS_PER_OP
    alloc_meaningful = abs(alloc_delta) >= MIN_ALLOC_BYTES_PER_OP

    time_won = time_significant and time_meaningful and time_delta < 0
    time_lost = time_significant and time_meaningful and time_delta > 0
    alloc_won = alloc_significant and alloc_meaningful and alloc_delta < 0
    alloc_lost = alloc_significant and alloc_meaningful and alloc_delta > 0

    # Hard rejects: any meaningful regression
    if alloc_lost:
        return "REJECT", f"alloc regressed: {b_alloc_per_op:.0f} → {c_alloc_per_op:.0f} B/op (Δ {alloc_delta:+.0f})"
    if time_lost:
        return "REJECT", f"time regressed: {b_time_mean:.2f} → {c_time_mean:.2f} ns (Δ {time_delta:+.2f}, CIs disjoint)"

    # Accepts: significant + meaningful win on either axis, no regression on other
    if alloc_won:
        return "KEEP", f"alloc win: {b_alloc_per_op:.0f} → {c_alloc_per_op:.0f} B/op (Δ {alloc_delta:+.0f}); time Δ {time_delta:+.2f} ns ({'sig' if time_significant else 'noise'})"
    if time_won:
        return "KEEP", f"time win: {b_time_mean:.2f} → {c_time_mean:.2f} ns (Δ {time_delta:+.2f}, CIs disjoint); alloc Δ {alloc_delta:+.0f} B/op"

    return "REJECT-UNCLEAR", f"no significant signal: time Δ {time_delta:+.2f} ns ({'sig' if time_significant else 'noise'}, {'meaningful' if time_meaningful else 'sub-floor'}); alloc Δ {alloc_delta:+.0f} B/op"


# ─── Main flow ────────────────────────────────────────────────────────────────


def revert_head() -> None:
    log("reverting HEAD via git revert --no-edit …")
    rc, out = git("revert", "--no-edit", "HEAD")
    if rc != 0:
        log(f"git revert failed: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter", required=True, help="BDN --filter pattern (e.g., '*Layout*')")
    parser.add_argument("--bench-name", default="microbench", help="Short name for results.tsv")
    parser.add_argument("--no-revert", action="store_true",
                        help="Do not git-revert on REJECT (for debugging)")
    args = parser.parse_args()

    if not working_tree_clean():
        log("FATAL: working tree not clean. Commit your change before running microbench.")
        return 5

    head_sha = git_sha("HEAD")
    base_sha = git_sha("HEAD~1")
    log(f"baseline={base_sha[:8]}  candidate={head_sha[:8]}  filter='{args.filter}'")

    # ── Path allowlist gate ────────────────────────────────────────────────
    ok, violations = check_path_allowlist(head_sha)
    if not ok:
        log("FATAL: HEAD commit touches forbidden paths. Allowed prefixes:")
        for p in ALLOWED_PATH_PREFIXES:
            log(f"  {p}")
        log("Violating files:")
        for v in violations:
            log(f"  {v}")
        log("Inner loop may only edit WPF product source. Reverting.")
        if not args.no_revert:
            revert_head()
        return 6
    STAGING.mkdir(exist_ok=True, parents=True)

    # ── Build both sides ────────────────────────────────────────────────────
    log("Phase 1: build baseline PresentationCore.dll")
    rc, _ = git("checkout", "--quiet", base_sha)
    if rc != 0:
        log("FATAL: could not checkout HEAD~1")
        return 3
    try:
        if not build_pc_release():
            log("FATAL: baseline build failed")
            return 3
        baseline_dll = stage_pc_dll("baseline")
        if baseline_dll is None:
            return 3

        log("Phase 2: build candidate PresentationCore.dll")
        rc, _ = git("checkout", "--quiet", head_sha)
        if rc != 0:
            log("FATAL: could not return to HEAD")
            return 3
        if not build_pc_release():
            log("FATAL: candidate build failed")
            return 3
        candidate_dll = stage_pc_dll("candidate")
        if candidate_dll is None:
            return 3
    finally:
        # Always return to HEAD even on failure
        git("checkout", "--quiet", head_sha)

    # ── Build microbench harness ────────────────────────────────────────────
    log("Phase 3: publish microbench harness")
    if not publish_microbench():
        log("FATAL: microbench publish failed")
        return 3

    # ── Run both sides ──────────────────────────────────────────────────────
    log("Phase 4: run baseline BDN")
    if not swap_pc_into_publish(baseline_dll):
        return 4
    clear_bdn_artifacts()
    rc, out = run_bdn(args.filter)
    baseline_results = parse_bdn_results()
    if not baseline_results:
        log(f"BENCH-FAIL: baseline produced no results (rc={rc})")
        for line in out.splitlines()[-10:]:
            log(f"  {line}")
        if not args.no_revert:
            revert_head()
        return 4
    # Save baseline JSON before overwriting with candidate run
    baseline_snapshot = STAGING / f"baseline-{base_sha[:8]}.json"
    baseline_snapshot.write_text(json.dumps(baseline_results, indent=2))
    log(f"  baseline: {len(baseline_results)} benchmark(s) captured → {baseline_snapshot.name}")

    log("Phase 5: run candidate BDN")
    if not swap_pc_into_publish(candidate_dll):
        return 4
    clear_bdn_artifacts()
    rc, out = run_bdn(args.filter)
    candidate_results = parse_bdn_results()
    if not candidate_results:
        log(f"BENCH-FAIL: candidate produced no results (rc={rc})")
        for line in out.splitlines()[-10:]:
            log(f"  {line}")
        if not args.no_revert:
            revert_head()
        return 4
    candidate_snapshot = STAGING / f"candidate-{head_sha[:8]}.json"
    candidate_snapshot.write_text(json.dumps(candidate_results, indent=2))
    log(f"  candidate: {len(candidate_results)} benchmark(s) captured → {candidate_snapshot.name}")

    # ── Decide ─────────────────────────────────────────────────────────────
    log("Phase 6: decide")

    by_name_b = {b["fullName"]: b for b in baseline_results}
    by_name_c = {b["fullName"]: b for b in candidate_results}
    common = sorted(set(by_name_b) & set(by_name_c))
    if not common:
        log("BENCH-FAIL: no common benchmarks between baseline and candidate")
        if not args.no_revert:
            revert_head()
        return 4

    verdicts = []
    for fullname in common:
        v, reason = decide(by_name_b[fullname], by_name_c[fullname])
        log(f"  [{v:14s}] {fullname.split('.')[-1]}: {reason}")
        verdicts.append((fullname, v, reason))

    # Aggregate: any KEEP → KEEP. Any REJECT → REJECT. Otherwise UNCLEAR.
    has_keep = any(v == "KEEP" for _, v, _ in verdicts)
    has_reject = any(v == "REJECT" for _, v, _ in verdicts)

    if has_reject:
        verdict = "REJECT"
        rc_out = 1
    elif has_keep:
        verdict = "KEEP"
        rc_out = 0
    else:
        verdict = "REJECT-UNCLEAR"
        rc_out = 2

    log(f"FINAL: {verdict}")

    # Append result row (compact)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tier": "B",
        "bench_name": args.bench_name,
        "filter": args.filter,
        "head": head_sha,
        "base": base_sha,
        "verdict": verdict,
        "per_bench": [{"name": fn, "verdict": v, "reason": r} for fn, v, r in verdicts],
    }
    with open(RESULTS_JSONL, "a") as f:
        f.write(json.dumps(row) + "\n")

    if rc_out != 0 and not args.no_revert:
        revert_head()

    return rc_out


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
