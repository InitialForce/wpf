using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Events;
using Microsoft.Windows.EventTracing.Metadata;
using Microsoft.Windows.EventTracing.Symbols;
using WpfPerfAnalyze.Analyzers;
using WpfPerfAnalyze.Models;

if (args.Length == 0 || args[0] is "-h" or "--help")
{
    Console.WriteLine("wpf-perf-analyze — ETL analysis tool for MotionCatalyst perf runs");
    Console.WriteLine();
    Console.WriteLine("Usage:");
    Console.WriteLine("  wpf-perf-analyze <etl-path> -o <json-path> [options]");
    Console.WriteLine();
    Console.WriteLine("Options:");
    Console.WriteLine("  -o, --output <path>        Output JSON file path (required)");
    Console.WriteLine("  --process-name <name>      Target process name (default: MotionCatalyst-cli.exe)");
    Console.WriteLine("  --top-n <n>                Top-N methods/allocators (default: 50)");
    Console.WriteLine("  --baseline-mode            Alias for --top-n 100 (no functional difference)");
    Console.WriteLine("  --allow-lost-events        Continue if ETL has lost events");
    Console.WriteLine();
    Console.WriteLine("Exit codes: 0=success, 1=usage error, 2=analysis error");
    return 0;
}

// Parse arguments
string? etlPath = null;
string? outputPath = null;
string processName = "MotionCatalyst-cli.exe";
int topN = 50;
bool allowLostEvents = false;
bool baselineMode = false;

for (int i = 0; i < args.Length; i++)
{
    switch (args[i])
    {
        case "-o" or "--output":
            outputPath = args[++i];
            break;
        case "--process-name":
            processName = args[++i];
            break;
        case "--top-n":
            topN = int.Parse(args[++i]);
            break;
        case "--baseline-mode":
            baselineMode = true;
            break;
        case "--allow-lost-events":
            allowLostEvents = true;
            break;
        default:
            if (!args[i].StartsWith('-') && etlPath == null)
                etlPath = args[i];
            else
            {
                Console.Error.WriteLine($"Unknown argument: {args[i]}");
                return 1;
            }
            break;
    }
}

if (baselineMode && topN == 50)
    topN = 100;

if (etlPath == null)
{
    Console.Error.WriteLine("Error: ETL file path is required.");
    return 1;
}

if (outputPath == null)
{
    Console.Error.WriteLine("Error: -o <output-path> is required.");
    return 1;
}

if (!File.Exists(etlPath))
{
    Console.Error.WriteLine($"Error: ETL file not found: {etlPath}");
    return 2;
}

Console.WriteLine($"Analyzing: {etlPath}");
Console.WriteLine($"Target process: {processName}");
Console.WriteLine($"Top-N: {topN}");
Console.WriteLine();

var result = new AnalysisResult
{
    EtlPath = Path.GetFullPath(etlPath),
    FileSizeBytes = new FileInfo(etlPath).Length,
};

try
{
    AnalyzeEtl(etlPath, result, processName, topN, allowLostEvents);
}
catch (InvalidTraceDataException ex)
{
    Console.Error.WriteLine($"Trace data error: {ex.Message}");
    Console.Error.WriteLine("Try --allow-lost-events if the trace has dropped events.");
    return 2;
}
catch (InvalidTraceFormatException ex)
{
    Console.Error.WriteLine($"Trace format error: {ex.Message}");
    return 2;
}

// Write output
var jsonOptions = new JsonSerializerOptions
{
    WriteIndented = true,
    DefaultIgnoreCondition = JsonIgnoreCondition.Never,
};

string outputDir = Path.GetDirectoryName(outputPath) ?? ".";
Directory.CreateDirectory(outputDir);

var json = JsonSerializer.Serialize(result, jsonOptions);
File.WriteAllText(outputPath, json);

Console.WriteLine($"Output written to: {outputPath}");
Console.WriteLine($"Warnings: {result.AnalysisWarnings.Count}");
foreach (var w in result.AnalysisWarnings)
    Console.WriteLine($"  [warn] {w}");

return 0;

