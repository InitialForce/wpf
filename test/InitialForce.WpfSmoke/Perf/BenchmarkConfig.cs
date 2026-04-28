using BenchmarkDotNet.Configs;
using BenchmarkDotNet.Diagnosers;
using BenchmarkDotNet.Exporters.Json;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Validators;

namespace InitialForce.WpfSmoke;

/// <summary>
/// BenchmarkDotNet configuration for the WpfSmoke perf harness.
/// Uses Job.ShortRun to keep CI wall time reasonable while still
/// producing statistically meaningful allocations-per-operation data.
///
/// Outputs:
///   - Console summary table
///   - JSON file consumed by perf/check-regression.py
/// </summary>
public class BenchmarkConfig : ManualConfig
{
    public BenchmarkConfig()
    {
        // Single short job: reduces CI time.
        // 1 launch × 3 warmup × 10 iterations per benchmark.
        AddJob(Job.ShortRun
            .WithLaunchCount(1)
            .WithWarmupCount(3)
            .WithIterationCount(10));

        // Memory diagnostics: allocations per operation appear in JSON output.
        AddDiagnoser(MemoryDiagnoser.Default);

        // JSON export consumed by perf/check-regression.py.
        AddExporter(JsonExporter.Full);

        // Suppress platform warnings in CI.
        AddValidator(ExecutionValidator.FailOnError);
    }
}
