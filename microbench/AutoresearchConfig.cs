using BenchmarkDotNet.Configs;
using BenchmarkDotNet.Diagnosers;
using BenchmarkDotNet.Exporters.Json;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Toolchains.CsProj;
using BenchmarkDotNet.Toolchains.DotNetCli;
using BenchmarkDotNet.Validators;

namespace WpfMicrobenchmarks;

/// <summary>
/// BDN config for autoresearch microbenchmarks.
///
/// Job profile is tuned for fast iteration (≈30–60s/benchmark) while keeping
/// per-bench CV under 1% on time and ~0% on alloc. Defaults chosen to match
/// the consensus recommendation (gemini-3.1-pro + gpt-5.5-pro): same-session
/// A/B with statistical decision rule downstream.
///
/// Per-iter the autoresearch loop runs both HEAD~1 (baseline) and HEAD
/// (candidate) in the same BDN invocation so machine state, JIT state, and
/// CPU frequency governor are identical between samples.
/// </summary>
public class AutoresearchConfig : ManualConfig
{
    public AutoresearchConfig()
    {
        AddJob(Job.Default
            .WithToolchain(CsProjCoreToolchain.From(new NetCoreAppSettings(
                targetFrameworkMoniker: "net10.0-windows",
                runtimeFrameworkVersion: null,
                name: ".NET 10.0 Windows")))
            .WithLaunchCount(1)
            .WithWarmupCount(3)
            .WithIterationCount(10)
            .WithUnrollFactor(16));

        AddDiagnoser(MemoryDiagnoser.Default);

        AddExporter(JsonExporter.Full);

        AddValidator(ExecutionValidator.FailOnError);
    }
}