static void AnalyzeEtl(
    string etlPath,
    AnalysisResult result,
    string processName,
    int topN,
    bool allowLostEvents)
{
    var settings = new TraceProcessorSettings
    {
        AllowLostEvents = allowLostEvents,
        AllowTimeInversion = true,
        SuppressFirstTimeSetupMessage = true,
    };

    using var trace = TraceProcessor.Create(etlPath, settings);

    // Register all data sources before calling Process()
    ITraceMetadata metadata = trace.UseMetadata();
    var pendingProcesses = trace.UseProcesses();
    var pendingCpuSampling = trace.UseCpuSamplingData();
    var pendingCpuScheduling = trace.UseCpuSchedulingData();
    // UseGenericEvents with no provider filter = all providers
    var pendingGenericEvents = trace.UseGenericEvents(new GenericEventSettings());

    // Optionally load symbols (uses _NT_SYMBOL_PATH env var if set)
    IPendingResult<ISymbolDataSource>? pendingSymbols = null;
    try
    {
        pendingSymbols = trace.UseSymbols();
    }
    catch (Exception ex)
    {
        result.AnalysisWarnings.Add($"Symbol loading skipped: {ex.Message}");
    }

    Console.WriteLine("Processing trace...");
    trace.Process();

    // Load symbols if available
    if (pendingSymbols?.HasResult == true)
    {
        try
        {
            var symbolTask = SymbolDataSourceExtensions.LoadSymbolsForConsoleAsync(
                pendingSymbols.Result,
                SymCachePath.Automatic);
            symbolTask.GetAwaiter().GetResult();
            Console.WriteLine("Symbols loaded.");
        }
        catch (Exception ex)
        {
            result.AnalysisWarnings.Add($"Symbol resolution failed: {ex.Message}. Methods will show as '???'.");
        }
    }
    else
    {
        result.AnalysisWarnings.Add("Symbol data not available — CPU method names will show as '???'.");
    }

    // Compute capture span from trace metadata (StartTime/StopTime are DateTimeOffset)
    try
    {
        var startTs = metadata.FirstAnalyzerDisplayedEventTime;
        var stopTs = metadata.LastEventTime;
        // TotalMilliseconds returns decimal for TraceTimestamp — cast to double
        result.CaptureSpanMs = Math.Max(0, (double)(stopTs.TotalMilliseconds - startTs.TotalMilliseconds));
        if (result.CaptureSpanMs == 0)
            result.CaptureSpanMs = (double)metadata.AnalyzerDisplayedDuration.TotalMilliseconds;
    }
    catch (Exception ex)
    {
        result.AnalysisWarnings.Add($"Could not compute capture span: {ex.Message}");
    }

    // Find target process ID (needed by all analyzers)
    int targetPid = 0;
    if (pendingProcesses.HasResult)
    {
        var procEntry = pendingProcesses.Result.Processes
            .Where(p => p.ImageName?.Contains(
                Path.GetFileNameWithoutExtension(processName),
                StringComparison.OrdinalIgnoreCase) == true)
            .OrderByDescending(p => p.Duration.HasValue ? (double)p.Duration.Value.TotalMilliseconds : 0)
            .FirstOrDefault();
        targetPid = procEntry?.Id ?? 0;

        if (targetPid == 0)
            result.AnalysisWarnings.Add($"Target process '{processName}' not found — analyzers will attempt to work without PID filter.");
    }

    Console.WriteLine($"Target PID: {(targetPid > 0 ? targetPid.ToString() : "not found")}");
    Console.WriteLine("Running analyzers...");

    // Process
    var processAnalyzer = new ProcessAnalyzer(processName);
    result.Process = processAnalyzer.Analyze(pendingProcesses, pendingCpuScheduling, result.AnalysisWarnings);

    // GC
    result.Gc = GcAnalyzer.Analyze(pendingGenericEvents, targetPid, result.AnalysisWarnings);

    // JIT
    result.Jit = JitAnalyzer.Analyze(pendingGenericEvents, targetPid, result.AnalysisWarnings);

    // WPF events
    result.Wpf = WpfEventAnalyzer.Analyze(pendingGenericEvents, targetPid, result.AnalysisWarnings);

    // CPU sampling (top-N methods)
    var cpuAnalyzer = new CpuSamplingAnalyzer(topN);
    result.TopCpuMethods = cpuAnalyzer.Analyze(pendingCpuSampling, targetPid, result.AnalysisWarnings);

    // Allocation top-N
    var allocAnalyzer = new AllocAnalyzer(topN);
    result.TopAllocators = allocAnalyzer.Analyze(pendingGenericEvents, targetPid, result.AnalysisWarnings);

    // PerfHarness events
    result.PerfHarnessEvents = PerfHarnessAnalyzer.Analyze(pendingGenericEvents, targetPid, result.AnalysisWarnings);

    // Exceptions
    result.Exceptions = ExceptionAnalyzer.Analyze(pendingGenericEvents, targetPid);
}
