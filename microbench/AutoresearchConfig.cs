using BenchmarkDotNet.Configs;
using BenchmarkDotNet.Diagnosers;
using BenchmarkDotNet.Exporters.Json;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Toolchains.InProcess.Emit;
using BenchmarkDotNet.Validators;

namespace WpfMicrobenchmarks;

/// <summary>
/// BDN config for autoresearch microbenchmarks.
///
/// Uses InProcessEmitToolchain — benchmarks run in the same process as the
/// BDN runner (Microbenchmarks.exe). The default CsProjCoreToolchain spawns
/// an inner process per benchmark; that inner process inherits a
/// Microsoft.WindowsDesktop.App framework reference (via the parent's
/// UseWPF=true) and emits it into its runtimeconfig.json's "frameworks"
/// list. The .NET host then resolves WindowsBase / System.Xaml /
/// PresentationCore from the system runtime pack (which holds the upstream
/// dotnet/wpf source, not our autoresearch fork's edits) instead of from the
/// inner build's own bin dir — silently bypassing every per-iter DLL swap
/// microbench.py performs and leaving every WindowsBase-resident edit
/// measuring as alloc Δ +0 / time Δ in the noise. PrivateAssets="all",
/// DisableTransitiveFrameworkReferences, and explicit Reference HintPaths in
/// Microbenchmarks.csproj all fail to override that propagation through BDN's
/// auto-generated inner csproj.
///
/// InProcess is a quality trade-off (host JIT/GC state shared across benches
/// within a session), but the alternative is verifiably-incorrect data —
/// out-of-process measures the wrong DLL. Each microbench.py iter still does
/// two fresh process launches (one per side of the A/B), so process-level
/// state is reset between baseline and candidate. Within a side, BDN's
/// per-bench warmup (3 runs) absorbs JIT noise; static state across benches
/// is the residual exposure.
///
/// Job profile is tuned for fast iteration (≈30–60s/benchmark) while keeping
/// per-bench CV under 1% on time and ~0% on alloc. Defaults chosen to match
/// the consensus recommendation (gemini-3.1-pro + gpt-5.5-pro): same-session
/// A/B with statistical decision rule downstream.
/// </summary>
public class AutoresearchConfig : ManualConfig
{
    public AutoresearchConfig()
    {
        AddJob(Job.Default
            .WithToolchain(InProcessEmitToolchain.Instance)
            .WithLaunchCount(1)
            .WithWarmupCount(3)
            .WithIterationCount(10)
            .WithUnrollFactor(16));

        AddDiagnoser(MemoryDiagnoser.Default);

        AddExporter(JsonExporter.Full);

        AddValidator(ExecutionValidator.FailOnError);
    }
}
