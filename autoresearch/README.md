# WPF Autoresearch

Karpathy-style single-agent Ralph loop applied to WPF performance optimisation
in MotionCatalyst's spike-9 playback scenario. The loop edits WPF source, builds,
runs the scenario N=5 times, scores it against a captured baseline with a
strict Pareto gate, and accepts or reverts.

## Files

| File              | Purpose                                                           |
|-------------------|-------------------------------------------------------------------|
| `program.md`      | The persistent prompt (re-read each iteration).                  |
| `eval.py`         | Build → swap DLLs → run spike N times → aggregate → decide.      |
| `bootstrap.py`    | Capture `baseline.json` from a clean WPF build before looping.   |
| `ralph.sh`        | The loop: `cat program.md | claude --dangerously-skip-permissions`. |
| `plot.py`         | Render `plot.svg` from `results.jsonl`.                          |
| `baseline.json`   | (generated) baseline medians + std for normalisation.            |
| `results.tsv`     | (generated) one row per iteration: iter, ts, sha, decision, z, medians. |
| `results.jsonl`   | (generated) full per-rep detail per iteration (for plots).      |

## Getting started

1. **Verify paths in `eval.py`** match your tree. Especially:
   - `WPF_REPO` (default `/c/work/wpf-perf`)
   - `WPF_BUILD_OUT` (the artifacts/lib/net10.0 path)
   - `MC_BUILD` (MotionCatalyst BUILD/x64_Release — production-true config)
   - `SPIKE` (path to spike-9-play-take.py)

2. **Capture baseline** (~10 minutes):
   ```bash
   cd /c/work/wpf-perf/autoresearch
   python3 bootstrap.py
   ```
   This builds vanilla WPF, runs the spike 10 times, writes `baseline.json`.
   Inspect the printed CV column — any metric with CV > 10% is too noisy to
   optimise without raising `WPF_AR_BOOTSTRAP_REPS` or pinning the workload.

3. **Start the loop:**
   ```bash
   ./ralph.sh 100      # 100 iterations max; Ctrl-C to stop earlier
   ```

4. **Watch progress:**
   ```bash
   tail -f results.tsv
   python3 plot.py     # generate plot.svg from results.jsonl
   ```

5. **Stop manually** by adding `<halt/>` anywhere in `program.md` — `ralph.sh`
   checks for the sentinel between iterations.

## Tuning knobs (env vars read by `eval.py`)

| Env var                    | Default | Meaning                                    |
|----------------------------|---------|--------------------------------------------|
| `WPF_AR_REPS`              | 5       | spike runs per evaluation                  |
| `WPF_AR_PARETO`            | 0.03    | per-metric regression threshold (3%)       |
| `WPF_AR_SPIKE_TIMEOUT`     | 180     | spike wall-clock timeout, seconds          |
| `WPF_AR_BUILD_TIMEOUT`     | 600     | WPF build timeout, seconds                 |
| `WPF_AR_BOOTSTRAP_REPS`    | 10      | bootstrap-only: reps for baseline capture |

## Composite score

```
z = 0.4·norm(alloc_bytes) + 0.3·norm(render_total_ms)
  + 0.2·norm(gc_max_pause_ms) + 0.1·norm(frame_p95_ms)
norm(x) = (x − baseline_median) / baseline_std
```

Lower z is better. Weights chosen so allocation (the most stable signal)
dominates and frame_p95 (noisier user-experience metric) contributes least.
Edit `WEIGHTS` in `eval.py` if your priorities differ.

## Decision protocol

Each iteration ends with one of:

| Decision        | Exit | Action taken by `eval.py`                          |
|-----------------|------|----------------------------------------------------|
| `KEEP`          | 0    | commit stays                                       |
| `REVERT`        | 1    | `git reset --hard HEAD^` (composite did not improve) |
| `REJECT-PARETO` | 2    | `git reset --hard HEAD^` (a metric regressed > 3%) |
| `BUILD-FAIL`    | 3    | `git reset --hard HEAD^` (build broke)             |
| `SPIKE-FAIL`    | 4    | `git reset --hard HEAD^` (scenario crashed)        |

`KEEP` requires both: composite z STRICTLY improved over the most recent KEPT
iteration AND no individual metric regressed past the Pareto threshold. The
agent does not get to declare victory — only `eval.py` does.

## Plotting

`plot.py` produces a 6-panel SVG:
1. Composite z over iterations, with KEEP/REVERT markers
2. Decision histogram
3-6. Each individual metric over iterations, with per-rep min/max error bars

Use the per-rep error bars to spot iterations where measurement noise
swamped the claimed improvement.

## Manual profile runs (outside the ralph loop)

For point-in-time captures against MC (e.g. after a hand-authored WPF fix,
or to validate a big-win), use `run-profile-2026-05-11.py`:

```bash
cd /c/work/wpf-perf
python3 -u autoresearch/run-profile-2026-05-11.py
```

This swaps the candidate WPF DLLs from `artifacts/bin/.../net10.0/` into
MC's `BUILD/x64_Release/`, runs `profile.py --run-multi` (startup +
take-open + playback scenarios), and restores stock DLLs in `finally` even
on crash.  Output lands in `autoresearch/profile-output/{scenario}/`.

### Prereq: MC must be built with `SnoopEnabled=true`

The perf scenarios drive MC via brokered MCP UI automation, which only
ships in the binary when SnoopWPF is enabled.  The MC csproj defaults
`SnoopEnabled=false` for Release config (to keep public builds free of the
private-feed dependency), so a stock `build.cmd build -c Release` produces
a `MotionCatalyst-cli.exe` that the perf scenarios can't drive — they will
hang in `mc_connect` or fail at the first `wpf_click`.

Rebuild MC explicitly with the flag set:

```bash
# Option A — env var (recognised by the existing csproj logic)
SNOOP_ENABLED=true build.cmd build -c Release

# Option B — direct dotnet build with property
cmd.exe /c "dotnet build src\motioncatalyst\Applications\MotionCatalyst\MotionCatalyst-cli.csproj -c Release -p:SnoopEnabled=true"
```

Verify after build: `ls BUILD/x64_Release/ | grep -i snoop` should list six
`SnoopWPF.*.dll` files.  If they're missing, the brokered-MCP path is
disabled and the scenarios will not drive the UI.

### Capturing stack-attributed allocations

`nettrace-probe.exe` (rolled up to type only) and `TypeStackProbe.exe` (top
N callstacks per type, attributable via `GCAllocationTick_V4` events) live
under `tools/probe/` and `tools/type-stack-probe/` respectively.  The
existing per-scenario `.nettrace` files capture stacks
(`DotNETRuntime:0x1FFBCCBFF:5` keyword bits include the Stack bit) so
stack attribution requires only the probe tool, not a recapture.
