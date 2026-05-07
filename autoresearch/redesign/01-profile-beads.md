# Tier A Profile Redesign — Bead Specs

## Bead: scenario-startup script
- Type: task
- Priority: P1
- BlockedBy: none
- Files touched:
  - `tools/poc/scenario-startup.py` (new)
- Acceptance criteria:
  - [ ] Script accepts `SCENARIO_RESULT_DIR` and `SCENARIO_NAME` env vars
  - [ ] Launches MC-cli via McpDriver, starts dotnet-trace immediately
        after PID appears (before mc_connect blocks), stops after mc_connect
        returns + 3s settle
  - [ ] Exits 0 on success; leaves exactly one `.nettrace` in result dir
  - [ ] Handles MC lifecycle: cooperative disconnect → taskkill on own PID
        in finally block (never kills by image name)
  - [ ] Reuses `tools/runner/src/wpf_perf_runner/` library (McpDriver,
        DotnetTraceCapture, find_dotnet_trace)
  - [ ] Idempotent: re-run overwrites prior artifacts without error
- Test: `SCENARIO_RESULT_DIR=/c/tmp/test-startup SCENARIO_NAME=startup python3 tools/poc/scenario-startup.py` → exit 0, `.nettrace` > 1 MB

---

## Bead: scenario-take-open script
- Type: task
- Priority: P1
- BlockedBy: scenario-startup script
- Files touched:
  - `tools/poc/scenario-take-open.py` (new)
- Acceptance criteria:
  - [ ] Navigates MC to Analysis screen, opens the canonical take fixture
        (same `SCENARIO_TAKES_SRC_DIR` / `SCENARIO_TAKE_FIXTURE` env vars
        as spike-9 with identical defaults)
  - [ ] Starts dotnet-trace just before the open-take click (not during
        warmup/navigation)
  - [ ] Stops trace when `VideoSlider.Maximum > 0` is confirmed + 2s settle
        (take-open complete signal, same poll as spike-9 lines 1053-1082)
  - [ ] Exits 0 on success; `.nettrace` produced in result dir
  - [ ] MC lifecycle: disconnect + taskkill own PID in finally
  - [ ] Does NOT import `spike-9-play-take.py` (that file is immutable;
        shared helpers come from `wpf_perf_runner`)
- Test: `SCENARIO_RESULT_DIR=/c/tmp/test-takeopen SCENARIO_NAME=take-open python3 tools/poc/scenario-take-open.py` → exit 0, `.nettrace` > 500 KB

---

## Bead: scenario-playback script
- Type: task
- Priority: P1
- BlockedBy: scenario-take-open script
- Files touched:
  - `tools/poc/scenario-playback.py` (new)
- Acceptance criteria:
  - [ ] Reimplements spike-9 playback window (takes loaded → trace starts
        → play 10s → pause → trace stops) using `wpf_perf_runner` helpers
  - [ ] Does NOT import or exec `spike-9-play-take.py`
  - [ ] Starts trace after warmup is complete (same pre-play-attach window
        as spike-9: `PRE_PLAY_ATTACH_S=3.0`)
  - [ ] `SCENARIO_INTRACE_SHOTS` env var controls mid-play screenshots
        (default off for clean profiling)
  - [ ] Exits 0; `.nettrace` produced in result dir
  - [ ] MC lifecycle clean (disconnect + own-PID taskkill)
- Test: `SCENARIO_RESULT_DIR=/c/tmp/test-playback SCENARIO_NAME=playback python3 tools/poc/scenario-playback.py` → exit 0, `.nettrace` > 1 MB

---

## Bead: AllocParser C# helper
- Type: task
- Priority: P2
- BlockedBy: none
- Files touched:
  - `tools/alloc-parser/AllocParser.csproj` (new)
  - `tools/alloc-parser/Program.cs` (new)
