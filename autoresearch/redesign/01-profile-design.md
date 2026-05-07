# Tier A Profile Redesign: Multi-Scenario + Allocation Attribution

## 1. Current State

`profile.py` (406 lines) runs a single scenario (spike-9 playback) and
produces a 30-entry `profile.json` ranked solely by CPU inclusive time.
Key structural facts:

- **Entry point** (`run_spike`, lines 114-133): sets `SPIKE9_RESULT_DIR`
  and `SPIKE9_NAME` env vars, runs `spike-9-play-take.py` as a subprocess,
  finds the newest `.nettrace` in `PROFILE_OUTPUT_DIR`.

- **CPU aggregation** (`aggregate_speedscope`, lines 177-234): reads a
  `.speedscope.json` produced by `dotnet-trace convert`. Walks evented
  profiles (O/C events) and charges each dt to every frame on the stack
  — this is inclusive-time, correct for framework ranking.

- **Benchmark mapping** (`existing_benchmarks`, lines 240-276):
  scans `microbench/Benchmarks/*.cs` for `Type.Method(` patterns against
  a hard-coded `KNOWN_WPF_TYPES` list; returns `{Type.Method: bdn_glob}`.

- **Output** (`write_profile_json`, lines 302-327): writes schema_version=1
  with `hot_paths` list. Each entry has:
  `method`, `samples` (ms inclusive), `cpu_pct_total`, `bdn_filter`
  (null if no bench), `needs_benchmark` (bool).

- **Synthetic entries** (lines 370-383): any benchmarked Type.Method NOT
  found in the ranked list is injected with `samples=0.0, cpu_pct_total=0.0`
  so inner Claude always has at least one testable target.

**Problem with current output**: all 30 real entries are Dispatcher/render
infrastructure from the play-take scenario — same call tree, similar
inclusive times (~1.71%). No layout, BAML, or startup methods appear.
Only one benchmark (`*GeometryParser*`) matches the bench DB, causing
inner Claude to loop on it (6+ REJECT-UNCLEAR observed).

**What microbench.py consumes** from profile.json: nothing directly —
it receives `--filter` and `--bench-name` from the inner Claude agent who
reads profile.json and picks `bdn_filter`. Fields consumed by inner Claude:
`method`, `cpu_pct_total`, `alloc_pct_total` (once added), `bdn_filter`,
`needs_benchmark`, `scenarios` (new). The schema must keep `hot_paths[]`
as the root array with those field names.

---

## 2. Element 1: Multi-Scenario Profile Aggregation

### 2.1 Scenario script convention

New scripts live at `tools/poc/scenario-<slug>.py`:

- `tools/poc/scenario-startup.py` — cold app startup (derives from
  spike-8-startup.py approach: start trace immediately on PID spawn,
  stop after mc_connect returns + settle)
- `tools/poc/scenario-take-open.py` — take-open (navigates to Analysis,
  opens a take from disk, traces from the open-click until VideoSlider.Maximum > 0)
- `tools/poc/scenario-playback.py` — 10s steady-state playback (wraps
  spike-9 behavior without reusing it directly, since spike-9 is immutable)

spike-9-play-take.py remains immutable (program.md line 69). The new playback
scenario is a clean reimplementation that shares the runner library.

