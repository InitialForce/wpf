using System;
using System.Linq;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Entry point for the smoke test assembly.
/// Supports two modes:
///   dotnet test               — NUnit test runner picks up via test adapter (default)
///   dotnet run -- --benchmark — BenchmarkDotNet runner for perf benchmarks
///   dotnet run -- --update-goldens [nunit-args...]
///                             — regenerates pixel-diff golden images before running tests
/// </summary>
internal static class Program
{
    [STAThread]
    public static int Main(string[] args)
    {
        // Parse custom flags before forwarding remaining args to NUnit/BDN.
        bool updateGoldens = args.Contains("--update-goldens", StringComparer.OrdinalIgnoreCase);
        bool runBenchmarks = args.Contains("--benchmark",      StringComparer.OrdinalIgnoreCase);

        if (updateGoldens)
        {
            PixelDiffHelper.UpdateGoldensMode = true;
            Console.WriteLine("[WpfSmoke] --update-goldens mode: golden images will be regenerated.");
        }

        if (runBenchmarks)
        {
            // Strip --benchmark from args and forward the rest to BDN.
            var bdnArgs = args
                .Where(a => !string.Equals(a, "--benchmark", StringComparison.OrdinalIgnoreCase))
                .ToArray();
            return BenchmarkRunner.Run(bdnArgs);
        }

        // Default: NUnit console runner.
        var nunitArgs = args
            .Where(a => !string.Equals(a, "--update-goldens", StringComparison.OrdinalIgnoreCase))
            .ToArray();

        return NUnit.ConsoleRunner.Execute(nunitArgs);
    }
}

/// <summary>
/// Thin wrapper around BenchmarkDotNet entry for the perf harness.
/// The actual benchmark classes live in Perf/PerfHarness.cs.
/// </summary>
internal static class BenchmarkRunner
{
    public static int Run(string[] args)
    {
        Console.WriteLine("[WpfSmoke] Starting BenchmarkDotNet harness...");
        BenchmarkDotNet.Running.BenchmarkSwitcher
            .FromAssembly(typeof(Program).Assembly)
            .Run(args, new BenchmarkConfig());
        return 0;
    }
}
