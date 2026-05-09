#!/usr/bin/env python3
"""Tier C scenario-alloc eval — whole-scenario A/B on total GC allocation bytes.

Complements microbench.py (Tier B per-method BDN) by running the take-open
scenario end-to-end against baseline vs candidate PresentationCore.dll and
deciding KEEP/REJECT on WPF-attributed GC allocation delta.

This closes the gap where microbench.py cannot reward fixes that reduce call
volume (e.g. eliminating per-render-pass MatrixTransform allocations in
Visual.TransformToAncestor) — the per-call cost stays constant but the total
alloc budget for a real take-open drops.

Architecture:
  1. Build PresentationCore.dll for HEAD~1 (baseline)  → save as baseline_pc
  2. Build PresentationCore.dll for HEAD   (candidate) → save as candidate_pc
  3. For each side (K runs):
     a. Swap PresentationCore.dll directly into MC's self-contained app dir
        (MC ships its own coreclr.dll + PresentationCore.dll; DOTNET_ROOT env
        does NOT apply to self-contained apps because the .NET host resolves
        framework DLLs from the app-local directory before consulting any
        DOTNET_ROOT setting — verified: MC_BUILD/MotionCatalyst-cli.runtimeconfig.json
        lists includedFrameworks, confirming self-contained publish)
     b. Run scenario-take-open.py (already captures a .nettrace with
        Microsoft-Windows-DotNETRuntime:0x1FFBCCBFF:5 = GCAllocationTick events)
     c. Sum WPF-attributed AllocationTick amounts from the .nettrace via AllocParser
        (max-frame value = most inclusive WPF stack frame = total WPF-attributed alloc)
        and emit "[ALLOC] gross_bytes=NNNN" to stdout
  4. Apply decision rule on K-sample t-CIs:
     - significant = non-overlapping 95% CIs across K runs
     - sub-floor = 256 KB total-alloc delta (end-to-end noise floor)
     - KEEP if alloc significantly down, no regression
     - else REJECT or REJECT-UNCLEAR

Shadow note: MC build at MC_BUILD is self-contained (coreclr.dll + PresentationCore.dll
live in the same directory). DOTNET_ROOT / DOTNET_ROOT_X64 env overrides have NO
effect on self-contained apps. The only way to exercise our local build is to copy
the staged PresentationCore.dll into MC_BUILD directly. We restore the original
after all runs to leave the MC installation clean.

AllocParser note: AllocParser uses inclusive attribution — each AllocationTick event's
bytes are charged to every frame on the call stack. The frame with the highest
alloc_bytes is the most inclusive WPF frame (the root of the WPF dispatch stack),
which accumulates all child allocations. For the take-open scenario, this is
effectively the total WPF-attributed allocation budget, which is what we want to
minimize. Non-WPF allocations (BCL, user code outside WPF) are excluded by design —
this is appropriate since we are optimizing WPF source.

Exit codes (same as microbench.py):
  0  KEEP
  1  REJECT
  2  REJECT-UNCLEAR
  3  BUILD-FAIL
  4  BENCH-FAIL
  5  DIRTY-TREE
  6  PATH-VIOLATION

Usage:
  python3 microbench-scenario.py --scenario take-open --bench-name 'scenario-take-open'
  # Smoke test (K=2 for speed, no revert):
  python3 microbench-scenario.py --scenario take-open --bench-name smoke --k 2 --no-revert
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# ─── Import shared helpers from microbench.py ────────────────────────────────
# microbench.py lives in the same directory; we import its helpers directly.
# This avoids copy-paste drift and ensures shadow setup, path allowlist, and
# git helpers all behave identically to the Tier B runner.

_THIS_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_THIS_DIR))

from microbench import (  # noqa: E402
    ASSEMBLIES,
    ALLOWED_PATH_PREFIXES,
    BUILD_TIMEOUT_S,
    HALT_FILE,
    RESULTS_JSONL,
    ROOT,
    WPF_REPO,
    build_assemblies,
    check_path_allowlist,
    git,
    git_sha,
    setup_dotnet_shadow,
    stage_assemblies,
    to_winpath,
    working_tree_clean,
)

# ─── Configuration ────────────────────────────────────────────────────────────

# MC self-contained build directory — contains PresentationCore.dll alongside
# coreclr.dll. This is where we swap DLLs for scenario runs (DOTNET_ROOT doesn't
# apply to self-contained apps).
MC_BUILD = Path("/c/work/desktop/wpf-test/src/motioncatalyst/BUILD/x64_Release")

# Scenario scripts map. Currently only take-open is wired up; add more here as
# they become stable enough for A/B comparison.
SCENARIO_SCRIPTS: dict[str, Path] = {
    "take-open": WPF_REPO / "tools" / "poc" / "scenario-take-open.py",
}

# Number of MEASURED runs per side (after warmup). Must be >= 5 for the decision
# rule (5-sample t-CI). Each run is a full take-open scenario (~30-90s wall).
SCENARIO_RUNS_K = int(os.environ.get("WPF_AR_SCENARIO_K", "5"))

# Number of warmup runs per side that are discarded (not included in measurements).
# JIT-cold run 0 typically has ~4-5x more GCAllocationTick bytes than subsequent
# runs because the CLR JIT-compiles WPF code and records the IL-to-native alloc
# in AllocationTick. Without discarding, run-0 dominates the mean and inflates
# variance, making all verdicts REJECT-UNCLEAR. Default: 1 warmup run per side.
SCENARIO_WARMUP_RUNS = int(os.environ.get("WPF_AR_SCENARIO_WARMUP", "1"))

# AllocParser binary — parses GCAllocationTick events from .nettrace to get
# WPF-attributed allocation bytes. The max-frame value (most inclusive WPF frame)
# approximates total WPF-attributed allocation for the scenario run.
ALLOC_PARSER_EXE = (
    WPF_REPO / "tools" / "alloc-parser" / "bin" / "Release"
    / "net10.0-windows" / "win-x64" / "publish" / "AllocParser.exe"
)

# Timeout per scenario run (seconds). take-open is typically 30-90s; 300s is
# generous headroom for a slow CI machine.
SCENARIO_TIMEOUT_S = int(os.environ.get("WPF_AR_SCENARIO_TIMEOUT", "300"))

# Sub-floor for alloc delta. 256 KB = 262144 bytes. End-to-end scenario noise
# (JIT warm-up, GC heuristics, lazy init on first run) can shift WPF-attributed
# alloc by a few hundred KB. Only signal consistently larger than this floor is
# treated as a real win.
MIN_SCENARIO_ALLOC_BYTES = int(os.environ.get("WPF_AR_SCENARIO_MIN_ALLOC", str(256 * 1024)))

# Sentinel file written while we hold the MC swap lock.
SCENARIO_LOCK = ROOT / "SCENARIO_LOCK"

# Staging dir for per-side results.
STAGING = ROOT / "microbench-staging"


def log(msg: str) -> None:
    print(f"[microbench-scenario] {msg}", flush=True)


# ─── MC build dir DLL swap ────────────────────────────────────────────────────


def save_original_pc() -> Path | None:
    """Back up the current PresentationCore.dll in MC_BUILD.

    Returns the backup path, or None if the DLL doesn't exist.
    """
    src = MC_BUILD / "PresentationCore.dll"
    if not src.exists():
        return None
    backup = MC_BUILD / "PresentationCore.dll.scenario-backup"
    shutil.copy2(src, backup)
    return backup


def restore_original_pc(backup: Path | None) -> None:
    """Restore PresentationCore.dll in MC_BUILD from backup.

    Retries up to 5 times with 2-second waits to handle the case where a
    crashed MC-cli process still holds the file lock briefly after taskkill.
    A failed restore is still reported so the caller can investigate.
    """
    target = MC_BUILD / "PresentationCore.dll"
    if backup is None or not backup.exists():
        return
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            shutil.copy2(backup, target)
            backup.unlink(missing_ok=True)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < 4:
                log(f"  restore attempt {attempt + 1}/5 failed ({exc}); retrying in 2s ...")
                time.sleep(2.0)
    log(f"  WARNING: could not restore MC PresentationCore.dll after 5 attempts: {last_exc}")
    log(f"    Manual fix: cp {backup} {target}")


def swap_pc_into_mc_build(staged_dll: Path) -> bool:
    """Copy staged PresentationCore.dll into MC's self-contained app directory.

    MC is self-contained — it ships its own runtime and WPF DLLs in x64_Release/.
    DOTNET_ROOT env overrides have no effect; we must copy directly here.

    Returns True on success.
    """
    target = MC_BUILD / "PresentationCore.dll"
    if not MC_BUILD.exists():
        log(f"  FATAL: MC_BUILD dir does not exist: {MC_BUILD}")
        return False
    if not staged_dll.exists():
        log(f"  FATAL: staged DLL not found: {staged_dll}")
        return False
    try:
        shutil.copy2(staged_dll, target)
        log(f"  swapped PresentationCore.dll into MC_BUILD ({staged_dll.name} -> {target.name})")
        return True
    except Exception as exc:
        log(f"  FATAL: DLL swap failed: {exc}")
        return False


# ─── AllocParser integration ──────────────────────────────────────────────────


def wpf_attributed_alloc_from_nettrace(nettrace: Path) -> int | None:
    """Return WPF-attributed allocation bytes from a .nettrace via AllocParser.

    AllocParser uses inclusive attribution: each GCAllocationTick event's bytes
    are charged to every frame on the call stack. The frame with the highest
    alloc_bytes is the most inclusive WPF frame — effectively the root of the
    WPF dispatch stack, which accumulates all child allocations. For the
    take-open scenario, this is the total WPF-attributed allocation budget.

    Emits "[ALLOC] gross_bytes=NNNN" to stdout on success (matching the spec's
    expected output format so callers can parse it from process stdout).

    Returns gross_bytes (int) or None on failure.
    """
    if not ALLOC_PARSER_EXE.exists():
        log(f"  WARNING: AllocParser not found at {ALLOC_PARSER_EXE}")
        return None

    with tempfile.NamedTemporaryFile(
        suffix=".alloc.json", delete=False, dir=str(nettrace.parent)
    ) as tf:
        out_path = Path(tf.name)

    try:
        rc_proc = subprocess.run(
            ["cmd.exe", "/c",
             to_winpath(ALLOC_PARSER_EXE),
             to_winpath(nettrace),
             "--output", to_winpath(out_path)],
            capture_output=True, text=True, timeout=180,
        )
        if rc_proc.returncode != 0:
            log(f"  AllocParser failed (rc={rc_proc.returncode}): "
                f"{rc_proc.stderr[:200]}")
            return None

        raw = out_path.read_text(encoding="utf-8").strip()
        if not raw:
            log("  AllocParser produced empty output")
            return None

        entries = json.loads(raw)
        if not entries:
            log("  AllocParser produced empty JSON array")
            return None

        # The entry with the maximum alloc_bytes is the most inclusive WPF frame
        # (the root frame that accumulated all child allocations). This is our
        # proxy for "total WPF-attributed allocation" for this scenario run.
        gross = max(
            int(e["alloc_bytes"])
            for e in entries
            if "alloc_bytes" in e and e["alloc_bytes"] > 0
        )
        print(f"[ALLOC] gross_bytes={gross}", flush=True)
        return gross

    except Exception as exc:
        log(f"  WARNING: alloc parse failed: {exc}")
        return None
    finally:
        out_path.unlink(missing_ok=True)
        # AllocParser creates a .etlx sidecar next to the nettrace; clean it up.
        etlx_candidates = [
            nettrace.with_suffix(".etlx"),
            nettrace.parent / (nettrace.stem + ".etlx"),
        ]
        for etlx in etlx_candidates:
            if etlx.exists():
                try:
                    etlx.unlink()
                except Exception:
                    pass


# ─── Scenario runner ──────────────────────────────────────────────────────────


def _run_scenario_attempt(
    script: Path, env: dict, run_dir: Path, run_label: str,
) -> tuple[int, list[str], float]:
    """Run scenario script once; return (rc, stdout_lines, elapsed_s)."""
    stdout_lines: list[str] = []
    t0 = time.monotonic()
    try:
        p = subprocess.Popen(
            ["python3", str(script)],
            cwd=str(WPF_REPO),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _drain() -> None:
            for line in p.stdout:  # type: ignore[union-attr]
                stdout_lines.append(line.rstrip())

        drain = threading.Thread(target=_drain, daemon=True)
        drain.start()
        try:
            rc = p.wait(timeout=SCENARIO_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log(f"  {run_label}: TIMEOUT after {SCENARIO_TIMEOUT_S}s")
            p.kill()
            drain.join(timeout=5.0)
            return -1, stdout_lines, time.monotonic() - t0
        drain.join(timeout=10.0)
        return rc, stdout_lines, time.monotonic() - t0
    except Exception as exc:
        log(f"  {run_label}: launch failed: {exc}")
        return -1, stdout_lines, time.monotonic() - t0


def run_scenario_once(
    scenario_slug: str, result_dir: Path, run_idx: int,
) -> int | None:
    """Run the scenario script once; return WPF-attributed alloc bytes or None.

    The scenario script captures a .nettrace (includes GCAllocationTick events
    via Microsoft-Windows-DotNETRuntime:0x1FFBCCBFF:5). We parse that nettrace
    via AllocParser to get WPF-attributed allocation bytes for this run.

    Automatically retries once on rc=99 (unhandled exception in the scenario
    script — typically a transient DISPATCHER_BUSY or broker IPC hiccup).
    The retry uses a fresh result sub-directory (run-NN-retry1) so the
    original artefacts are preserved for debugging.
    """
    script = SCENARIO_SCRIPTS.get(scenario_slug)
    if script is None:
        log(f"  unknown scenario: {scenario_slug}")
        return None

    # Up to 2 attempts: the initial run plus one retry on rc=99.
    for attempt in range(2):
        attempt_suffix = "" if attempt == 0 else f"-retry{attempt}"
        run_dir = result_dir / f"run-{run_idx:02d}{attempt_suffix}"
        run_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SCENARIO_RESULT_DIR"] = str(run_dir)
        env["SCENARIO_NAME"] = f"{scenario_slug}-run{run_idx:02d}{attempt_suffix}"
        env["MC_PERF_MODE"] = "1"

        run_label = f"run {run_idx}" + (f" retry{attempt}" if attempt else "")
        log(f"  {run_label}: launching {script.name} -> {run_dir}")

        rc, stdout_lines, elapsed = _run_scenario_attempt(script, env, run_dir, run_label)

        if rc == 99 and attempt == 0:
            # rc=99 is the scenario's bare except-Exception sentinel — a transient
            # IPC or dispatcher failure.  Retry once with a 5 s cool-down to let
            # any lingering MC-cli processes from the crashed run exit cleanly.
            log(f"  {run_label}: rc=99 (transient exception); retrying in 5 s ...")
            for line in stdout_lines[-10:]:
                log(f"    {line}")
            time.sleep(5.0)
            continue

        elapsed_str = f"{elapsed:.1f}s"
        if rc != 0:
            log(f"  {run_label}: scenario failed (rc={rc}, {elapsed_str})")
            for line in stdout_lines[-5:]:
                log(f"    {line}")
            return None

        log(f"  {run_label}: scenario done ({elapsed_str})")
        break  # success or non-retryable failure
    else:
        # Both attempts returned rc=99.
        log(f"  run {run_idx}: both attempts failed (rc=99); giving up")
        return None

    if rc != 0:
        return None

    # Find the nettrace produced by this run.
    nettraces = sorted(
        run_dir.glob("*.nettrace"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not nettraces:
        log(f"  run {run_idx}: no .nettrace produced in {run_dir}")
        return None

    nettrace = nettraces[0]
    nettrace_mb = nettrace.stat().st_size / 1024 / 1024
    log(f"  run {run_idx}: parsing {nettrace.name} ({nettrace_mb:.1f} MB) via AllocParser ...")

    gross = wpf_attributed_alloc_from_nettrace(nettrace)
    if gross is None:
        log(f"  run {run_idx}: AllocParser returned no data")
        return None

    log(f"  run {run_idx}: gross_bytes={gross:,}  ({gross/1024/1024:.1f} MB)")
    return gross


def run_k_runs(
    scenario_slug: str, side: str, staged_dll: Path, k: int, warmup: int = 0,
) -> list[int]:
    """Swap DLL, run (warmup + K) scenario runs, return list of K gross_bytes.

    The first `warmup` runs are discarded (JIT-cold; AllocationTick records
    IL-to-native compilation bytes that inflate alloc ~4-5x vs steady state).
    Returns list of measured (non-warmup) alloc measurements. May be shorter
    than K if some runs fail.
    """
    if not swap_pc_into_mc_build(staged_dll):
        return []

    result_dir = STAGING / f"scenario-{scenario_slug}-{side}"
    result_dir.mkdir(parents=True, exist_ok=True)

    total_runs = warmup + k
    results: list[int] = []
    for i in range(total_runs):
        is_warmup = i < warmup
        label = f"warmup-{i}" if is_warmup else f"meas-{i - warmup}"
        gross = run_scenario_once(scenario_slug, result_dir, i)
        if is_warmup:
            if gross is not None:
                log(f"  run {i} [{label}]: gross_bytes={gross:,} (discarded — warmup)")
            else:
                log(f"  run {i} [{label}]: failed (warmup — continuing)")
            continue
        if gross is not None:
            results.append(gross)
            log(f"  run {i} [{label}]: gross_bytes={gross:,}  ({len(results)}/{k} measured)")
        else:
            log(f"  run {i} [{label}]: failed; continuing ({len(results)}/{i - warmup + 1} ok)")

    return results


# ─── Decision rule ────────────────────────────────────────────────────────────


def mean_and_ci(samples: list[int]) -> tuple[float, float, float]:
    """Return (mean, ci_lower, ci_upper) using a 95% two-tailed t-interval.

    Uses Student's t-distribution for small samples (n < 30). Requires n >= 2.
    For n = 1, returns (mean, mean, mean) as a degenerate case.
    """
    n = len(samples)
    if n < 2:
        m = float(samples[0]) if samples else 0.0
        return m, m, m

    m = sum(samples) / n
    variance = sum((x - m) ** 2 for x in samples) / (n - 1)
    std_err = math.sqrt(variance / n) if variance > 0 else 0.0

    # Two-tailed 95% CI t critical values (df = n-1, alpha = 0.025 per tail).
    # Source: standard t-table.
    t_table = {
        2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
        7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262,
        15: 2.145, 20: 2.093, 25: 2.060,
    }
    keys = sorted(t_table)
    t_crit = 1.96  # large-n approximation
    for i_k, k_val in enumerate(keys):
        if n == k_val:
            t_crit = t_table[k_val]
            break
        if i_k + 1 < len(keys) and k_val < n < keys[i_k + 1]:
            k_lo, k_hi = k_val, keys[i_k + 1]
            # Linear interpolation between adjacent table entries.
            t_crit = t_table[k_lo] + (t_table[k_hi] - t_table[k_lo]) * (n - k_lo) / (k_hi - k_lo)
            break
        if n < k_val:
            t_crit = t_table[k_val]
            break

    margin = t_crit * std_err
    return m, m - margin, m + margin


def cis_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return not (hi1 < lo2 or hi2 < lo1)


def decide_scenario(baseline_samples: list[int], candidate_samples: list[int]) -> tuple[str, str]:
    """Compare K baseline vs K candidate alloc measurements; return (verdict, reason)."""
    if len(baseline_samples) < 2 or len(candidate_samples) < 2:
        return "REJECT-UNCLEAR", (
            f"insufficient samples: baseline={len(baseline_samples)}, "
            f"candidate={len(candidate_samples)} (need >= 2 each)"
        )

    b_mean, b_lo, b_hi = mean_and_ci(baseline_samples)
    c_mean, c_lo, c_hi = mean_and_ci(candidate_samples)

    delta = c_mean - b_mean
    significant = not cis_overlap(b_lo, b_hi, c_lo, c_hi)
    meaningful = abs(delta) >= MIN_SCENARIO_ALLOC_BYTES

    delta_kb = delta / 1024
    b_mean_mb = b_mean / 1024 / 1024
    c_mean_mb = c_mean / 1024 / 1024

    if not significant:
        return "REJECT-UNCLEAR", (
            f"no significant signal: alloc delta {delta_kb:+.0f} KB "
            f"(baseline={b_mean_mb:.1f} MB, candidate={c_mean_mb:.1f} MB, CIs overlap)"
        )

    if not meaningful:
        return "REJECT-UNCLEAR", (
            f"sub-floor: alloc delta {delta_kb:+.0f} KB "
            f"(floor={MIN_SCENARIO_ALLOC_BYTES // 1024} KB), CIs disjoint"
        )

    if delta > 0:
        return "REJECT", (
            f"alloc regressed: {b_mean_mb:.1f} -> {c_mean_mb:.1f} MB "
            f"(delta {delta_kb:+.0f} KB, CIs disjoint)"
        )

    return "KEEP", (
        f"alloc win: {b_mean_mb:.1f} -> {c_mean_mb:.1f} MB "
        f"(delta {delta_kb:+.0f} KB, CIs disjoint)"
    )


# ─── Main flow ────────────────────────────────────────────────────────────────


def revert_head() -> None:
    log("reverting HEAD via git revert --no-edit ...")
    rc, out = git("revert", "--no-edit", "HEAD")
    if rc != 0:
        log(f"git revert failed: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="take-open",
                        help="Scenario slug (e.g. take-open). Default: take-open")
    parser.add_argument("--bench-name", default="scenario-take-open",
                        help="Short name for results.jsonl row")
    parser.add_argument("--k", type=int, default=SCENARIO_RUNS_K,
                        help=f"Measured runs per side (default: {SCENARIO_RUNS_K})")
    parser.add_argument("--warmup", type=int, default=SCENARIO_WARMUP_RUNS,
                        help=f"Warmup runs per side (discarded, not measured; "
                             f"default: {SCENARIO_WARMUP_RUNS}). Warmup runs absorb "
                             f"JIT-cold AllocationTick inflation (~4-5x vs steady state).")
    parser.add_argument("--no-revert", action="store_true",
                        help="Do not git-revert on REJECT (for debugging)")
    parser.add_argument("--ignore-halt", action="store_true",
                        help="Ignore HALT sentinel (for testing/smoke runs only)")
    args = parser.parse_args()

    if args.scenario not in SCENARIO_SCRIPTS:
        log(f"FATAL: unknown scenario '{args.scenario}'. Available: {list(SCENARIO_SCRIPTS)}")
        return 4

    if not working_tree_clean():
        log("FATAL: working tree not clean. Commit your change before running microbench-scenario.")
        return 5

    if HALT_FILE.exists() and not args.ignore_halt:
        log(f"HALT sentinel present at {HALT_FILE} — not running scenario benchmark")
        log("  Use --ignore-halt to bypass (testing/smoke runs only)")
        return 4

    if not ALLOC_PARSER_EXE.exists():
        log(f"FATAL: AllocParser not found at {ALLOC_PARSER_EXE}")
        log("  Build it: cd /c/work/wpf-perf/tools/alloc-parser && "
            "dotnet publish -c Release -r win-x64 --self-contained")
        return 4

    head_sha = git_sha("HEAD")
    base_sha = git_sha("HEAD~1")
    log(f"baseline={base_sha[:8]}  candidate={head_sha[:8]}  scenario='{args.scenario}'  K={args.k}")

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

    # ── Sentinel lock ──────────────────────────────────────────────────────
    # Write SCENARIO_LOCK so concurrent ralph iters don't race to swap the
    # MC build dir's PresentationCore.dll simultaneously.
    SCENARIO_LOCK.write_text(
        f"pid={os.getpid()} started={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
        encoding="utf-8",
    )

    # Back up MC's original PresentationCore.dll before any swap.
    # Always restored in the finally block.
    pc_backup = save_original_pc()
    if pc_backup is not None:
        log(f"  backed up MC PresentationCore.dll -> {pc_backup.name}")
    else:
        log("  WARNING: MC PresentationCore.dll not found at MC_BUILD; swap may fail")

    # Shadow setup — needed for build_assemblies / stage_assemblies infrastructure.
    # The shadow itself is NOT used for MC runtime (MC is self-contained).
    if not setup_dotnet_shadow():
        log("FATAL: failed to set up DOTNET_ROOT shadow (needed for build infrastructure).")
        SCENARIO_LOCK.unlink(missing_ok=True)
        restore_original_pc(pc_backup)
        return 4

    try:
        # ── Build both sides ────────────────────────────────────────────────
        log(f"Phase 1: build baseline assemblies ({', '.join(a['name'] for a in ASSEMBLIES)})")
        rc, _ = git("checkout", "--quiet", base_sha)
        if rc != 0:
            log("FATAL: could not checkout HEAD~1")
            return 3
        try:
            if not build_assemblies():
                log("FATAL: baseline build failed")
                return 3
            baseline_staged = stage_assemblies("scenario.baseline")
            if baseline_staged is None:
                return 3

            log("Phase 2: build candidate assemblies")
            rc, _ = git("checkout", "--quiet", head_sha)
            if rc != 0:
                log("FATAL: could not return to HEAD")
                return 3
            if not build_assemblies():
                log("FATAL: candidate build failed")
                return 3
            candidate_staged = stage_assemblies("scenario.candidate")
            if candidate_staged is None:
                return 3
        finally:
            git("checkout", "--quiet", head_sha)

        # PresentationCore is the only assembly we swap into MC's app dir.
        # WindowsBase and System.Xaml are also staged (used by microbench.py's
        # shadow swap) but MC's self-contained layout means only PresentationCore
        # matters for the scenario alloc signal.
        baseline_pc = baseline_staged.get("PresentationCore")
        candidate_pc = candidate_staged.get("PresentationCore")
        if baseline_pc is None or candidate_pc is None:
            log("FATAL: PresentationCore not in staged assemblies")
            return 3

        log(f"  baseline PC staged: {baseline_pc.name}")
        log(f"  candidate PC staged: {candidate_pc.name}")
        log(f"  MC_BUILD: {MC_BUILD}")
        log("  NOTE: DOTNET_ROOT shadow NOT used for MC (self-contained app);")
        log("        DLL is swapped directly into MC_BUILD/PresentationCore.dll")

        # ── Run baseline side ───────────────────────────────────────────────
        log(f"Phase 3: run baseline scenario (warmup={args.warmup}, K={args.k})")
        baseline_samples = run_k_runs(
            args.scenario, "baseline", baseline_pc, args.k, warmup=args.warmup
        )
        log(f"  baseline: {len(baseline_samples)}/{args.k} measured  samples={baseline_samples}")
        if len(baseline_samples) < 2:
            log(f"BENCH-FAIL: baseline produced only {len(baseline_samples)} usable measurements")
            if not args.no_revert:
                revert_head()
            return 4

        # ── Run candidate side ──────────────────────────────────────────────
        log(f"Phase 4: run candidate scenario (warmup={args.warmup}, K={args.k})")
        candidate_samples = run_k_runs(
            args.scenario, "candidate", candidate_pc, args.k, warmup=args.warmup
        )
        log(f"  candidate: {len(candidate_samples)}/{args.k} measured  samples={candidate_samples}")
        if len(candidate_samples) < 2:
            log(f"BENCH-FAIL: candidate produced only {len(candidate_samples)} usable measurements")
            if not args.no_revert:
                revert_head()
            return 4

    finally:
        # Always restore MC's original PresentationCore.dll and release lock.
        restore_original_pc(pc_backup)
        SCENARIO_LOCK.unlink(missing_ok=True)
        log("  restored MC PresentationCore.dll; SCENARIO_LOCK released")

    # ── Decide ─────────────────────────────────────────────────────────────
    log("Phase 5: decide")
    b_mean, b_lo, b_hi = mean_and_ci(baseline_samples)
    c_mean, c_lo, c_hi = mean_and_ci(candidate_samples)
    log(f"  baseline:  mean={b_mean/1024/1024:.2f} MB  CI=[{b_lo/1024/1024:.2f}, {b_hi/1024/1024:.2f}]  n={len(baseline_samples)}")
    log(f"  candidate: mean={c_mean/1024/1024:.2f} MB  CI=[{c_lo/1024/1024:.2f}, {c_hi/1024/1024:.2f}]  n={len(candidate_samples)}")

    verdict, reason = decide_scenario(baseline_samples, candidate_samples)
    log(f"  [{verdict}] {reason}")
    log(f"FINAL: {verdict}")

    rc_out = {"KEEP": 0, "REJECT": 1, "REJECT-UNCLEAR": 2}.get(verdict, 2)

    # ── Append result row ──────────────────────────────────────────────────
    # Schema mirrors microbench.py with tier="C" and scenario-specific fields.
    scenario_name = f"scenario-{args.scenario}"
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tier": "C",
        "bench_name": args.bench_name,
        "scenario": args.scenario,
        "head": head_sha,
        "base": base_sha,
        "verdict": verdict,
        "k": args.k,
        "warmup": args.warmup,
        "baseline_samples_bytes": baseline_samples,
        "candidate_samples_bytes": candidate_samples,
        "baseline_mean_bytes": round(b_mean),
        "candidate_mean_bytes": round(c_mean),
        "delta_bytes": round(c_mean - b_mean),
        "per_bench": [
            {
                "name": scenario_name,
                "verdict": verdict,
                "reason": reason,
                "baseline_mean_mb": round(b_mean / 1024 / 1024, 3),
                "candidate_mean_mb": round(c_mean / 1024 / 1024, 3),
                "delta_kb": round((c_mean - b_mean) / 1024, 1),
            }
        ],
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
