#!/usr/bin/env python3
"""WPF Autoresearch evaluation harness.

Run from /c/work/wpf-perf/autoresearch/ after the agent has committed a
change to /c/work/wpf-perf/. Builds WPF, swaps the produced DLLs into
MotionCatalyst's BUILD output, runs the spike-9 scenario REPS times,
aggregates per-metric medians + std-dev, applies a strict Pareto gate,
and decides KEEP / REVERT / REJECT-PARETO.

Logs:
- results.tsv   one row per iteration: iter, ts, sha, decision, z, medians
- results.jsonl one JSON line per iteration with per-rep detail (for plots)

Exit codes:
  0  KEEP            — your commit stays
  1  REVERT          — composite did not improve; eval did `git reset --hard HEAD^`
  2  REJECT-PARETO   — composite improved but a metric regressed > 3%; reverted
  3  BUILD-FAIL      — wpf build failed; reverted
  4  SPIKE-FAIL      — spike run crashed/timed out; reverted
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.resolve()
WPF_REPO = Path("/c/work/wpf-perf")
PC_BUILD_OUT = WPF_REPO / "artifacts" / "bin" / "PresentationCore" / "x64" / "Release" / "net10.0"
FORK_STAGE = WPF_REPO / "artifacts" / "fork-swap-out"
SWAP_TOOL = WPF_REPO / "tools" / "swap-wpf" / "swap-wpf.ps1"
BUILD_PC_SCRIPT = WPF_REPO / "build-pc-perf.ps1"
MC_BUILD = Path("/c/work/desktop/wpf-test/src/motioncatalyst/BUILD/x64_Debug")
SPIKE = Path("/c/work/wpf-perf/tools/poc/spike-9-play-take.py")

RESULTS_TSV = ROOT / "results.tsv"
RESULTS_JSONL = ROOT / "results.jsonl"
BASELINE_PATH = ROOT / "baseline.json"

REPS = int(os.environ.get("WPF_AR_REPS", "5"))
PARETO_THRESHOLD = float(os.environ.get("WPF_AR_PARETO", "0.03"))  # 3 %
SPIKE_TIMEOUT_S = int(os.environ.get("WPF_AR_SPIKE_TIMEOUT", "300"))
BUILD_TIMEOUT_S = int(os.environ.get("WPF_AR_BUILD_TIMEOUT", "600"))
# Scenario-completion gate. Healthy reps capture ~10,500 frames; reps that
# terminate playback early drop to <5,000 frames and produce noisy steady-state
# stats that swamp the Pareto signal. Set to 0 to disable.
MIN_RENDER_PASSES = int(os.environ.get("WPF_AR_MIN_PASSES", "8000"))
MAX_REP_ATTEMPTS = int(os.environ.get("WPF_AR_MAX_REP_ATTEMPTS", "3"))

# Metric extraction: each entry is (key) → (dotted path into analysis.json).
# Steady-state metrics drop the first 30 frames (warmup, JIT, layout cascade,
# VBlank channel registration race) and reflect user-perceived smoothness in
# the DWM-locked ~60 Hz regime that real users actually experience. Empirical
# CV across reps: 1–6 %, vs >50 % for the old whole-rep frame_p95_ms.
METRICS = {
    "alloc_bytes":          "gc.totalAllocBytes",
    "gc_max_pause_ms":      "gc.maxPauseTimeMs",
    "ss_p50_ms":            "wpf.steadyState.renderFrameP50Ms",
    "ss_p95_ms":            "wpf.steadyState.renderFrameP95Ms",
    "ss_p99_ms":            "wpf.steadyState.renderFrameP99Ms",
    "missed_16_count":      "wpf.steadyState.missedFrames16Count",
    "missed_50_count":      "wpf.steadyState.missedFrames50Count",
}
WEIGHTS = {
    "alloc_bytes":      0.30,
    "gc_max_pause_ms":  0.10,
    "ss_p50_ms":        0.10,
    "ss_p95_ms":        0.20,
    "ss_p99_ms":        0.15,
    "missed_16_count":  0.10,
    "missed_50_count":  0.05,
}

# ─── Helpers ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[eval] {msg}", flush=True)


def cmd(argv: list[str], cwd: Path | None = None, timeout: int | None = None,
        env: dict | None = None) -> tuple[int, str]:
    """Run a subprocess; return (rc, combined stdout+stderr)."""
    p = subprocess.run(
        argv, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def git_sha(repo: Path) -> str:
    rc, out = cmd(["git", "rev-parse", "HEAD"], cwd=repo)
    return out.strip() if rc == 0 else "?"


def iter_count() -> int:
    if not RESULTS_TSV.exists():
        return 0
    return max(0, sum(1 for _ in RESULTS_TSV.open()) - 1)  # minus header


def last_kept_z() -> float:
    """Return z of the most recent KEEP decision, or +inf if none."""
    if not RESULTS_TSV.exists():
        return float("inf")
    last = None
    for line in RESULTS_TSV.open():
        cols = line.rstrip("\n").split("\t")
        if len(cols) >= 5 and cols[3] == "KEEP":
            try:
                last = float(cols[4])
            except ValueError:
                pass
    return last if last is not None else float("inf")


# ─── Build + DLL swap ────────────────────────────────────────────────────────
#
# Phase 1 only rebuilds PresentationCore.dll (the known hot-path target).
# Other 3 swap DLLs (PresentationFramework / System.Xaml / WindowsBase) keep
# whatever's in fork-swap-out/ — the agent's edits to those projects would not
# be picked up. Extending to the full build is a follow-up if the agent needs
# to attack those subsystems.


def cleanup_orphan_processes(spike_stdout: str = "") -> None:
    """Kill orphans the timed-out spike couldn't clean up itself.

    Hard rule (CLAUDE.md): NEVER kill MotionCatalyst.exe by image name —
    that would kill the user's session. Instead:
      - UiMcpHost is MC-internal; users don't run it. Killing by image is OK.
      - For MotionCatalyst, parse the PID the spike printed via its cleanup
        log line (`[cleanup] taskkill /F /PID {pid}` or similar) and kill
        only that. If we can't identify our PID, log and bail — leave it for
        the user to triage.
    """
    # 1) Broker is always safe to kill by image — only the autoresearch loop
    #    spawns it on this machine.
    subprocess.run(
        ["cmd.exe", "/c", "taskkill", "/F", "/IM", "UiMcpHost.exe", "/T"],
        capture_output=True, text=True, timeout=15,
    )
    # 1b) MotionCatalyst-cli.exe (NOT the GUI MotionCatalyst.exe) is the CLI
    #     variant the spike spawns. Users run the GUI; nobody runs the CLI
    #     interactively — so killing by image is safe and reliable. This
    #     covers crashes where the spike never logged a [mc-pid] marker.
    subprocess.run(
        ["cmd.exe", "/c", "taskkill", "/F", "/IM", "MotionCatalyst-cli.exe", "/T"],
        capture_output=True, text=True, timeout=15,
    )
    # 2) MC: only kill the PID the spike tracked, parsed from its captured stdout.
    import re
    pids = set()
    for m in re.finditer(r"taskkill\s+/F\s+/PID\s+(\d+)", spike_stdout):
        pids.add(m.group(1))
    for m in re.finditer(r"\[mc-pid\]\s+(\d+)", spike_stdout):  # if spike adds an explicit marker
        pids.add(m.group(1))
    for pid in pids:
        subprocess.run(
            ["cmd.exe", "/c", "taskkill", "/F", "/PID", pid],
            capture_output=True, text=True, timeout=15,
        )
    if pids:
        log(f"  (killed orphan MC PIDs from spike log: {sorted(pids)})")
    else:
        log("  (cleaned UiMcpHost; could not identify spike's MC PID — check tasklist)")


def to_winpath(p: Path) -> str:
    """Convert /c/foo/bar → C:\\foo\\bar for PowerShell args."""
    s = str(p)
    if s.startswith("/c/"):
        return "C:\\" + s[3:].replace("/", "\\")
    return s


def build_presentation_core() -> bool:
    """Run build-pc-perf.ps1 to rebuild PresentationCore.dll. ~1-2 min."""
    log("building PresentationCore (Release, x64) …")
    rc, out = cmd(
        [
            "cmd.exe", "/c",
            "powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
            "-File", to_winpath(BUILD_PC_SCRIPT),
        ],
        cwd=WPF_REPO, timeout=BUILD_TIMEOUT_S,
    )
    if rc != 0:
        log(f"build failed (rc={rc}). Last 30 lines:")
        for line in out.splitlines()[-30:]:
            print("  " + line)
        return False

    # Stage rebuilt PresentationCore.{dll,pdb} into fork-swap-out where
    # swap-wpf.ps1 expects them.
    FORK_STAGE.mkdir(parents=True, exist_ok=True)
    for ext in ("dll", "pdb"):
        src = PC_BUILD_OUT / f"PresentationCore.{ext}"
        dst = FORK_STAGE / f"PresentationCore.{ext}"
        if not src.is_file():
            log(f"FATAL: build output missing: {src}")
            return False
        shutil.copy2(src, dst)
    log(f"staged PresentationCore.dll/pdb → {FORK_STAGE}")
    return True


def do_swap() -> bool:
    """Use swap-wpf.ps1 -Force to install the staged fork DLLs into MC's build.

    Pre-sweep orphan MC-cli/UiMcpHost processes that may be holding handles
    on the target DLLs from a prior crashed iter — otherwise swap-wpf fails
    with 'file is being used by another process' and the iter BUILD-FAILs.
    """
    cleanup_orphan_processes("")
    log("swap-wpf swap -Force …")
    rc, out = cmd(
        [
            "cmd.exe", "/c",
            "powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
            "-File", to_winpath(SWAP_TOOL),
            "swap",
            "-ForkOutputDir", to_winpath(FORK_STAGE),
            "-McBuildDir", to_winpath(MC_BUILD),
            "-Force",
        ],
        cwd=WPF_REPO, timeout=120,
    )
    if rc != 0:
        log(f"swap failed (rc={rc}). Last 20 lines:")
        for line in out.splitlines()[-20:]:
            print("  " + line)
        return False
    return True


def do_restore() -> None:
    """Always-safe restore using swap-wpf.ps1 manifest. Retries on DLL lock —
    OS file-handle release after taskkill is non-instant."""
    log("swap-wpf restore …")
    last_out = ""
    for attempt in range(4):
        if attempt > 0:
            wait_s = 2 ** attempt  # 2, 4, 8 s
            log(f"  restore: DLL still locked, waiting {wait_s}s before retry …")
            time.sleep(wait_s)
        rc, out = cmd(
            [
                "cmd.exe", "/c",
                "powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
                "-File", to_winpath(SWAP_TOOL),
                "restore",
                "-McBuildDir", to_winpath(MC_BUILD),
            ],
            cwd=WPF_REPO, timeout=120,
        )
        last_out = out
        if rc == 0:
            return
        # Only retry on the "file in use" failure pattern
        if "is being used by another process" not in out:
            break
    log(f"WARN: restore failed after retries. Last 15 lines:")
    for line in last_out.splitlines()[-15:]:
        print("  " + line)


# ─── Spike runner ────────────────────────────────────────────────────────────


def _run_spike_once(rep: int, iter_num: int, attempt: int) -> dict | None:
    """One spike attempt. Returns metrics dict on success, None on failure."""
    suffix = f"rep{rep}" if attempt == 0 else f"rep{rep}-retry{attempt}"
    name = f"ar-iter{iter_num:04d}-{suffix}"
    out_dir = Path(f"/c/work/wpf-perf-{name}")
    if out_dir.is_dir():
        shutil.rmtree(out_dir)

    env = os.environ.copy()
    env["SPIKE9_NAME"] = name

    label = f"rep {rep}" if attempt == 0 else f"rep {rep} retry{attempt}"
    log(f"  {label}: launching spike (out={out_dir.name}) …")
    try:
        rc, out = cmd(
            ["python3", str(SPIKE)],
            timeout=SPIKE_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired as ex:
        log(f"  {label}: TIMEOUT after {SPIKE_TIMEOUT_S}s")
        partial_stdout = ex.stdout.decode(errors="replace") if ex.stdout else ""
        cleanup_orphan_processes(partial_stdout)
        return None

    if rc != 0:
        # Spike emits explicit non-zero rc on play-verify failure (17),
        # broker disconnect, etc. Treat as a failed rep and let the caller retry.
        # Also: a crashed spike often leaves an MC-cli (and broker) running
        # with file handles on the swapped DLLs — the next swap then BUILD-FAILs.
        # Sweep orphans before returning so the next attempt can swap cleanly.
        log(f"  {label}: spike exit rc={rc} — rejecting rep")
        cleanup_orphan_processes(out or "")
        return None

    analysis = out_dir / "analysis.json"
    if not analysis.is_file() or analysis.stat().st_size == 0:
        log(f"  {label}: no analysis.json at {analysis}")
        return None

    try:
        data = json.loads(analysis.read_text())
    except json.JSONDecodeError as e:
        log(f"  {label}: analysis.json parse error: {e}")
        return None

    # Scenario-integrity floor. Healthy reps allocate 600+ MB during the
    # 10-second playback (video frame buffers, DynamicData updates, etc.).
    # Reps where the take never opened or playback failed silently land at
    # 200–400 MB with degraded p99 — those are not real measurements and
    # must not pollute the baseline. 400 MB is comfortably below the
    # observed healthy floor and well above the failure-mode ceiling.
    alloc_floor = float(os.environ.get("WPF_AR_ALLOC_FLOOR", "400000000"))
    alloc = data.get("gc", {}).get("totalAllocBytes", 0)
    if alloc < alloc_floor:
        log(f"  {label}: rejected (alloc_bytes={alloc:,} < {alloc_floor:,.0f} "
            "— scenario integrity)")
        return None

    # Scenario-completion gate. Healthy reps capture ~10,500 frames during the
    # full playback. Reps that terminate early (playback aborted, take loop
    # ended prematurely) drop to 1,000–5,000 frames with degraded steady-state
    # stats. 8,000 is comfortably below the healthy floor and well above the
    # truncated-rep ceiling. Set WPF_AR_MIN_PASSES=0 to disable.
    if MIN_RENDER_PASSES > 0:
        render_pass_count = data.get("wpf", {}).get("renderPassCount", 0)
        if render_pass_count < MIN_RENDER_PASSES:
            log(
                f"  {label}: rejected (renderPassCount={render_pass_count} "
                f"< {MIN_RENDER_PASSES} — scenario truncated)"
            )
            return None

    extracted = {}
    for key, path in METRICS.items():
        try:
            v: object = data
            for part in path.split("."):
                v = v[part]  # type: ignore[index]
            if not isinstance(v, (int, float)):
                raise TypeError(f"non-numeric at {path}: {v!r}")
            extracted[key] = float(v)
        except (KeyError, TypeError) as e:
            log(f"  {label}: metric {key} ({path}) extraction failed: {e}")
            return None

    log(f"  {label}: " + ", ".join(f"{k}={v:.3f}" for k, v in extracted.items()))
    return extracted


def run_spike(rep: int, iter_num: int) -> dict | None:
    """Run spike-9 with up to MAX_REP_ATTEMPTS attempts. Returns metrics or None."""
    for attempt in range(MAX_REP_ATTEMPTS):
        m = _run_spike_once(rep, iter_num, attempt=attempt)
        if m is not None:
            return m
        if attempt + 1 < MAX_REP_ATTEMPTS:
            log(f"  rep {rep}: attempt {attempt + 1} failed; retrying …")
    return None


# ─── Aggregation + scoring ───────────────────────────────────────────────────


def aggregate(per_rep: list[dict]) -> tuple[dict, dict]:
    """Return (medians, stds) over the per-rep dicts."""
    medians, stds = {}, {}
    for k in METRICS:
        xs = [r[k] for r in per_rep]
        medians[k] = statistics.median(xs)
        stds[k] = statistics.stdev(xs) if len(xs) > 1 else 0.0
    return medians, stds


def composite_z(medians: dict, baseline: dict) -> tuple[float, dict]:
    """Composite z-score and per-metric component contributions."""
    components = {}
    z = 0.0
    for k, w in WEIGHTS.items():
        b_med = baseline["medians"][k]
        b_std = max(baseline["stds"][k], 1e-9)  # avoid div-by-zero
        component = (medians[k] - b_med) / b_std
        components[k] = component
        z += w * component
    return z, components


def pareto_violations(medians: dict, baseline: dict) -> list[str]:
    """Metrics that regressed by more than PARETO_THRESHOLD vs baseline median."""
    bad = []
    for k in METRICS:
        b_med = baseline["medians"][k]
        if b_med <= 0:
            continue
        delta_pct = (medians[k] - b_med) / b_med
        if delta_pct > PARETO_THRESHOLD:
            bad.append(f"{k}=+{delta_pct*100:.1f}%")
    return bad


# ─── Logging + decision ──────────────────────────────────────────────────────


def write_logs(payload: dict) -> None:
    with RESULTS_JSONL.open("a") as f:
        f.write(json.dumps(payload) + "\n")

    if not RESULTS_TSV.exists():
        RESULTS_TSV.write_text(
            "iter\tts\tsha\tdecision\tz\t"
            + "\t".join(METRICS.keys()) + "\n"
        )
    cols = [
        str(payload["iter"]),
        payload["ts"],
        payload["git_sha"][:8],
        payload["decision"],
        f"{payload.get('z', float('nan')):.4f}",
    ]
    medians = payload.get("medians", {})
    for k in METRICS:
        v = medians.get(k)
        cols.append(f"{v:.3f}" if isinstance(v, (int, float)) else "-")
    with RESULTS_TSV.open("a") as f:
        f.write("\t".join(cols) + "\n")


def decide_and_revert(decision: str, payload: dict) -> int:
    """Write logs and (if applicable) revert the agent's last commit."""
    payload["decision"] = decision
    write_logs(payload)

    log(f"DECISION: {decision} — {payload.get('reason', '')}")

    exit_codes = {"KEEP": 0, "REVERT": 1, "REJECT-PARETO": 2,
                  "BUILD-FAIL": 3, "SPIKE-FAIL": 4}
    rc = exit_codes[decision]

    if rc != 0:
        iter_sha = payload.get("git_sha")
        if iter_sha:
            # Use `git revert` (creates a new commit) rather than `git reset
            # --hard` so any orchestrator commits stacked on top of the iter
            # are preserved. With reset --hard, an orchestrator commit ends
            # up dangling in reflog and the fix it carried is lost.
            env = os.environ.copy()
            env.setdefault("GIT_AUTHOR_NAME", "wpf-ar-eval")
            env.setdefault("GIT_AUTHOR_EMAIL", "eval@autoresearch.local")
            env.setdefault("GIT_COMMITTER_NAME", "wpf-ar-eval")
            env.setdefault("GIT_COMMITTER_EMAIL", "eval@autoresearch.local")
            log(f"reverting iter commit {iter_sha[:8]} in {WPF_REPO} via git revert")
            r = subprocess.run(
                ["git", "revert", "--no-edit", iter_sha],
                cwd=str(WPF_REPO), env=env, capture_output=True, text=True,
            )
            if r.returncode != 0:
                # Conflict — orchestrator commit edited the same file. Abort
                # the revert and fall back to reset --hard <iter_sha>^.
                log(f"WARN: git revert failed: {r.stderr.strip()[:200]}")
                subprocess.run(["git", "revert", "--abort"],
                               cwd=str(WPF_REPO), check=False)
                subprocess.run(
                    ["git", "reset", "--hard", f"{iter_sha}^"],
                    cwd=str(WPF_REPO), check=False,
                )
        else:
            log(f"WARN: no git_sha in payload; reverting HEAD^ in {WPF_REPO}")
            subprocess.run(
                ["git", "reset", "--hard", "HEAD^"],
                cwd=str(WPF_REPO), check=False,
            )
    return rc