Each scenario script:
- Accepts `SCENARIO_RESULT_DIR` and `SCENARIO_NAME` env vars (analogous to
  spike-9's `SPIKE9_RESULT_DIR`/`SPIKE9_NAME`)
- Produces exactly one `.nettrace` in `$SCENARIO_RESULT_DIR/`
- Handles MC lifecycle: launch via McpDriver, drive, disconnect, taskkill
- Returns exit 0 on success, non-zero on failure
- Is idempotent: re-running overwrites existing artifacts

### 2.2 Take fixture for take-open scenario

The playback scenario (spike-9) already requires a real `.take` file
from `/f/work/takes` (controlled by `SPIKE9_TAKES_SRC_DIR`). The same
env var / fixture applies to the take-open scenario — it reuses the same
import-takes helper.

A minimal canonical take is NOT shipped in `tools/poc/data/` because:
1. Real .take files are large (video data) and not suited for git.
2. The placeholder takes in `Tests/…/Assets/Takes` have 5-byte AVIs and
   cannot exercise the video-open code path.
3. The existing `SPIKE9_TAKE_FIXTURE` / `SPIKE9_TAKES_SRC_DIR` env var
   pattern is already deployed and documented.

The take-open scenario uses the same env vars (`SCENARIO_TAKES_SRC_DIR`,
`SCENARIO_TAKE_FIXTURE`) defaulting to the same values as spike-9.

### 2.3 profile.py changes

`profile.py` gains a new multi-scenario mode. Structural changes:

**New CLI flags:**
- `--run-multi` — run all three scenario scripts, aggregate, write profile.json
- `--run` remains for backward compatibility (single-scenario, spike-9)
- `--trace` remains for single-trace analysis

**New function `run_scenario(slug, script_path) -> Path`:**
Mirrors `run_spike()` but accepts an arbitrary script path and uses
`SCENARIO_RESULT_DIR` / `SCENARIO_NAME` env vars. Returns path to the
newest `.nettrace` in the result dir.

**New function `aggregate_multi_scenario(scenario_traces: dict[str, Path]) -> list[Entry]`:**
1. For each scenario, calls `convert_to_speedscope()` + `aggregate_speedscope()`
   → `(counter, total_ms)`.
2. Filters each counter to WPF methods via `is_wpf_method()` (reuses existing).
3. Builds a unified set: union of top-K from each scenario (K=10 per scenario
   → max 30 entries before dedup).
4. For each method in the union, records per-scenario inclusive ms and pct.
5. Deduplicates by exact method string.
6. Ranks by `max(cpu_pct_total across scenarios)` — takes the hottest
   scenario for each method as its primary signal.

**Schema additions to `hot_paths` entries (backward-compatible):**
```json
{
  "method": "...",
  "samples": 1234.5,           // sum of inclusive ms across all scenarios (unchanged meaning)
  "cpu_pct_total": 2.5,        // max cpu% across scenarios (was: single-scenario pct)
  "bdn_filter": "...",
  "needs_benchmark": false,
  "scenarios": ["startup", "playback"],   // NEW: which scenarios this appears in
  "scenario_cpu_pct": {                   // NEW: per-scenario breakdown
    "startup": 4.1,
    "take_open": 0.0,
    "playback": 2.5
  },
  "alloc_bytes": 0,            // NEW (Phase 2, zero until Element 2 lands)
  "alloc_pct_total": 0.0       // NEW (Phase 2, zero until Element 2 lands)
}
```

Existing consumers (inner Claude) read `cpu_pct_total` and `bdn_filter` —
both fields remain. The new fields are additive. `samples` is redefined as
"sum across scenarios" which is a compatible change (inner Claude uses it
only for display, not decision logic).

**Per-scenario weighting:** Equal weight for dedup and union. The
`scenario_cpu_pct` breakdown is shown to inner Claude so it can pick
methods relevant to the scenario being optimized. No frequency-weighted
aggregation — this avoids penalizing short-duration scenarios (startup ≈
25s vs playback ≈ 19s) and keeps the logic simple.

**Target profile.json size:** 15-20 entries. With 3 scenarios × top-10
each = 30 candidates, post-dedup empirically yields 15-20 since
render/dispatcher infrastructure overlaps across scenarios.

### 2.4 Notes section update

`write_profile_json()` gets an updated `notes` array that describes the
multi-scenario origin. The `source` field becomes a list of per-scenario
trace file names + sizes.

---

## 3. Element 2: AllocationTick Allocation Attribution

### 3.1 Why AllocationTick is not in speedscope

`dotnet-trace convert --format speedscope` emits only SampleProfiler
stack samples (evented format). AllocationTick events
(`Microsoft-Windows-DotNETRuntime:AllocationTick`) are CLR keyword events
with payload `(AllocationAmount, TypeName, HeapIndex, Address)` plus a
stack. They exist in the raw `.nettrace` but dotnet-trace's speedscope
exporter drops them.

The raw `.nettrace` contains these events when
`Microsoft-Windows-DotNETRuntime:0x1FFBCCBFF:5` is in the provider list.
`EVENTPIPE_PROVIDERS` in `dotnet_trace.py` (line 35-44) already includes
this provider at the right keywords and level. **No change to the trace
capture command is needed.** AllocationTick data is already being recorded;
we just aren't parsing it.

### 3.2 Parser approach: C# TraceEvent helper (recommended)

**Option A — Python + `dotnet-trace report`:**
`dotnet-trace report --report gc-verbose` dumps GC events but not
AllocationTick stacks in a usable structured format.

**Option B — Python + direct nettrace parsing:**
The `.nettrace` format is a binary EventPipe stream. There is no
production-quality pure-Python parser. The `perfetto` library and
`microsoft-diagnostics-eventtrace` are not pip-installable in this env.

**Option C — Small C# helper (recommended):**
A ~100-line C# tool at `tools/alloc-parser/AllocParser.csproj` uses
`Microsoft.Diagnostics.Tracing.TraceEvent` (NuGet) to:
1. Open the `.nettrace` with `TraceLog.OpenOrConvert()`.
2. Walk `AllocationTick` events, accumulate `alloc_bytes` per frame name
   by summing `AllocationAmount` across all events where that frame appears
   in the call stack (same inclusive-attribution logic as `aggregate_speedscope`).
3. Filter to WPF frames.
4. Write a compact JSON: `[{"frame": "...", "alloc_bytes": N}, ...]`.

`profile.py` calls this helper via `cmd.exe /c AllocParser.exe <nettrace>
--output <json>` and parses the output JSON. The helper is built once as
part of the project (Release, self-contained) and its binary path is
`tools/alloc-parser/bin/Release/net10.0/win-x64/publish/AllocParser.exe`.

### 3.3 AllocationTick noise floor

AllocationTick fires every ~100KB of allocated bytes (the CLR sampling
interval). A 10s trace with ~50 MB total managed allocation produces ~500
events. Each event carries the allocation type and a stack. This is
sampled, not exhaustive:
- Methods that allocate < 100KB total in the trace window may produce zero
  events (invisible to this approach).
- Short-lived small allocations in tight loops are likely undercounted.
- The noise floor is approximately 100KB per stack path. We document this
  in profile.json's `notes` field.

### 3.4 Aggregation in profile.py

New function `aggregate_alloc(alloc_json: Path) -> Counter[str, int]`:
- Reads the JSON output of AllocParser.
- Returns `Counter[frame_name → alloc_bytes]`.

`write_profile_json()` gains an `alloc_counter` parameter. For each
hot_path entry, it looks up `alloc_counter[method]` and computes
`alloc_pct_total = 100 * alloc_bytes / total_alloc_bytes`.

### 3.5 Re-ranking rule

Inner Claude (program.md) already says "pick ONE hot path" with bias toward
"high `alloc_pct_total` or `cpu_pct_total`". The new `alloc_pct_total`
field is additive to the existing decision criteria — no change to
program.md needed (program.md is immutable to the orchestrator anyway; but
the field name `alloc_pct_total` is already referenced there at line 86,
anticipating Phase 2).

Entries are NOT re-ranked by combined score. The ranking order in
profile.json remains CPU-primary (so the file is human-readable from top
to bottom by impact). Inner Claude applies its own pick logic using both
columns.

### 3.6 Multi-scenario alloc aggregation

Same pattern as CPU: for each scenario trace, run AllocParser → get an
alloc counter. Sum across scenarios to get `alloc_bytes` per method. Add
`scenario_alloc_bytes` dict alongside `scenario_cpu_pct` for completeness
but don't require inner Claude to use it.

---

## 4. Open Questions / Risks

| # | Question | Risk | Mitigation |
|---|---|---|---|
| 1 | Take fixture portability | If `/f/work/takes/` is unavailable, take-open and playback scenarios both fail | Make `SCENARIO_TAKES_SRC_DIR` required; fail loudly with instructions |
| 2 | startup scenario trace duration | spike-8 used 25s; if MC startup is faster post-optimization, most of trace is idle | Detect mc_connect return time; use `PRE_CONNECT_S + settle` not fixed 25s |
| 3 | AllocParser.exe build dependency | Adding a C# build step increases Tier A setup time | Build once, cache binary, only rebuild if .csproj changes (mtime check) |
| 4 | AllocationTick on cold startup | CLR JIT compilation itself allocates heavily and will appear in alloc ranking | Add `[System.Runtime.CompilerServices.JitInterop]` and CLR internal namespaces to a `WPF_ALLOC_IGNORE_PATTERNS` list |
| 5 | profile.json size growth | 3 scenarios × per-scenario dicts = larger JSON | Target ≤ 20 entries, gzip not needed (inner Claude reads it raw) |
| 6 | Scenario MC lifecycle conflict | If orchestrator runs scenarios sequentially, second scenario may find MC still alive from first | Each scenario script tracks its own PID via `find_new_pid(pre_pids)` and kills it in finally; confirmed pattern from spike-9 |
| 7 | speedscope `samples` field rename | current `samples` is actually ms, not sample count; renaming would break inner Claude | Keep field name `samples`, update docstring only |

---

## 5. Test Plan

### 5.1 Unit tests (no MC required)

- `test_aggregate_multi_scenario_dedup`: pass two counters with overlapping
  keys, verify union deduplication and per-scenario breakdown.
- `test_is_wpf_method`: verify namespace/module filter hits and misses.
- `test_find_bench_filter_synthetic`: verify synthetic entries match.

### 5.2 Integration tests (MC + traces required)

**Step 1 — Single scenario smoke test:**
```
SCENARIO_RESULT_DIR=/tmp/test-startup python3 tools/poc/scenario-startup.py
```
Verify: `.nettrace` produced, > 1 MB, exit 0.

**Step 2 — Alloc parser smoke test:**
```
cmd.exe /c AllocParser.exe <startup.nettrace> --output /tmp/alloc.json
cat /tmp/alloc.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d), 'frames')"
```
Verify: JSON produced, at least one WPF frame present.

**Step 3 — Multi-scenario profile run:**
```
python3 /c/work/wpf-perf/autoresearch/profile.py --run-multi
```
Verify:
- profile.json has 15-20 entries.
- At least 2 scenarios represented in the `scenarios` field of ≥ 1 entry.
- At least one entry has `alloc_pct_total > 0`.
- At least one entry has `needs_benchmark: false` (existing bench covered).
- schema_version still 1 (backward compat).
- `bdn_filter` still present on entries.

**Step 4 — Inner Claude backward compat check:**
Run one iteration of inner Claude with the new profile.json. Verify it can
parse the file and pick a hot path without error (the `alloc_pct_total`
and `scenario_*` fields must not confuse the pick logic — they are new
columns, not replacements).
