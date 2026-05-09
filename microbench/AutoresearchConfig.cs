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
/// Uses out-of-process CsProjCoreToolchain. Each benchmark gets its own child
/// process, so JIT/GC state does not leak across benches — the gold-standard
/// BDN measurement quality. The previous run used InProcessEmitToolchain
/// because BDN's inner csproj (auto-generated per benchmark via ProjectReference
/// back to Microbenchmarks.csproj) inherits the parent's UseWPF=true →
/// FrameworkReference Microsoft.WindowsDesktop.App, which propagates into the
/// inner build's runtimeconfig.json's "frameworks" list, so the .NET host
/// resolves WindowsBase / System.Xaml / PresentationCore from the system
/// runtime pack (`C:\Program Files\dotnet\shared\Microsoft.WindowsDesktop.App\`)
/// — silently bypassing every per-iter DLL swap microbench.py was performing
/// in the publish dir, leaving every WindowsBase-resident edit measuring as
/// alloc Δ +0 / time Δ in the noise. PrivateAssets="all", DisableTransitive
/// FrameworkReferences, and explicit Reference HintPaths in Microbenchmarks.csproj
/// all failed to override that propagation through BDN's auto-generated inner
/// csproj.
///
/// Fix (2026-05-09): DOTNET_ROOT shadow. microbench.py constructs a composite
/// shadow dotnet root at /c/work/wpf-perf/.dotnet-shadow/ (NTFS junctions back
/// to the system installation for sdk/host/packs/Microsoft.NETCore.App, plus
/// a physical deep copy of the Microsoft.WindowsDesktop.App pack so we can
/// overwrite WindowsBase / System.Xaml / PresentationCore per side). The
/// orchestrator sets DOTNET_ROOT, DOTNET_ROOT_X64, DOTNET_MULTILEVEL_LOOKUP=0,
/// and DOTNET_ReadyToRun=0 in the BDN environment. The inner BDN child process
/// inherits these, so its hostfxr resolves Microsoft.WindowsDesktop.App from
/// the shadow → loads our patched DLLs. Verified end-to-end with a hash check:
/// inner-child WindowsBase.Location matched the artifacts/bin/.../WindowsBase.dll
/// hash exactly.
///
/// We pin the inner build's CLI to the shadow's dotnet.exe via
/// NetCoreAppSettings.WithCustomDotNetCliPath. BDN sets DOTNET_MULTILEVEL_LOOKUP=0
/// automatically when a custom CLI path is supplied (per BenchmarkDotNet
/// source, DotNetCliCommandExecutor.BuildStartInfo). The orchestrator sets it
/// again at execution time as a defensive override.
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
            .WithToolchain(CsProjCoreToolchain.From(new NetCoreAppSettings(
                targetFrameworkMoniker: "net10.0-windows",
                runtimeFrameworkVersion: null,
                name: ".NET 10 (shadow)",
                customDotNetCliPath: @"C:\work\wpf-perf\.dotnet-shadow\dotnet.exe")))
            .WithLaunchCount(1)
            .WithWarmupCount(3)
            .WithIterationCount(10)
            .WithUnrollFactor(16));

        AddDiagnoser(MemoryDiagnoser.Default);

        AddExporter(JsonExporter.Full);

        AddValidator(ExecutionValidator.FailOnError);
    }
}
