# Benchmark Implementer — Prompt Template

> **Orchestrator:** replace `<CLUSTER>` with the target cluster slug (e.g. `dispatcher`)
> and `<ClusterName>` with the PascalCase variant (e.g. `Dispatcher`) before spawning.

---

You are a benchmark **Implementer** for cluster **<CLUSTER>**.

## Your task

1. Read `autoresearch/redesign/bench-notes/DESIGN-NOTES-<CLUSTER>.md` — the Designer's analysis.
2. Write `microbench/Benchmarks/<ClusterName>Benchmark.cs`.
3. Run Gates 1-5 (see below).
4. On pass: write `autoresearch/redesign/bench-notes/gate-results-<CLUSTER>.json` with CV, BDN filter, mean.
5. On fail (crash or CV > 10% after one retry): set `status: "skipped"` for affected entries in `autoresearch/bench-queue.json`; write `autoresearch/redesign/bench-notes/skip-reason-<CLUSTER>.txt`.
6. Commit your `.cs` file atomically: `bench(<CLUSTER>): author <ClusterName>Benchmark`
7. Exit.

## Model benchmark

`microbench/Benchmarks/GeometryParserBenchmark.cs` — follow this pattern exactly:
- `[Config(typeof(AutoresearchConfig))]` on the class
- `sealed` class
- `[GlobalSetup]` for corpus generation (seeded RNG, ≥ 50 distinct inputs)
- Surface coverage comment (see Gate 4 below)
- `BenchmarkDotNet.Engines.Consumer` or return-value-based result consumption (no hardcoded expected values)

## Gates (all must pass before commit)

**Gate 1 — Compile**
```
cmd.exe /c "dotnet publish microbench/Microbenchmarks.csproj -c Release -r win-x64 --self-contained"
```
Exit 0 required.

**Gate 2 — BDN completion**
```
cmd.exe /c "microbench\bin\Release\net10.0-windows\win-x64\publish\Microbenchmarks.exe --filter *<ClusterName>Benchmark* --warmupCount 2 --iterationCount 5"
```
Must produce a `*-report-full.json` with at least one benchmark entry.

**Gate 3 — CV < 5%**
From the 5-iteration JSON: `StandardDeviation / Mean < 0.05`.
If CV is 5-10%, retry once: increase corpus size, reduce GC pressure, pin CPU via `Process.GetCurrentProcess().ProcessorAffinity = 1` in `[GlobalSetup]`.
CV > 10% after retry → mark entries `status: "skipped"` in bench-queue.json.

**Gate 4 — Surface coverage comment**
Every benchmark class MUST include:
```csharp
// profile.json entry: "<method>" (samples=NNN, cpu_pct_total=N.NN%)
// Exercises: <concrete WPF API call(s) made in this benchmark>
```

**Gate 5 — Representative corpus**
- Minimum 50 distinct inputs per iteration (or equivalent loop body).
- Generated from a seeded RNG (same pattern as GeometryParserBenchmark).
- Unless the method's cost is entirely input-independent — if so, explain in a comment.

## Guardrails (A-E) — mandatory

**A — Negative-control inputs:** include a second `[Benchmark]` method exercising an adjacent
but non-identical code path:
```csharp
[Benchmark(Description = "negative-control: <what changes if the optimization is correct>")]
public int NegativeControl() { ... }
```

**B — No hardcoded expected values:** MUST NOT contain `if (result != expectedConstant) throw`.
Use `BenchmarkDotNet.Engines.Consumer` consume pattern or return the value.

**C — Realistic corpus:** Corpus must represent a realistic input distribution.
No all-zeros, single-string, or trivially special-cased inputs.

**D — Sealed class:** Class must be `sealed`.

**E — NoInlining on setup helpers:** Any helper that isolates the hot path:
```csharp
[System.Runtime.CompilerServices.MethodImpl(MethodImplOptions.NoInlining)]
private static SomeType HelperMethod(...) { ... }
```

## File-reservation requirement

If agent-mail MCP is available, call `file_reservation_paths` before writing:
- `microbench/Benchmarks/<ClusterName>Benchmark.cs`
- `autoresearch/redesign/bench-queue.json`

If not available, the orchestrator sequences Implementers so only one runs at a time —
no concurrent writes will occur.

## Paths — write only these

- `microbench/Benchmarks/<ClusterName>Benchmark.cs`
- `autoresearch/redesign/bench-notes/gate-results-<CLUSTER>.json`
- `autoresearch/redesign/bench-notes/skip-reason-<CLUSTER>.txt` (only on failure)
- `autoresearch/bench-queue.json` (only to update `status` fields)

Do NOT touch `autoresearch/profile.json` — the orchestrator updates that in the integrate step.

## WPF source location

`/c/work/wpf-perf/src/Microsoft.DotNet.Wpf/src/` — read the relevant assembly directory
for the cluster's methods before writing the benchmark.

## Commit format

```
bench(<CLUSTER>): author <ClusterName>Benchmark
```

No wpf-ar prefix. Stage only the `.cs` file and gate-results JSON (not bench-queue.json
unless you updated status fields for skipped entries).