# ─── Main ────────────────────────────────────────────────────────────────────


def is_session_connected() -> bool:
    """Return True iff the user's interactive session is connected (has a
    physical or RDP display attached). WPF's D3D9 video preview fails with
    D3DERR_NOTAVAILABLE in disconnected sessions, sending MC into the
    CPU-rendered InteropBitmap fallback — every spike then truncates to
    <1000 frames and SPIKE-FAILs the 8000 floor. Without this guard ralph
    burns API quota on guaranteed-fail iters until the user reconnects.

    `query session` output: STATE column reads 'Active' / 'Conn' / 'Disc'.
    We require Active or Conn for the user-named session that owns this
    process tree. Anything else (no session, query failure) → assume bad
    and let ralph back off.
    """
    try:
        rc, out = cmd(
            ["cmd.exe", "/c", "query", "session"],
            timeout=15,
        )
    except Exception:
        return False
    if rc != 0:
        return False
    user = os.environ.get("USER", os.environ.get("USERNAME", "")).lower()
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        # query session output is positional; USERNAME may be missing on disc
        # rows, so search for the user token if present, otherwise rely on
        # state token.
        joined = line.lower()
        if user and user in joined:
            if " active " in f" {joined} " or "active" in cols:
                return True
            if " conn " in f" {joined} " or "conn" in cols:
                return True
            return False
    # Fallback: if we see any 'Active' / 'Conn' state at all, assume good.
    return any(s in out for s in (" Active ", " Conn "))