- Acceptance criteria:
  - [ ] Uses `Microsoft.Diagnostics.Tracing.TraceEvent` NuGet package
  - [ ] CLI: `AllocParser.exe <nettrace-path> --output <json-path> [--top N]`
  - [ ] Parses AllocationTick events from the `.nettrace` and attributes
        `AllocationAmount` bytes to every frame in the allocation call stack
        (inclusive attribution: same frame-charging logic as speedscope CPU)
  - [ ] Filters output to WPF namespaces/modules (same filter set as
        `WPF_NAMESPACE_PATTERNS` in profile.py)
  - [ ] Output JSON: `[{"frame": "<name>", "alloc_bytes": N}, ...]` sorted
        by `alloc_bytes` descending
  - [ ] Writes empty array `[]` (not an error) when no AllocationTick events
        found (noise-floor case)
  - [ ] Targets net10.0-windows; publishes self-contained win-x64
  - [ ] Build: `dotnet publish -c Release -r win-x64 --self-contained`
        produces `bin/Release/net10.0-windows/win-x64/publish/AllocParser.exe`
- Test: Run against a known `.nettrace` captured with DotNETRuntime at
        keyword 0x1FFBCCBFF (already the default); verify output JSON has
        at least one frame with `alloc_bytes > 0`. Run against a small/empty
        trace; verify empty array output, exit 0.

---

## Bead: profile.py multi-scenario aggregation
- Type: task
- Priority: P1
- BlockedBy: scenario-startup script, scenario-take-open script, scenario-playback script
- Files touched:
  - `autoresearch/profile.py` (modify)
- Acceptance criteria:
  - [ ] New `--run-multi` flag runs all three scenario scripts sequentially,
        collecting one `.nettrace` per scenario into
        `PROFILE_OUTPUT_DIR/startup/`, `PROFILE_OUTPUT_DIR/take-open/`,
        `PROFILE_OUTPUT_DIR/playback/`
  - [ ] `aggregate_multi_scenario(scenario_traces)` function: takes
        `dict[str, Path]` mapping slug → nettrace, returns unified ranked
        list with per-scenario CPU columns
  - [ ] Union of top-10 from each scenario, deduped by exact method string
  - [ ] Resulting profile.json has 15-20 entries (enforced: if union > 20,
        truncate to top-20 by max-cpu-pct-across-scenarios)
  - [ ] New fields on each entry: `scenarios` (list[str]), `scenario_cpu_pct`
        (dict[str, float]). Existing fields unchanged.
  - [ ] `--run` and `--trace` modes still work (single-scenario, backward compat)
  - [ ] schema_version remains 1
  - [ ] `source` field is now a list of per-scenario trace summaries
  - [ ] Notes field updated to describe multi-scenario origin
- Test: `python3 autoresearch/profile.py --run-multi` completes without
        error; profile.json validates: `len(hot_paths)` in range [1,20],
        every entry has `scenarios`, `scenario_cpu_pct`, `alloc_bytes`,
        `alloc_pct_total` keys.

---

## Bead: profile.py alloc attribution
- Type: task
- Priority: P2
- BlockedBy: AllocParser C# helper, profile.py multi-scenario aggregation
- Files touched:
  - `autoresearch/profile.py` (modify)
- Acceptance criteria:
  - [ ] New function `build_alloc_parser_cmd(nettrace, output_json)` returns
        the `cmd.exe /c AllocParser.exe ...` argv; binary path is derived
        from `WPF_REPO / "tools/alloc-parser/bin/..."` with a clear
        FileNotFoundError if the binary is absent
  - [ ] New function `aggregate_alloc(alloc_json) -> Counter[str, int]`
        reads AllocParser output JSON
  - [ ] `--run-multi` calls AllocParser for each scenario trace, accumulates
        alloc counters, computes per-entry `alloc_bytes` (sum across
        scenarios) and `alloc_pct_total` (100 * alloc_bytes / total_alloc)
  - [ ] `--run` and `--trace` single-scenario modes also call AllocParser
        if binary is available; if binary absent, `alloc_bytes=0`,
        `alloc_pct_total=0.0` (graceful degradation, logged as warning)
  - [ ] Notes field in profile.json mentions ~100KB AllocationTick noise floor
  - [ ] Alloc columns added to the summary table logged at end of `main()`
- Test: After running `--run-multi`, at least one `hot_paths` entry has
        `alloc_pct_total > 0`. Run with binary absent; verify exit 0 and
        all `alloc_pct_total == 0.0` (not an error).
