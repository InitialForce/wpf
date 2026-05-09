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
     - absolute floors guard against trivial wins (alloc < 16 B/op,
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
  5  DIRTY-TREE      — working tree not clean; no action taken
  6  PATH-VIOLATION  — HEAD commit touches forbidden paths; reverted
  7  HALT            — diagnostic threshold reached (10 consecutive REJECT-UNCLEAR
                       across all tier-B rows). HALT sentinel file written.

Usage:
  python3 microbench.py --filter '*LayoutManager*' --bench-name 'layout-update'
"""

from __future__ import annotations

import argparse
import datetime
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
BUILD_SCRIPT = WPF_REPO / "build-pf-perf.ps1"

# WPF product assemblies whose source is editable per the path allowlist AND
# which we can build locally. Each entry's locally-built copy is staged and
# swapped into microbench's publish dir for both the baseline and candidate BDN
# runs — without that, edits in WindowsBase / System.Xaml silently no-op
# because BDN keeps loading the system runtime pack version. Building
# PresentationFramework.csproj (via build-pf-perf.ps1) transitively rebuilds
# PresentationCore + WindowsBase + System.Xaml via project refs, so a single
# build script call produces all four DLLs.
#
# PresentationFramework is NOW included. build-pf-perf.ps1 bypasses the
# DirectWriteForwarder.vcxproj C++ reference (and the transitive DWF ref in
# ReachFramework.csproj) via SkipDirectWriteForwarderProjectRef=true +
# DirectWriteForwarderBinaryPath pointing at the installed WindowsDesktop pack
# — the same technique used for PresentationCore in the previous build script.
# ABI verification (assembly version 10.0.0.0, PublicKeyToken=31bf3856ad364e35)
# confirms the locally-built PF.dll is metadata-compatible with the shadow's
# WindowsDesktop pack copy, so the same dual-swap mechanism (publish dir +
# shadow pack) that works for PC also works for PF.
ASSEMBLIES: list[dict] = [
    {
        "name": "PresentationFramework",
        "build_dir": WPF_REPO / "artifacts" / "bin" / "PresentationFramework" / "x64" / "Release" / "net10.0",
    },
    {
        "name": "PresentationCore",
        "build_dir": WPF_REPO / "artifacts" / "bin" / "PresentationCore" / "x64" / "Release" / "net10.0",
    },
    {
        "name": "WindowsBase",
        "build_dir": WPF_REPO / "artifacts" / "bin" / "WindowsBase" / "x64" / "Release" / "net10.0",
    },
    {
        "name": "System.Xaml",
        "build_dir": WPF_REPO / "artifacts" / "bin" / "System.Xaml" / "x64" / "Release" / "net10.0",
    },
]

MICROBENCH_PROJ = WPF_REPO / "microbench"
MICROBENCH_PUBLISH = MICROBENCH_PROJ / "bin" / "Release" / "net10.0-windows" / "win-x64" / "publish"
MICROBENCH_EXE = MICROBENCH_PUBLISH / "Microbenchmarks.exe"
MICROBENCH_RESULTS = MICROBENCH_PUBLISH / "BenchmarkDotNet.Artifacts" / "results"

# DOTNET_ROOT shadow for out-of-process BDN. See AutoresearchConfig.cs comment
# for the full diagnosis. The shadow is a composite directory containing
# junctions back to the system .NET installation for sdk/host/packs/Microsoft.
# NETCore.App, plus a physical deep copy of the Microsoft.WindowsDesktop.App
# pack (so the per-iter DLL swap can overwrite WindowsBase / System.Xaml /
# PresentationCore without affecting other .NET apps on the box). BDN's inner
# build child processes resolve framework assemblies via DOTNET_ROOT, so they
# load our patched DLLs from the shadow's WindowsDesktop.App pack.
DOTNET_SHADOW = WPF_REPO / ".dotnet-shadow"
DOTNET_SHADOW_WIN = "C:\\work\\wpf-perf\\.dotnet-shadow"
DOTNET_SYS = Path("/c/Program Files/dotnet")
DOTNET_SYS_WIN = "C:\\Program Files\\dotnet"

STAGING = ROOT / "microbench-staging"
RESULTS_TSV = ROOT / "results.tsv"
RESULTS_JSONL = ROOT / "results.jsonl"
COOLDOWN_JSON = ROOT / "cooldown.json"

BUILD_TIMEOUT_S = int(os.environ.get("WPF_AR_BUILD_TIMEOUT", "600"))
BENCH_TIMEOUT_S = int(os.environ.get("WPF_AR_BENCH_TIMEOUT", "300"))

# Diagnostic halt: if the last N consecutive tier-B rows are all REJECT-UNCLEAR,
# stop the loop and write a HALT sentinel. Default is 10; tunable via env var
# WPF_AR_HALT_UNCLEAR_THRESHOLD (e.g., export WPF_AR_HALT_UNCLEAR_THRESHOLD=5
# for a shorter patience window during testing).
HALT_FILE = ROOT / "HALT"
HALT_UNCLEAR_THRESHOLD = int(os.environ.get("WPF_AR_HALT_UNCLEAR_THRESHOLD", "10"))

# Absolute floors to avoid trivial wins. Alloc: 16 bytes/op ≈ 2 pointer-sized
# fields per iteration — small enough that a wrapper-kill (CCM=48B, boxed
# enum=24B, SyncCtx=32B) registers as a real win. BDN reports
# BytesAllocatedPerOperation deterministically (CV ≈ 0 in steady state), so an
# aggressive floor here is safe — it's the only signal that survives the time
# axis's ~1-3 ns/op noise on STA-batch benchmarks. Time: 5 ns/op ≈ 16 cycles on
# a 3.4 GHz CPU.
MIN_ALLOC_BYTES_PER_OP = float(os.environ.get("WPF_AR_MIN_ALLOC_BYTES", "16"))
MIN_TIME_NS_PER_OP = float(os.environ.get("WPF_AR_MIN_TIME_NS", "5"))

# Inner-loop commits may ONLY touch product code under these prefixes (relative
# to WPF_REPO). Prevents the agent from editing benchmarks, baselines, harness
# scripts, or profile manifests — the consensus models flagged this as the
# single biggest stop-the-line risk ("Goodhart's Law on benchmark code").
#
# PresentationFramework is now included. build-pf-perf.ps1 bypasses the
# DirectWriteForwarder C++ build via SkipDirectWriteForwarderProjectRef=true,
# and the locally-built PF.dll is ABI-compatible with the shadow pack copy
# (Version=10.0.0.0, PublicKeyToken=31bf3856ad364e35 match), so dual-swap
# (publish dir + shadow WindowsDesktop pack) works identically to PC/WB/SX.
ALLOWED_PATH_PREFIXES = (
    "src/Microsoft.DotNet.Wpf/src/PresentationFramework/",
    "src/Microsoft.DotNet.Wpf/src/PresentationCore/",
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
    """Convert /c/foo/bar → C:\\foo\\bar for PowerShell / cmd args. Mirrors
    eval.py since `cygpath` isn't reliably on PATH in this WSL setup."""
    s = str(p)
    if s.startswith("/c/"):
        return "C:\\" + s[3:].replace("/", "\\")
    return s


def git(*args: str, cwd: Path = WPF_REPO) -> tuple[int, str]:
    return cmd(["git", *args], cwd=cwd)


def git_sha(ref: str = "HEAD", cwd: Path = WPF_REPO) -> str:
    rc, out = git("rev-parse", ref, cwd=cwd)
    if rc != 0:
        raise RuntimeError(f"git rev-parse {ref} failed: {out}")
    return out.strip()


def working_tree_clean() -> bool:
    """True iff no TRACKED file differs from HEAD. Untracked files are ignored
    (the wpf-perf repo has many pre-existing untracked POC scripts that are
    irrelevant to autoresearch state)."""
    rc, _ = git("diff-index", "--quiet", "HEAD", "--")
    return rc == 0


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


def build_assemblies() -> bool:
    """Build all WPF product assemblies (Release, x64) via build-pf-perf.ps1.

    Returns True iff every ASSEMBLIES entry's <Name>.dll exists at its build_dir
    after the build. We delete the per-assembly DLLs first so a stale copy from
    a prior side can't masquerade as a successful new build.
    """
    log(f"  building {len(ASSEMBLIES)} WPF assemblies (Release, x64) at {git_sha()[:8]} …")
    for asm in ASSEMBLIES:
        dll = asm["build_dir"] / f"{asm['name']}.dll"
        if dll.exists():
            try:
                dll.unlink()
            except Exception:
                pass
    rc, out = cmd(
        ["cmd.exe", "/c", "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", to_winpath(BUILD_SCRIPT)],
        cwd=WPF_REPO, timeout=BUILD_TIMEOUT_S,
    )
    if rc != 0:
        log(f"  build failed (rc={rc}); tail:")
        for line in out.splitlines()[-20:]:
            log(f"    {line}")
        return False
    missing: list[str] = []
    for asm in ASSEMBLIES:
        dll = asm["build_dir"] / f"{asm['name']}.dll"
        if not dll.exists():
            missing.append(f"{asm['name']} → {dll}")
    if missing:
        log("  build succeeded but some DLLs are missing:")
        for m in missing:
            log(f"    {m}")
        return False
    return True


def detect_wpf_pack_version() -> str | None:
    """Return the highest installed Microsoft.WindowsDesktop.App version
    (e.g. '10.0.7'). Picks the one we'll target the shadow at.
    """
    pack_root = DOTNET_SYS / "shared" / "Microsoft.WindowsDesktop.App"
    if not pack_root.exists():
        return None
    versions = []
    for d in pack_root.iterdir():
        if d.is_dir() and d.name[0].isdigit():
            try:
                # Only consider 10.x.y series
                parts = d.name.split(".")
                if int(parts[0]) >= 10:
                    versions.append((tuple(int(p) for p in parts), d.name))
            except ValueError:
                continue
    if not versions:
        return None
    versions.sort()
    return versions[-1][1]


def setup_dotnet_shadow() -> bool:
    """Idempotent — build the shadow root if missing/incomplete.

    Layout:
      .dotnet-shadow/
        sdk/                                  → junction to system
        host/                                 → junction to system
        packs/                                → junction to system
        sdk-manifests, templates, swidtag, metadata → junctions to system
        shared/Microsoft.NETCore.App/         → junction to system
        shared/Microsoft.WindowsDesktop.App/<ver>/   ← physical copy (mutable)
        dotnet.exe, dnx.cmd                   → physical copies

    The pack version is determined dynamically from what the system has
    installed (highest 10.x.y). We re-create the shadow if the version
    drifted (e.g. .NET update) or if structural files are missing.
    """
    pack_ver = detect_wpf_pack_version()
    if pack_ver is None:
        log("FATAL: could not detect installed Microsoft.WindowsDesktop.App version")
        return False

    pack_dir = DOTNET_SHADOW / "shared" / "Microsoft.WindowsDesktop.App" / pack_ver
    sentinel = DOTNET_SHADOW / ".shadow-version"
    expected_sentinel = pack_ver

    # Fast path: sentinel matches → shadow is good as-is. Per-iter swap will
    # overwrite WindowsBase/System.Xaml/PresentationCore so the previous run's
    # patched DLLs do not leak into this run's measurement. (Both sides of the
    # A/B in this iter explicitly stage + swap their own builds, so any prior
    # contents are clobbered before measurement.)
    if (sentinel.exists() and sentinel.read_text().strip() == expected_sentinel
            and pack_dir.is_dir()
            and (pack_dir / "WindowsBase.dll").exists()
            and (DOTNET_SHADOW / "dotnet.exe").exists()):
        return True

    log(f"setting up DOTNET_ROOT shadow at {DOTNET_SHADOW} (pack={pack_ver}) …")
    DOTNET_SHADOW.mkdir(exist_ok=True, parents=True)
    (DOTNET_SHADOW / "shared").mkdir(exist_ok=True)

    # Junctions for the unchanged subtrees. cmd.exe mklink /J is the only way
    # to create directory junctions from this WSL setup (os.symlink would
    # require SeCreateSymbolicLinkPrivilege; junctions don't).
    junction_targets = [
        ("sdk", "sdk"),
        ("host", "host"),
        ("packs", "packs"),
        ("sdk-manifests", "sdk-manifests"),
        ("templates", "templates"),
        ("swidtag", "swidtag"),
        ("metadata", "metadata"),
        ("shared\\Microsoft.NETCore.App", "shared\\Microsoft.NETCore.App"),
    ]
    for sub, target in junction_targets:
        src_path = DOTNET_SHADOW / sub.replace("\\", "/")
        if src_path.is_symlink() or src_path.exists():
            # Already there. Don't try to recreate (mklink fails on existing).
            continue
        # Ensure parent exists
        src_path.parent.mkdir(exist_ok=True, parents=True)
        rc, out = cmd(
            ["cmd.exe", "/c", "mklink", "/J",
             f"{DOTNET_SHADOW_WIN}\\{sub}",
             f"{DOTNET_SYS_WIN}\\{target}"],
            timeout=30,
        )
        if rc != 0:
            log(f"  mklink /J failed for {sub}: {out.strip()}")
            return False

    # Physical copy of the WindowsDesktop.App pack — strip read-only attrs so
    # per-iter swap can overwrite.
    sys_pack = DOTNET_SYS / "shared" / "Microsoft.WindowsDesktop.App" / pack_ver
    if not sys_pack.is_dir():
        log(f"FATAL: system pack {sys_pack} not found")
        return False
    pack_dir.mkdir(exist_ok=True, parents=True)
    for f in sys_pack.iterdir():
        if f.is_file():
            dst = pack_dir / f.name
            shutil.copy2(f, dst)
            try:
                dst.chmod(0o644)
            except Exception:
                pass

    # Physical copy of dotnet.exe + auxiliary files at the shadow root.
    for name in ("dotnet.exe", "dnx.cmd", "LICENSE.txt", "ThirdPartyNotices.txt"):
        src = DOTNET_SYS / name
        if src.exists():
            shutil.copy2(src, DOTNET_SHADOW / name)

    sentinel.write_text(expected_sentinel)
    log(f"  shadow ready at {DOTNET_SHADOW}")
    return True


def shadow_pack_dir() -> Path | None:
    """Return the shadow's Microsoft.WindowsDesktop.App/<ver>/ dir."""
    ver = detect_wpf_pack_version()
    if ver is None:
        return None
    return DOTNET_SHADOW / "shared" / "Microsoft.WindowsDesktop.App" / ver


def bdn_env() -> dict:
    """Environment dict for BDN out-of-process runs.

    DOTNET_ROOT / DOTNET_ROOT_X64 point hostfxr at the shadow.
    DOTNET_MULTILEVEL_LOOKUP=0 prevents fallback to system C:\\Program Files\\dotnet.
    DOTNET_ReadyToRun=0 disables ReadyToRun so the host doesn't reject our
      pure-IL local builds for not matching the system pack's R2R metadata.
    PATH prepend ensures any indirect `dotnet` invocations also use the shadow.
    WPF_AR_EXPECTED_PACK_DIR is the path ShadowGuard.cs (in microbench/) checks
      against Assembly.Location at module load — fails the inner child loudly
      if the shadow override didn't take effect.

    WSLENV is the WSL → Windows env-var propagation whitelist. Without it,
    NONE of the variables we set here would reach cmd.exe (and therefore BDN).
    The colon-separated names list each var that should cross the bash-to-Windows
    boundary; we keep any pre-existing value (e.g. WT_SESSION) and append ours.
    Diagnosed 2026-05-09 after ShadowGuard kept tripping in BDN inner children
    despite microbench.py setting the var — the var was set on python's side,
    but WSL silently dropped everything except DOTNET_ROOT (and even that only
    worked for iter 062 because BDN's customDotNetCliPath makes the inner
    child use shadow's dotnet.exe regardless of the env).
    """
    env = os.environ.copy()
    env["DOTNET_ROOT"] = DOTNET_SHADOW_WIN
    env["DOTNET_ROOT_X64"] = DOTNET_SHADOW_WIN
    env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
    env["DOTNET_ReadyToRun"] = "0"
    env["PATH"] = DOTNET_SHADOW_WIN + os.pathsep + env.get("PATH", "")

    pack_dir = shadow_pack_dir()
    if pack_dir is not None:
        # Convert /c/foo/bar to C:\foo\bar for ShadowGuard's StartsWith match.
        env["WPF_AR_EXPECTED_PACK_DIR"] = to_winpath(pack_dir)

    propagated = [
        "DOTNET_ROOT", "DOTNET_ROOT_X64", "DOTNET_MULTILEVEL_LOOKUP",
        "DOTNET_ReadyToRun", "WPF_AR_EXPECTED_PACK_DIR",
    ]
    existing_wslenv = env.get("WSLENV", "")
    parts = [p for p in existing_wslenv.split(":") if p] if existing_wslenv else []
    for name in propagated:
        if name not in parts:
            parts.append(name)
    env["WSLENV"] = ":".join(parts)

    return env


def stage_assemblies(side: str) -> dict[str, Path] | None:
    """Copy each just-built <Name>.dll into a side-specific staging path.

    Returns a dict {name: staged_path} on success, or None if any expected DLL
    is missing.
    """
    STAGING.mkdir(exist_ok=True, parents=True)
    staged: dict[str, Path] = {}
    for asm in ASSEMBLIES:
        src = asm["build_dir"] / f"{asm['name']}.dll"
        if not src.exists():
            log(f"  cannot stage {asm['name']}.{side}: source missing at {src}")
            return None
        dst = STAGING / f"{asm['name']}.{side}.dll"
        shutil.copy2(src, dst)
        staged[asm["name"]] = dst
    return staged


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


def swap_assemblies_into_publish(staged: dict[str, Path]) -> bool:
    """Overwrite each <Name>.dll in BOTH the publish dir and the shadow pack.

    The publish-dir swap keeps the OUTER Microbenchmarks.exe consistent with
    our build (it loads its WPF DLLs from the publish dir because the outer
    project is published self-contained). The shadow swap is the one that
    actually matters — BDN's inner child processes load from there via
    DOTNET_ROOT (see setup_dotnet_shadow / bdn_env).

    A missing publish-dir target is fatal — silently skipping would leave the
    system runtime pack version in place. The shadow target must exist (it
    was copied during setup_dotnet_shadow); a missing shadow target indicates
    the shadow is broken and is also fatal.
    """
    pack_dir = shadow_pack_dir()
    if pack_dir is None or not pack_dir.is_dir():
        log("  shadow pack dir missing — setup_dotnet_shadow not called?")
        return False

    for name, src in staged.items():
        publish_target = MICROBENCH_PUBLISH / f"{name}.dll"
        shadow_target = pack_dir / f"{name}.dll"
        if not publish_target.exists():
            log(f"  publish target {publish_target} missing — publish did not produce expected layout")
            return False
        if not shadow_target.exists():
            log(f"  shadow target {shadow_target} missing — broken shadow setup")
            return False
        shutil.copy2(src, publish_target)
        shutil.copy2(src, shadow_target)
    return True


def clear_bdn_artifacts() -> None:
    if MICROBENCH_RESULTS.exists():
        shutil.rmtree(MICROBENCH_RESULTS, ignore_errors=True)


def run_bdn(filter_pattern: str) -> tuple[int, str]:
    log(f"  running BDN with --filter '{filter_pattern}' (DOTNET_ROOT shadow) …")
    return cmd(
        ["cmd.exe", "/c", to_winpath(MICROBENCH_EXE),
         "--filter", filter_pattern,
         "--exporters", "json"],
        cwd=MICROBENCH_PUBLISH, timeout=BENCH_TIMEOUT_S,
        env=bdn_env(),
    )


def parse_bdn_results() -> list[dict] | None:
    """Read every *-report-full.json in results dir; return the Benchmarks list flat.

    Treats benchmarks with null/missing Statistics as un-run (BDN writes them
    when the inner child crashes on module load — e.g., ShadowGuard tripping).
    Returns None if NO benchmarks have valid stats so the caller routes to the
    BENCH-FAIL exit path instead of crashing in decide() on float(None).
    """
    if not MICROBENCH_RESULTS.exists():
        return None
    out: list[dict] = []
    for f in sorted(MICROBENCH_RESULTS.glob("*-report-full.json")):
        try:
            data = json.loads(f.read_text())
            for b in data.get("Benchmarks", []):
                stats = b.get("Statistics")
                if stats is None or not isinstance(stats, dict) or "Mean" not in stats:
                    log(f"  skipping {b.get('FullName','?')}: no Statistics (inner child crashed?)")
                    continue
                out.append({
                    "fullName": b["FullName"],
                    "stats": stats,
                    "memory": b.get("Memory") or {},
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


# ─── Halt threshold ───────────────────────────────────────────────────────────


def check_halt_threshold() -> bool:
    """Return True iff the last HALT_UNCLEAR_THRESHOLD tier-B rows are all REJECT-UNCLEAR.

    Reads results.jsonl line by line, filters to tier=="B" rows, takes the
    tail of HALT_UNCLEAR_THRESHOLD rows, and returns True only when:
      - the tail length exactly equals HALT_UNCLEAR_THRESHOLD, AND
      - every row in that tail has verdict == "REJECT-UNCLEAR".

    Returns False whenever fewer than HALT_UNCLEAR_THRESHOLD tier-B rows exist
    so the loop is never halted prematurely on a fresh results file.
    """
    if not RESULTS_JSONL.exists():
        return False
    tier_b_rows: list[dict] = []
    with open(RESULTS_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("tier") == "B":
                tier_b_rows.append(row)
    tail = tier_b_rows[-HALT_UNCLEAR_THRESHOLD:]
    if len(tail) < HALT_UNCLEAR_THRESHOLD:
        return False
    return all(r.get("verdict") == "REJECT-UNCLEAR" for r in tail)


def write_halt_sentinel(tail_rows: list[dict]) -> None:
    """Write a plain-text HALT sentinel file to HALT_FILE.

    Format follows the design doc §2 "Sentinel file format": a HEAD line,
    written-at timestamp, last-N tier-B rows summary, possible causes, and
    recovery instructions.
    """
    written_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines: list[str] = [
        f"HALT: WPF autoresearch loop stopped — {HALT_UNCLEAR_THRESHOLD} consecutive REJECT-UNCLEAR across all paths.",
        f"Written: {written_at}",
        f"Last {HALT_UNCLEAR_THRESHOLD} tier-B rows:",
    ]
    for r in tail_rows:
        ts = r.get("ts", "?")
        filt = r.get("filter", "?")
        verdict = r.get("verdict", "?")
        bench = r.get("bench_name", "?")
        lines.append(f"  [{ts}] {filt}  {verdict}  {bench}")
    lines += [
        "Possible causes:",
        "  1. All easy wins on covered paths are exhausted — the benchmark-author pass needs to",
        "     cover new hot paths from profile.json.",
        "  2. The benchmark noise floor is too high — BDN iteration count may need tuning.",
        "  3. The profiler data in profile.json is stale — re-run Tier A.",
        "Recovery:",
        "  - Delete this file to allow the loop to resume.",
        "  - Add a NOTE to program.md explaining what changed (new benchmarks, new profile, etc.).",
        "  - Increase WPF_AR_HALT_UNCLEAR_THRESHOLD if you want a longer patience window.",
    ]
    HALT_FILE.write_text("\n".join(lines) + "\n")


# ─── Cooldown snapshot ───────────────────────────────────────────────────────


def compute_cooldown_state() -> dict:
    """Compute per-filter cooldown state from results.jsonl.

    Mirrors the algorithm in tools/cool-list.py so the JSON snapshot agrees
    with the standalone helper.  The two scripts are intentionally independent
    (no shared import).

    Algorithm (design doc §1 "Query algorithm"):
    1. Read all tier-B rows from results.jsonl.
    2. For each unique filter, find the two most recent rows.
    3. If both are REJECT-UNCLEAR AND fewer than 5 tier-B rows have been written
       since the second-most-recent REJECT-UNCLEAR, that filter is COOLED.

    Returns a dict with schema:
      {
        "computed_at": "<UTC ISO timestamp>",
        "cool_filters": [
          { "filter": "...", "cooled_at_row": int, "rows_since": int,
            "eligible_after_row": int }
        ],
        "all_filters": [<list of all unique filter strings ever seen>]
      }
    """
    cooldown_window = 5  # tier-B rows; matches design doc

    tier_b_rows: list[dict] = []
    if RESULTS_JSONL.exists():
        with open(RESULTS_JSONL, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("tier") == "B":
                    tier_b_rows.append(row)

    total_b = len(tier_b_rows)

    # Group by filter: list of (tier_b_index, verdict) in file order
    filter_indices: dict[str, list[tuple[int, str]]] = {}
    for i, row in enumerate(tier_b_rows):
        filt = row.get("filter", "<no-filter>")
        verdict = row.get("verdict", "<unknown>")
        filter_indices.setdefault(filt, []).append((i, verdict))

    cool_filters: list[dict] = []
    for filt, entries in filter_indices.items():
        if len(entries) < 2:
            continue
        idx_second_last, v_second_last = entries[-2]
        idx_last, v_last = entries[-1]
        if v_last == "REJECT-UNCLEAR" and v_second_last == "REJECT-UNCLEAR":
            # Count tier-B rows written AFTER the second-most-recent REJECT-UNCLEAR
            rows_since = total_b - 1 - idx_second_last
            if rows_since < cooldown_window:
                eligible_after_row = idx_second_last + cooldown_window
                cool_filters.append({
                    "filter": filt,
                    "cooled_at_row": idx_second_last,
                    "rows_since": rows_since,
                    "eligible_after_row": eligible_after_row,
                })

    # Sort cool_filters by cooled_at_row for deterministic output
    cool_filters.sort(key=lambda x: x["cooled_at_row"])

    return {
        "computed_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "cool_filters": cool_filters,
        "all_filters": sorted(filter_indices.keys()),
    }


def write_cooldown_snapshot() -> None:
    """Write the current cooldown state to cooldown.json.

    This is diagnostic data only — inner Claude does NOT read this file.
    Failure to write is non-fatal: we log a warning and continue.
    """
    try:
        state = compute_cooldown_state()
        COOLDOWN_JSON.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        log(f"cooldown snapshot written → {COOLDOWN_JSON.name}"
            f" ({len(state['cool_filters'])} cooled, {len(state['all_filters'])} total filters)")
    except Exception as exc:
        log(f"WARNING: failed to write cooldown snapshot ({COOLDOWN_JSON}): {exc}")


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

    if not setup_dotnet_shadow():
        log("FATAL: failed to set up DOTNET_ROOT shadow. Out-of-process BDN cannot run.")
        return 4

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
    log(f"Phase 1: build baseline assemblies ({', '.join(a['name'] for a in ASSEMBLIES)})")
    rc, _ = git("checkout", "--quiet", base_sha)
    if rc != 0:
        log("FATAL: could not checkout HEAD~1")
        return 3
    try:
        if not build_assemblies():
            log("FATAL: baseline build failed")
            return 3
        baseline_staged = stage_assemblies("baseline")
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
        candidate_staged = stage_assemblies("candidate")
        if candidate_staged is None:
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
    if not swap_assemblies_into_publish(baseline_staged):
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
    if not swap_assemblies_into_publish(candidate_staged):
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

    # ── Halt threshold check ────────────────────────────────────────────────
    if check_halt_threshold():
        # Re-read the tail rows for the sentinel summary (they are now on disk).
        tier_b_tail: list[dict] = []
        with open(RESULTS_JSONL) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("tier") == "B":
                    tier_b_tail.append(r)
        tail_rows = tier_b_tail[-HALT_UNCLEAR_THRESHOLD:]
        write_halt_sentinel(tail_rows)
        print(
            f"[microbench] HALT: {HALT_UNCLEAR_THRESHOLD} consecutive REJECT-UNCLEAR"
            f" — writing HALT sentinel ({HALT_FILE})",
            file=sys.stderr,
        )
        write_cooldown_snapshot()
        return 7

    # ── Cooldown snapshot (diagnostic; always written) ──────────────────────
    write_cooldown_snapshot()

    return rc_out


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