def start_display_keepalive() -> subprocess.Popen | None:
    """Spawn the keep-display-awake.ps1 helper. WPF's D3D9 video preview path
    fails with D3DERR_INVALIDCALL when monitors are asleep, sending MC into
    InteropBitmap (CPU) fallback — the spike then captures <1000 frames per
    rep instead of >10000 and SPIKE-FAILs on the 8000 floor. Holding
    ES_DISPLAY_REQUIRED for the duration of eval keeps D3D9 working overnight.
    """
    helper = ROOT / "keep-display-awake.ps1"
    if not helper.is_file():
        log("WARN: keep-display-awake.ps1 not found; D3D9 may fail if display sleeps")
        return None
    try:
        p = subprocess.Popen(
            ["cmd.exe", "/c", "powershell", "-ExecutionPolicy", "Bypass",
             "-NoProfile", "-File", to_winpath(helper)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log(f"display keepalive started (PID {p.pid})")
        return p
    except Exception as e:
        log(f"WARN: failed to start display keepalive: {e}")
        return None


def stop_display_keepalive(p: subprocess.Popen | None) -> None:
    if p is None:
        return
    try:
        p.kill()
        p.wait(timeout=5)
        log("display keepalive stopped")
    except Exception as e:
        log(f"WARN: failed to stop display keepalive: {e}")


def main() -> int:
    if not BASELINE_PATH.is_file():
        log(f"FATAL: baseline missing at {BASELINE_PATH}. Run bootstrap.py first.")
        return 5
    baseline = json.loads(BASELINE_PATH.read_text())

    # Bail out early if the user's session is disconnected — D3D9 is broken
    # in that state and any iter would SPIKE-FAIL on scenario truncation.
    # Returning fast lets ralph.sh's backoff put the loop to sleep until the
    # user reconnects (or the system comes back). NEVER spends quota on a
    # guaranteed-fail iter.
    if not is_session_connected():
        log("FATAL: user session disconnected — D3D9 unavailable. "
            "Skipping iter; ralph.sh will back off.")
        return 6

    iter_num = iter_count() + 1
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sha = git_sha(WPF_REPO)
    base_payload = {"iter": iter_num, "ts": ts, "git_sha": sha}

    log(f"=== iter {iter_num} sha={sha[:8]} ===")

    keepalive = start_display_keepalive()

    try:
        if not build_presentation_core():
            return decide_and_revert("BUILD-FAIL", {**base_payload, "reason": "wpf build failed"})

        if not do_swap():
            return decide_and_revert("BUILD-FAIL", {**base_payload, "reason": "swap-wpf failed"})

        per_rep: list[dict] = []
        try:
            for i in range(REPS):
                m = run_spike(rep=i, iter_num=iter_num)
                if m is None:
                    return decide_and_revert(
                        "SPIKE-FAIL",
                        {**base_payload, "reason": f"rep {i} failed",
                         "per_rep": per_rep},
                    )
                per_rep.append(m)
        finally:
            do_restore()
    finally:
        stop_display_keepalive(keepalive)

    medians, stds = aggregate(per_rep)
    z, components = composite_z(medians, baseline)
    last_z = last_kept_z()
    bad = pareto_violations(medians, baseline)

    payload = {
        **base_payload,
        "z": z,
        "z_components": components,
        "medians": medians,
        "stds": stds,
        "per_rep": per_rep,
        "last_kept_z": last_z if last_z != float("inf") else None,
        "pareto_violations": bad,
    }

    if bad:
        payload["reason"] = f"pareto violations: {', '.join(bad)}"
        return decide_and_revert("REJECT-PARETO", payload)

    if last_z == float("inf"):
        # First iteration after baseline — accept any improvement vs baseline z=0.
        if z >= 0:
            payload["reason"] = f"z={z:.4f} ≥ baseline (0.0); not an improvement"
            return decide_and_revert("REVERT", payload)
    else:
        if z >= last_z:
            payload["reason"] = f"z={z:.4f} ≥ last_kept_z={last_z:.4f}"
            return decide_and_revert("REVERT", payload)

    payload["reason"] = f"composite improved: z={z:.4f}"
    return decide_and_revert("KEEP", payload)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
