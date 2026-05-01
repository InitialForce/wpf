using System.Globalization;
using WpfPerfDiff;

// Use invariant culture so decimal separators are always '.' regardless of OS locale.
CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
CultureInfo.DefaultThreadCurrentUICulture = CultureInfo.InvariantCulture;

// ── Argument parsing ──────────────────────────────────────────────────────────
// Usage: wpf-perf-diff <baseline-spec> <experimental-spec> -o <report.md> [--threshold-pct 5]
//   <baseline-spec>     : path or comma-separated list of analyze JSON paths
//   <experimental-spec> : path or comma-separated list of analyze JSON paths
//   -o <report.md>      : output markdown path (default: stdout)
//   --threshold-pct N   : threshold % for flagging (default: 5)
//   --label-baseline L  : label override for baseline side
//   --label-exp L       : label override for experimental side

if (args.Length < 2)
{
    PrintUsage();
    return 1;
}

string baselineSpec = args[0];
string experimentalSpec = args[1];
string? outputPath = null;
double thresholdPct = 5.0;
string? labelBaseline = null;
string? labelExp = null;

for (int i = 2; i < args.Length; i++)
{
    switch (args[i])
    {
        case "-o" or "--output":
            if (i + 1 >= args.Length) { Console.Error.WriteLine("Missing value for -o"); return 1; }
            outputPath = args[++i];
            break;
        case "--threshold-pct":
            if (i + 1 >= args.Length || !double.TryParse(args[++i], out thresholdPct))
            { Console.Error.WriteLine("Invalid --threshold-pct"); return 1; }
            break;
        case "--label-baseline":
            if (i + 1 >= args.Length) { Console.Error.WriteLine("Missing value for --label-baseline"); return 1; }
            labelBaseline = args[++i];
            break;
        case "--label-exp":
            if (i + 1 >= args.Length) { Console.Error.WriteLine("Missing value for --label-exp"); return 1; }
            labelExp = args[++i];
            break;
        default:
            Console.Error.WriteLine($"Unknown argument: {args[i]}");
            PrintUsage();
            return 1;
    }
}

// ── Load ──────────────────────────────────────────────────────────────────────
Console.Error.WriteLine($"Loading baseline:     {baselineSpec}");
Console.Error.WriteLine($"Loading experimental: {experimentalSpec}");

List<WpfPerfDiff.Models.AnalysisResult> baselineRuns;
List<WpfPerfDiff.Models.AnalysisResult> experimentalRuns;
try
{
    baselineRuns = AnalysisLoader.Load(baselineSpec);
    experimentalRuns = AnalysisLoader.Load(experimentalSpec);
}
catch (Exception ex)
{
    Console.Error.WriteLine($"[error] Failed to load inputs: {ex.Message}");
    return 2;
}

Console.Error.WriteLine($"Loaded {baselineRuns.Count} baseline run(s), {experimentalRuns.Count} experimental run(s).");

// ── Aggregate ─────────────────────────────────────────────────────────────────
string bLabel = labelBaseline ?? BuildLabel(baselineSpec, baselineRuns.Count);
string eLabel = labelExp ?? BuildLabel(experimentalSpec, experimentalRuns.Count);

var baselineAgg = Aggregator.Aggregate(baselineRuns, bLabel);
var experimentalAgg = Aggregator.Aggregate(experimentalRuns, eLabel);

// ── Diff ──────────────────────────────────────────────────────────────────────
var diff = Differ.Compute(baselineAgg, experimentalAgg, thresholdPct);

// ── Report ────────────────────────────────────────────────────────────────────
string report = ReportWriter.Write(diff);

if (outputPath != null)
{
    string? dir = Path.GetDirectoryName(outputPath);
    if (!string.IsNullOrEmpty(dir))
        Directory.CreateDirectory(dir);
    await File.WriteAllTextAsync(outputPath, report).ConfigureAwait(false);
    Console.Error.WriteLine($"Report written to: {outputPath}");
}
else
{
    Console.Write(report);
}

return 0;

// ── Helpers ───────────────────────────────────────────────────────────────────

static string BuildLabel(string spec, int count)
{
    var paths = spec.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
    string firstName = Path.GetFileNameWithoutExtension(paths[0]);
    return count == 1 ? firstName : $"{firstName} ({count} runs, median)";
}

static void PrintUsage()
{
    Console.Error.WriteLine("""
        wpf-perf-diff — A/B comparison of wpf-perf-analyze JSON outputs

        Usage:
          wpf-perf-diff <baseline> <experimental> [-o <report.md>] [--threshold-pct N]

        Arguments:
          <baseline>           Path to a single analyze JSON, or comma-separated list for multi-run.
          <experimental>       Path to a single analyze JSON, or comma-separated list for multi-run.
          -o <report.md>       Output path for the markdown report (default: stdout).
          --threshold-pct N    Flag metrics that change by more than N% (default: 5).
          --label-baseline L   Override the label for the baseline side.
          --label-exp L        Override the label for the experimental side.

        Examples:
          wpf-perf-diff base.json exp.json -o report.md
          wpf-perf-diff base1.json,base2.json,base3.json exp1.json,exp2.json,exp3.json -o report.md --threshold-pct 3
        """);
}
