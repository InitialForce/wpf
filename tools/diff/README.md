# wpf-perf-diff

A/B comparison tool for `wpf-perf-analyze` JSON outputs. Reads two sets of analysis runs
(baseline + experimental), computes deltas, and emits a markdown report.

## Usage

```
wpf-perf-diff <baseline> <experimental> [-o <report.md>] [--threshold-pct N]
```

### Single-run comparison

```
wpf-perf-diff baseline.json experimental.json -o report.md
```

### Multi-run (median aggregation)

Pass comma-separated paths for each side. Median + stddev are computed when N >= 3.

```
wpf-perf-diff \
  base1.json,base2.json,base3.json,base4.json,base5.json \
  exp1.json,exp2.json,exp3.json,exp4.json,exp5.json \
  -o report.md --threshold-pct 5
```

### Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `<baseline>` | required | Path or comma-separated list of analyze JSON files |
| `<experimental>` | required | Path or comma-separated list of analyze JSON files |
| `-o <path>` | stdout | Output path for the markdown report |
| `--threshold-pct N` | 5 | Flag metrics with change > N% as wins/regressions |
| `--label-baseline L` | filename | Override label for the baseline side |
| `--label-exp L` | filename | Override label for the experimental side |

## Report sections

| Section | Description |
|---------|-------------|
| Headline metrics | Scenario duration, total CPU, alloc, GC, JIT, layout, render |
| Per-step timings | Duration of each named scenario step (requires PerfHarnessEvents) |
| Top CPU regressions | Methods that got slower by > threshold%, ordered by absolute delta |
| Top CPU wins | Methods that got faster |
| Top allocation regressions | Methods that allocated more |
| Top allocation wins | Methods that allocated less |
| Methods exclusive to one side | Methods in top-CPU list of only baseline or only experimental |
| Run-to-run noise | Stddev table; warns if stddev > 5% of median |
| Verdict | One-sentence automated summary |

## Flag legend

| Symbol | Meaning |
|--------|---------|
| `✓ win` | Improvement > threshold% |
| `⚠ win` | Large improvement > 2× threshold% |
| `✗ regression` | Regression > threshold% |
| `⚠ ✗ regression` | Large regression > 2× threshold% |
| `–` | Change within threshold (noise) |

## Module layout

```
tools/diff/
  WpfPerfDiff.csproj     .NET 10 console
  Program.cs             CLI argument parsing + entry point
  AnalysisLoader.cs      Load + validate analyzer JSON files
  Aggregator.cs          Median/stddev aggregation across N runs
  Differ.cs              Compute deltas + classify regression/win/noise
  ReportWriter.cs        Markdown emitter + verdict logic
  Models/
    AnalysisResult.cs    DTOs (copied from analyze project)
    AggregatedRun.cs     Aggregated metrics for one side
    DiffResult.cs        Delta + classification models
  Tests/
    AggregatorTests.cs   Unit tests (NUnit 4)
```

## Input JSON format

The input JSON must be the output of `wpf-perf-analyze`. See `tools/analyze/Models/AnalysisResult.cs`
for the full schema. Key sections used by the diff tool:

- `process.cpuTimeMs` — total CPU
- `gc.totalAllocBytes`, `gc.gen0Count` etc. — GC metrics
- `jit.methodsJitted` — JIT count
- `wpf.layoutPassCount`, `wpf.renderPassCount` etc. — WPF metrics
- `topCpuMethods[].{method,module,cpuMs}` — per-method CPU
- `topAllocators[].{method,module,allocBytes}` — per-method alloc
- `perfHarnessEvents.{scenarioStartTimestampMs,scenarioEndTimestampMs,stepTimings}` — scenario duration + per-step
