using BenchmarkDotNet.Running;
using System;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Entry point for the standalone BenchmarkDotNet perf harness.
///
/// Usage:
///   dotnet run -c Release -- [BDN args]
///
/// Examples:
///   dotnet run -c Release -- --filter "*"
///   dotnet run -c Release -- --filter "*SMOKE-002*" --exporters json
///
/// Results are written to BenchmarkDotNet.Artifacts/ in the output directory.
/// CI post-processes the JSON via perf/check-regression.py.
/// </summary>
internal static class PerfProgram
{
    [STAThread]
    public static void Main(string[] args)
    {
        Console.WriteLine("InitialForce.WpfPerf — BenchmarkDotNet harness");
        Console.WriteLine($"Run at: {DateTime.UtcNow:O}");
        Console.WriteLine();

        BenchmarkSwitcher
            .FromAssembly(typeof(PerfProgram).Assembly)
            .Run(args, new BenchmarkConfig());
    }
}
