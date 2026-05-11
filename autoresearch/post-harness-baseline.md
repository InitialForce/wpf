# Post-Harness Baseline — 2026-05-11

## Build

| Artifact | mtime |
|---|---|
| MotionCatalyst.Logging.dll | 2026-05-11 20:34 |
| MotionCatalyst-cli.exe | 2026-05-11 20:35 |
| MotionCatalyst.exe | 2026-05-11 20:25 |

Build: `dotnet build src\motioncatalyst\Applications\MotionCatalyst\MotionCatalyst-cli.csproj -c Release -p:SnoopEnabled=true`

Edit applied: all 6 `[Event]` declarations in `PerfHarnessEventSource.cs` have `ActivityOptions = EventActivityOptions.Disable`.

Note: `SnoopEnabled=true` must be passed explicitly for Release builds — the default MSBuild logic sets it `false` for Release config.

## Stock DLL Restoration

CLEAN — `run-profile-2026-05-11.py` restores candidate WPF DLLs in its `finally` block unconditionally. Verified clean on all three runs.

## Harness Fix Confirmation — All 3 Scenarios

Run at 2026-05-11 20:47–20:51. All 3 scenarios completed successfully.

Fresh `nettrace-probe --json` run on each `.nettrace`:

| Scenario | FourElementAsyncLocalValueMap | ActivityInfo | System.Threading.ExecutionContext |
|---|---|---|---|
| startup | NONE | NONE | NONE |
| take-open | NONE | NONE | NONE |
| playback | NONE | NONE | NONE |

The `ActivityOptions = EventActivityOptions.Disable` edit confirmed effective across all three scenarios.

## 3-Way Comparison Table

Reference points:
- **PRE-T4**: `{scenario}-pre-t4/analysis.json` (stock WPF, no harness fix)
- **POST-T4**: `{scenario}/analysis-post-t4.json` (T4 `AdornerLayer._zOrderMap` pool patch, no harness fix)
- **POST-HARNESS**: `{scenario}/analysis-post-harness.json` (T4 + `ActivityOptions.Disable` fix, fresh 2026-05-11 run)

### startup

| Metric | PRE-T4 | POST-T4 | POST-HARNESS |
|---|---|---|---|
| totalAllocBytes | 308.0 MB | 307.6 MB | 295.0 MB |
| renderPassCount | 707 | 758 | 800 |
| renderFrameP95Ms | 72.77 ms | 74.70 ms | 73.86 ms |
| harness artifacts | NONE | NONE | NONE |

### take-open

| Metric | PRE-T4 | POST-T4 | POST-HARNESS |
|---|---|---|---|
| totalAllocBytes | 615.6 MB | 298.6 MB | 249.5 MB |
| renderPassCount | 32,484 | 33,516 | 33,205 |
| renderFrameP95Ms | 10.55 ms | 9.93 ms | 10.33 ms |
| harness artifacts | 22.15 MB | 22.77 MB | **NONE** |

Note: T4 cut totalAllocBytes by 317 MB (51.5%); harness fix removed a further 22 MB of `ActivityTracker` noise, giving the clearest alloc picture yet.

### playback

| Metric | PRE-T4 | POST-T4 | POST-HARNESS |
|---|---|---|---|
| totalAllocBytes | 245.8 MB | 533.0 MB | 26.7 MB |
| renderPassCount | 18,169 | 876 | 17,971 |
| renderFrameP95Ms | 1.72 ms | 31.63 ms | 1.98 ms |
| harness artifacts | 12.91 MB | NONE | **NONE** |

Note: POST-T4 playback was an invalid idle-capture (876 passes, 533 MB — anomaly noted in prior run). POST-HARNESS is a valid real-playback capture (17,971 passes, 26.7 MB). The post-harness `totalAllocBytes` drop to 26.7 MB looks like a much shorter trace window (18 s capture vs longer for take-open); renderPassCount matches PRE-T4 confirming it's a real playback.

## Analysis Files

| Scenario | Path |
|---|---|
| startup post-harness probe | `profile-output/startup/analysis-post-harness.json` |
| take-open post-harness probe | `profile-output/take-open/analysis-post-harness.json` |
| playback post-harness probe | `profile-output/playback/analysis-post-harness.json` |

Probe invocation: `nettrace-probe.exe <trace> --top 30 --json <out>`

## Known Remaining Issues

- `MotionCatalyst.exe` (GUI build) was not rebuilt with `SnoopEnabled=true`; only `MotionCatalyst-cli.exe` is usable for brokered-MCP perf runs in Release config.
- `build.cmd build -c Release` does not forward `-p:SnoopEnabled=true`; passing it via `dotnet build` is required for brokered-MCP-enabled Release builds.  Worth wiring up.

## Next-round targets (post-harness, all 3 scenarios in top-30)

After T4 + harness fix, no single residual WPF wedge is large enough to call a "big win" candidate.  The remaining top allocators on take-open are dominated by MC domain types (`ForceSample`, `ForceSampleWithLayout[]`, `ForcePlateSerializationDataSample`, etc.) plus modest WPF residuals:

| Type | take-open | playback | combined | comment |
|---|---|---|---|---|
| `DispatcherOperation` | 7.99 MB | 4.58 MB | 12.6 MB | layout/render re-queue (was T6) |
| `Task`1[System.Object]` | 5.54 MB | 3.30 MB | 8.8 MB | DispatcherOperation continuations |
| `DispatcherHookEventArgs` | 2.56 MB | 2.23 MB | 4.8 MB | dispatcher hook args |
| `DispatcherOperationTaskSource`1` | 2.45 MB | 0.95 MB | 3.4 MB | task-source backing |
| Text formatting cluster | ~8 MB | — | ~8 MB | `FullTextLine` + `LSRun` + `Span` |
| `System.Windows.ModifiedValue` | 3.83 MB | 1.49 MB | 5.3 MB | DP animation override storage |

The next ~5 candidates each sit at 1-5 MB.  None justifies a single-PR optimization on its own; a sweep targeting the dispatcher-op cluster could collectively recover ~25-30 MB on take-open but the risk/reward versus T1/T2/T4 is much lower.
