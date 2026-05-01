# wpf-perf-analyze

ETL analysis tool for MotionCatalyst perf runs (W3.2). Reads a Windows ETW `.etl`
file via `Microsoft.Windows.EventTracing.Processing` (TraceProcessor) and emits
structured JSON metrics suitable for A/B comparison.

## Usage

```
wpf-perf-analyze <etl-path> -o <json-path> [options]
```

### Options

| Flag | Description | Default |
|---|---|---|
| `-o, --output <path>` | Output JSON file path | (required) |
| `--process-name <name>` | Target process name | `MotionCatalyst-cli.exe` |
| `--top-n <n>` | Top-N methods / allocator types | `50` |
| `--baseline-mode` | Alias for `--top-n 100` | off |
| `--allow-lost-events` | Continue if the ETL has dropped events | off |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Usage / argument error |
| 2 | Analysis error (bad ETL, missing file) |

## Output JSON

The output file is a single JSON object (`AnalysisResult`) with the following fields:

```jsonc
{
  "etlPath":        "<absolute path>",
  "fileSizeBytes":  12345678,
  "captureSpanMs":  30000.0,
  "analysisWarnings": ["..."],

  "process": {
    "pid": 1234,
    "imageName": "MotionCatalyst-cli.exe",
    "startTimeMs": 0.0,
    "durationMs": 30000.0,
    "cpuTimeMs": 1500.0,
    "contextSwitchCount": 40000
  },

  "gc": {
    "totalGcCount": 42,
    "gen0Count": 30, "gen1Count": 8, "gen2Count": 4,
    "totalPauseMs": 25.3,
    "totalAllocBytes": 1073741824
  },

  "jit": {
    "methodCount": 1200,
    "totalJitTimeMs": 450.0
  },

  "wpf": {
    "layoutCount": 600,   "layoutTotalMs": 80.0,
    "renderCount": 1800,  "renderTotalMs": 250.0,
    "bamlCount":  120,    "bamlTotalMs": 15.0
  },

  "topCpuMethods": [
    { "method": "SomeMethod", "module": "SomeModule", "sampleCount": 400, "cpuPercent": 2.5 }
  ],

  "topAllocators": [
    { "method": "System.String", "module": "System", "allocBytes": 10485760, "allocCount": 80 }
  ],

  "perfHarnessEvents": {
    "scenario": "Scrubbing",
    "scenarioStartTimestampMs": 1000.0,
    "scenarioEndTimestampMs": 5000.0,
    "stepTimings": [
      { "name": "OpenProject", "startMs": 1050.0, "endMs": 1300.0, "elapsedMs": 250.0 }
    ],
    "idleDetections": [
      { "step": "OpenProject", "atMs": 1295.0 }
    ]
  },

  "exceptions": {
    "totalCount": 3,
    "byType": [
      { "type": "System.InvalidOperationException", "count": 2 }
    ]
  }
}
```

`perfHarnessEvents` is `null` when no `MotionCatalyst-PerfHarness` EventSource events
are present in the trace (i.e. the W1.3 harness has not yet emitted events, or the
provider was not enabled during capture).

## Building

```bash
# From repo root on Windows/WSL
cd tools/analyze
dotnet build WpfPerfAnalyze.csproj -c Debug
```

Or via the repo build script if available.

## Running tests

```bash
cd tools/analyze
dotnet test Tests/WpfPerfAnalyze.Tests.csproj
```

## Prerequisites

- .NET 10 SDK
- `Microsoft.Windows.EventTracing.Processing.All` 1.12.10 (restored automatically via NuGet)
- Symbols (optional): set `_NT_SYMBOL_PATH` to a local symbol cache for CPU method name resolution

## EventSource provider

The `MotionCatalyst-PerfHarness` EventSource GUID is computed at startup from the
provider name using the standard BCL algorithm (SHA-1 name-based UUID v5). Event IDs:

| ID | Name | Fields |
|---|---|---|
| 1 | ScenarioStart | Scenario (string) |
| 2 | ScenarioEnd | Scenario (string) |
| 3 | StepStart | StepName (string) |
| 4 | StepEnd | StepName (string), ElapsedMs (double) |
| 5 | IdleDetected | StepName (string) |
| 6 | ScenarioSentinel | (none) |
