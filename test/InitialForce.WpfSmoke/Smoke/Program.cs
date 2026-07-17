using NUnitLite;
using System;
using System.Linq;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Entry point for the smoke test assembly. Runs the NUnitLite console runner.
/// Passing --update-goldens regenerates the pixel-diff golden images before the run;
/// all other arguments are forwarded to NUnit (e.g. --result:path for NUnit3 XML).
/// Benchmarks live in the separate Perf/InitialForce.WpfPerf.csproj project.
/// </summary>
internal static class Program
{
    [STAThread]
    public static int Main(string[] args)
    {
        bool updateGoldens = args.Contains("--update-goldens", StringComparer.OrdinalIgnoreCase);

        if (updateGoldens)
        {
            PixelDiffHelper.UpdateGoldensMode = true;
            Console.WriteLine("[WpfSmoke] --update-goldens mode: golden images will be regenerated.");
        }

        var nunitArgs = args
            .Where(a => !string.Equals(a, "--update-goldens", StringComparison.OrdinalIgnoreCase))
            .ToArray();

        return new AutoRun(typeof(Program).Assembly).Execute(nunitArgs);
    }
}
