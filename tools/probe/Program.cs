// Quick probe / analyze of a .nettrace or .etl file. Two output modes:
//   - default:   human-readable text dump
//   - --json <p>: writes AnalysisResult JSON suitable for wpf-perf-diff

using System.Text.Json;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Etlx;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Stacks;
using WpfPerfAnalyze.Models;

if (args.Length == 0)
{
    Console.Error.WriteLine("usage: nettrace-probe <file.nettrace|file.etl> [--top <n>] [--json <out.json>]");
    Console.Error.WriteLine("  For .gcdump files: open in PerfView ('File > Open' the .gcdump) or Visual Studio.");
    return 1;
}

string path = args[0];
int topN = 20;
string? jsonPath = null;
for (int i = 1; i < args.Length; i++)
{
    if (args[i] == "--top" && i + 1 < args.Length)
        topN = int.Parse(args[++i]);
    else if (args[i] == "--json" && i + 1 < args.Length)
        jsonPath = args[++i];
}

if (!File.Exists(path))
{
    Console.Error.WriteLine($"ERROR: file not found: {path}");
    return 2;
}

Console.WriteLine($"=== Probing: {path} ===");
Console.WriteLine($"=== Size: {new FileInfo(path).Length / 1024.0 / 1024.0:F2} MB ===");

// Convert to ETLX. Use the right helper based on extension.
string etlxPath;
if (path.EndsWith(".nettrace", StringComparison.OrdinalIgnoreCase))
{
    etlxPath = TraceLog.CreateFromEventPipeDataFile(path);
}
else
{
    etlxPath = TraceLog.CreateFromEventTraceLogFile(path);
}
Console.WriteLine($"=== ETLX: {etlxPath} ===");
using var traceLog = new TraceLog(etlxPath);

Console.WriteLine();
Console.WriteLine($"Sessions: start={traceLog.SessionStartTime:O} end={traceLog.SessionEndTime:O}");
Console.WriteLine($"Duration: {traceLog.SessionDuration.TotalSeconds:F1}s");
Console.WriteLine($"EventCount: {traceLog.EventCount:N0}");
Console.WriteLine($"LostEvents: {traceLog.EventsLost:N0}");
Console.WriteLine();

// Enumerate processes seen.
Console.WriteLine("=== Processes ===");
foreach (var p in traceLog.Processes.OrderBy(p => p.Name))
{
    if (string.IsNullOrEmpty(p.Name)) continue;
    Console.WriteLine($"  pid={p.ProcessID,6}  name={p.Name,-40}  cmd={p.CommandLine}");
}
Console.WriteLine();

// Modules loaded — proves which WPF assembly is in use.
Console.WriteLine("=== Loaded modules (matching wpf|presentation|windowsbase|xaml) ===");
foreach (var p in traceLog.Processes)
{
    if (p.Name?.Equals("MotionCatalyst-cli", StringComparison.OrdinalIgnoreCase) != true) continue;
    var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    foreach (var module in traceLog.ModuleFiles)
    {
        var name = module.Name ?? "";
        var fn = name.ToLowerInvariant();
        if (fn.Contains("wpf") || fn.Contains("presentation") || fn.Contains("windowsbase")
            || fn.Contains("system.xaml") || fn.Contains("uiautomation")
            || fn.Contains("initialforce.wpf"))
        {
            if (seen.Add(name))
                Console.WriteLine($"  {module.FilePath}");
        }
    }
}
Console.WriteLine();

// Tally events by provider.
var byProvider = new Dictionary<string, long>();
var byProviderEvent = new Dictionary<string, Dictionary<string, long>>();
var dotnetRuntimeEvents = new Dictionary<string, long>();
foreach (var ev in traceLog.Events)
{
    var provider = ev.ProviderName ?? "(null)";
    byProvider.TryGetValue(provider, out var c);
    byProvider[provider] = c + 1;

    if (!byProviderEvent.TryGetValue(provider, out var inner))
    {
        inner = new Dictionary<string, long>();
        byProviderEvent[provider] = inner;
    }
    var evName = ev.EventName ?? "(null)";
    inner.TryGetValue(evName, out var ec);
    inner[evName] = ec + 1;

    if (provider == "Microsoft-Windows-DotNETRuntime")
    {
        dotnetRuntimeEvents.TryGetValue(evName, out var dc);
        dotnetRuntimeEvents[evName] = dc + 1;
    }
}

// Dump DotNETRuntime event histogram for diagnostics.
Console.WriteLine();
Console.WriteLine("=== DotNETRuntime event histogram (full) ===");
foreach (var (k, c) in dotnetRuntimeEvents.OrderByDescending(p => p.Value))
{
    Console.WriteLine($"  {c,8:N0}  {k}");
}

Console.WriteLine($"=== Event totals by provider (top {topN}) ===");
foreach (var (prov, count) in byProvider.OrderByDescending(p => p.Value).Take(topN))
{
    Console.WriteLine($"  {count,10:N0}  {prov}");
}
Console.WriteLine();

// For each WPF or perf-harness provider, list event names.
Console.WriteLine("=== WPF / PerfHarness event detail ===");
foreach (var prov in byProvider.Keys.Where(p =>
    p.Contains("WPF", StringComparison.OrdinalIgnoreCase) ||
    p.Contains("PerfHarness", StringComparison.OrdinalIgnoreCase) ||
    p.Contains("Presentation", StringComparison.OrdinalIgnoreCase)))
{
    Console.WriteLine($"  Provider: {prov}");
    var inner = byProviderEvent[prov];
    foreach (var (ev, count) in inner.OrderByDescending(p => p.Value).Take(10))
    {
        Console.WriteLine($"    {count,8:N0}  {ev}");
    }
}
Console.WriteLine();

// CLR sampling profiler – walk stacks for top symbols.
Console.WriteLine("=== Top CPU samples — leaf method (CLR sample profiler) ===");
var leafCounts = new Dictionary<string, long>();
var inclusiveCounts = new Dictionary<string, long>();   // method appears anywhere in stack
var byModule = new Dictionary<string, long>();
long totalSamples = 0;

foreach (var ev in traceLog.Events)
{
    if (ev.ProviderName != "Microsoft-DotNETCore-SampleProfiler") continue;
    if (ev.EventName != "Thread/Sample" && ev.EventName != "Thread") continue;
    var cs = ev.CallStack();
    if (cs == null) continue;
    totalSamples++;

    // Leaf
    var leaf = cs.CodeAddress?.FullMethodName ?? "(unresolved)";
    if (string.IsNullOrEmpty(leaf)) leaf = $"0x{cs.CodeAddress?.Address:x}";
    leafCounts.TryGetValue(leaf, out var lc);
    leafCounts[leaf] = lc + 1;

    // Module
    var mod = cs.CodeAddress?.ModuleFile?.Name ?? "(no-mod)";
    byModule.TryGetValue(mod, out var mc);
    byModule[mod] = mc + 1;

    // Walk every frame
    var seen = new HashSet<string>();
    for (var frame = cs; frame != null; frame = frame.Caller)
    {
        var name = frame.CodeAddress?.FullMethodName;
        if (string.IsNullOrEmpty(name)) continue;
        if (seen.Add(name))
        {
            inclusiveCounts.TryGetValue(name, out var ic);
            inclusiveCounts[name] = ic + 1;
        }
    }
}

Console.WriteLine($"  Total samples captured: {totalSamples:N0}");
Console.WriteLine();
Console.WriteLine("  --- Top leaf methods ---");
foreach (var (name, count) in leafCounts.OrderByDescending(p => p.Value).Take(topN))
{
    var pct = totalSamples > 0 ? 100.0 * count / totalSamples : 0;
    Console.WriteLine($"  {count,6:N0} ({pct,5:F2}%)  {name}");
}
Console.WriteLine();
Console.WriteLine("  --- Top inclusive (method appears anywhere in stack) ---");
foreach (var (name, count) in inclusiveCounts.OrderByDescending(p => p.Value).Take(topN))
{
    var pct = totalSamples > 0 ? 100.0 * count / totalSamples : 0;
    Console.WriteLine($"  {count,6:N0} ({pct,5:F2}%)  {name}");
}
Console.WriteLine();
Console.WriteLine("=== Top modules (samples landed in) ===");
foreach (var (mod, count) in byModule.OrderByDescending(p => p.Value).Take(topN))
{
    var pct = totalSamples > 0 ? 100.0 * count / totalSamples : 0;
    Console.WriteLine($"  {count,6:N0} ({pct,5:F2}%)  {mod}");
}

// WPF-only filtered analysis: only samples whose stack contains WPF code.
Console.WriteLine();
Console.WriteLine("=== WPF-attributable samples ===");
long wpfSamples = 0;
var wpfLeaf = new Dictionary<string, long>();
var wpfInclusive = new Dictionary<string, long>();

foreach (var ev in traceLog.Events)
{
    if (ev.ProviderName != "Microsoft-DotNETCore-SampleProfiler") continue;
    if (ev.EventName != "Thread/Sample" && ev.EventName != "Thread") continue;
    var cs = ev.CallStack();
    if (cs == null) continue;

    bool hasWpf = false;
    string? leafName = null;
    var seen = new HashSet<string>();
    var localInclusive = new List<string>();

    for (var frame = cs; frame != null; frame = frame.Caller)
    {
        var name = frame.CodeAddress?.FullMethodName;
        if (string.IsNullOrEmpty(name)) continue;
        if (leafName == null) leafName = name;

        var modName = frame.CodeAddress?.ModuleFile?.Name?.ToLowerInvariant() ?? "";
        if (modName.Contains("presentation") || modName.Contains("windowsbase")
            || modName.Contains("system.xaml") || modName.Contains("uiautomation")
            || modName.Contains("initialforce.wpf"))
        {
            hasWpf = true;
        }

        if (seen.Add(name)) localInclusive.Add(name);
    }

    if (hasWpf)
    {
        wpfSamples++;
        if (leafName != null)
        {
            wpfLeaf.TryGetValue(leafName, out var lc);
            wpfLeaf[leafName] = lc + 1;
        }
        foreach (var name in localInclusive)
        {
            wpfInclusive.TryGetValue(name, out var c);
            wpfInclusive[name] = c + 1;
        }
    }
}

Console.WriteLine($"  WPF-touching samples: {wpfSamples:N0} ({(totalSamples > 0 ? 100.0 * wpfSamples / totalSamples : 0):F2}% of total)");
Console.WriteLine();
Console.WriteLine("  --- Top leaf methods on WPF-touching stacks ---");
foreach (var (name, count) in wpfLeaf.OrderByDescending(p => p.Value).Take(topN))
{
    var pct = wpfSamples > 0 ? 100.0 * count / wpfSamples : 0;
    Console.WriteLine($"  {count,6:N0} ({pct,5:F2}%)  {name}");
}
Console.WriteLine();
Console.WriteLine("  --- Top inclusive methods on WPF-touching stacks ---");
foreach (var (name, count) in wpfInclusive.OrderByDescending(p => p.Value)
    .Where(p => p.Key.StartsWith("System.Windows.", StringComparison.Ordinal) || p.Key.StartsWith("MS.", StringComparison.Ordinal)
                || p.Key.Contains("PresentationCore") || p.Key.Contains("WindowsBase")
                || p.Key.Contains("PresentationFramework"))
    .Take(topN))
{
    var pct = wpfSamples > 0 ? 100.0 * count / wpfSamples : 0;
    Console.WriteLine($"  {count,6:N0} ({pct,5:F2}%)  {name}");
}

// JSON output for diff/CI consumption.
if (jsonPath != null)
{
    var fileSize = new FileInfo(path).Length;
    var mcProcess = traceLog.Processes.FirstOrDefault(p =>
        p.Name?.Equals("MotionCatalyst-cli", StringComparison.OrdinalIgnoreCase) == true);

    long allocBytes = 0;
    int gen0 = 0, gen1 = 0, gen2 = 0, gcCount = 0;
    long lohBytes = 0, pohBytes = 0;
    long sohBytes = 0;
    int allocTickEvents = 0;
    var allocByType = new Dictionary<string, long>();
    double totalGcPause = 0;
    double maxGcPause = 0;
    double suspendStart = 0;
    int methodsJitted = 0;
    double totalJitMs = 0;
    int exceptionCount = 0;
    int contentionCount = 0;
    var exceptionByType = new Dictionary<string, int>();

    static long ToLong(object? o) => o switch
    {
        null => 0,
        long l => l,
        int i => i,
        uint u => u,
        ulong ul => (long)ul,
        short s => s,
        ushort us => us,
        byte b => b,
        sbyte sb => sb,
        _ => Convert.ToInt64(o),
    };

    // EventSource.Write<T>(...) packs an [EventData] struct into a single
    // anonymous payload field that TraceEvent surfaces as a TraceLogging
    // StructValue (essentially IDictionary<string,object>). PayloadByName
    // on the TraceEvent itself only sees the wrapper; we have to reach
    // into the struct's own keys to get ElementType / ElapsedMicros.
    static (string typeName, long elapsedMicros, long selfMicros) ExtractElementTiming(TraceEvent ev)
    {
        string typeName = "(unknown)";
        long elapsedMicros = 0;
        long selfMicros = 0;
        // EventSource.Write<T> wraps the [EventData] struct in a single
        // anonymous TraceLogging StructValue payload. Parsing the printed
        // form is pragmatic — TraceEvent's StructValue isn't an
        // IDictionary the way classic ETW manifests are.
        // Pretty-print: "StructValue { \"ElementType\":\"Path\",\"ElapsedMicros\":\"5244\",... }".
        for (int i = 0; i < ev.PayloadNames.Length; i++)
        {
            var raw = ev.PayloadValue(i);
            if (raw is null) continue;
            var s = raw.ToString();
            if (string.IsNullOrEmpty(s)) continue;
            typeName = ParseStringField(s, "ElementType") ?? typeName;
            elapsedMicros = ParseLongField(s, "ElapsedMicros") ?? elapsedMicros;
            selfMicros = ParseLongField(s, "SelfMicros") ?? selfMicros;
        }
        return (typeName, elapsedMicros, selfMicros);

        static string? ParseStringField(string s, string field)
        {
            var k = "\"" + field + "\":\"";
            var ki = s.IndexOf(k, StringComparison.Ordinal);
            if (ki < 0) return null;
            ki += k.Length;
            var ke = s.IndexOf('"', ki);
            return ke > ki ? s.Substring(ki, ke - ki) : null;
        }

        static long? ParseLongField(string s, string field)
        {
            var k = "\"" + field + "\":";
            var ki = s.IndexOf(k, StringComparison.Ordinal);
            if (ki < 0) return null;
            ki += k.Length;
            if (ki < s.Length && s[ki] == '"') ki++;
            int ke = ki;
            while (ke < s.Length && (char.IsDigit(s[ke]) || s[ke] == '-')) ke++;
            if (ke <= ki) return null;
            return long.TryParse(s.AsSpan(ki, ke - ki), out var v) ? v : null;
        }
    }

    static (double atMs, long total, long callbacks, long renderingEvent, long targets, long commit, int tickLoop, int regTargets)
        ExtractRenderFrameTiming(TraceEvent ev)
    {
        long total = 0, callbacks = 0, renderingEvent = 0, targets = 0, commit = 0;
        int tickLoop = 0, regTargets = 0;
        for (int i = 0; i < ev.PayloadNames.Length; i++)
        {
            var raw = ev.PayloadValue(i);
            if (raw is null) continue;
            var s = raw.ToString();
            if (string.IsNullOrEmpty(s)) continue;
            total = ParseLongField(s, "ElapsedMicros") ?? total;
            callbacks = ParseLongField(s, "CallbacksMicros") ?? callbacks;
            renderingEvent = ParseLongField(s, "RenderingEventMicros") ?? renderingEvent;
            targets = ParseLongField(s, "TargetsMicros") ?? targets;
            commit = ParseLongField(s, "CommitMicros") ?? commit;
            tickLoop = (int)(ParseLongField(s, "TickLoopCount") ?? tickLoop);
            regTargets = (int)(ParseLongField(s, "RegisteredTargets") ?? regTargets);
        }
        return (ev.TimeStampRelativeMSec, total, callbacks, renderingEvent, targets, commit, tickLoop, regTargets);

        static long? ParseLongField(string s, string field)
        {
            var k = "\"" + field + "\":";
            var ki = s.IndexOf(k, StringComparison.Ordinal);
            if (ki < 0) return null;
            ki += k.Length;
            if (ki < s.Length && s[ki] == '"') ki++;
            int ke = ki;
            while (ke < s.Length && (char.IsDigit(s[ke]) || s[ke] == '-')) ke++;
            if (ke <= ki) return null;
            return long.TryParse(s.AsSpan(ki, ke - ki), out var v) ? v : null;
        }
    }

    foreach (var ev in traceLog.Events)
    {
        var prov = ev.ProviderName ?? "";
        if (prov != "Microsoft-Windows-DotNETRuntime") continue;

        switch (ev.EventName)
        {
            case "GC/Start":
            {
                gcCount++;
                var depth = (int)ToLong(ev.PayloadByName("Depth"));
                if (depth == 0) gen0++;
                else if (depth == 1) gen1++;
                else if (depth == 2) gen2++;
                break;
            }
            case "GC/SuspendEEStart":
                suspendStart = ev.TimeStampRelativeMSec;
                break;
            case "GC/RestartEEStop":
                if (suspendStart > 0)
                {
                    var pause = ev.TimeStampRelativeMSec - suspendStart;
                    totalGcPause += pause;
                    if (pause > maxGcPause) maxGcPause = pause;
                    suspendStart = 0;
                }
                break;
            case "GC/AllocationTick":
            {
                allocTickEvents++;
                var amt64 = ToLong(ev.PayloadByName("AllocationAmount64"));
                if (amt64 == 0)
                    amt64 = ToLong(ev.PayloadByName("AllocationAmount"));
                allocBytes += amt64;

                // AllocationKind: Small=0, Large=1, Pinned=2 (in .NET 6+).
                var kind = (int)ToLong(ev.PayloadByName("AllocationKind"));
                if (kind == 1) lohBytes += amt64;
                else if (kind == 2) pohBytes += amt64;
                else sohBytes += amt64;

                var typeName = ev.PayloadByName("TypeName") as string ?? "(unknown)";
                allocByType.TryGetValue(typeName, out var tc);
                allocByType[typeName] = tc + amt64;
                break;
            }
            case "Method/JittingStarted":
            case "Method/JIT_Start":
                methodsJitted++;
                break;
            case "Exception/Start":
                exceptionCount++;
                var exType = ev.PayloadByName("ExceptionType") as string ?? "(unknown)";
                exceptionByType.TryGetValue(exType, out var c);
                exceptionByType[exType] = c + 1;
                break;
            case "Contention/Start":
                contentionCount++;
                break;
        }
    }

    Console.WriteLine();
    Console.WriteLine($"=== Allocation summary ===");
    Console.WriteLine($"  AllocationTick events: {allocTickEvents:N0}");
    Console.WriteLine($"  Total alloc bytes:     {allocBytes / 1024.0 / 1024.0:F2} MB");
    Console.WriteLine($"  SOH (small):           {sohBytes / 1024.0 / 1024.0:F2} MB");
    Console.WriteLine($"  LOH (large):           {lohBytes / 1024.0 / 1024.0:F2} MB");
    Console.WriteLine($"  POH (pinned):          {pohBytes / 1024.0 / 1024.0:F2} MB");
    Console.WriteLine($"  GC count: gen0={gen0} gen1={gen1} gen2={gen2}");
    Console.WriteLine($"  Total GC pause:        {totalGcPause:F1} ms");
    Console.WriteLine($"  Max GC pause:          {maxGcPause:F1} ms");
    Console.WriteLine($"  Exceptions thrown:     {exceptionCount}");
    Console.WriteLine($"  Contention events:     {contentionCount}");
    Console.WriteLine();
    Console.WriteLine($"  Top alloc types:");
    foreach (var (t, b) in allocByType.OrderByDescending(p => p.Value).Take(15))
    {
        Console.WriteLine($"    {b / 1024.0 / 1024.0,8:F2} MB  {t}");
    }

    // Top CPU methods (leaf-aggregated). We've already populated leafCounts.
    var topCpu = leafCounts.OrderByDescending(p => p.Value).Take(topN)
        .Select(p => new CpuMethodEntry
        {
            Method = p.Key,
            Module = "",
            Samples = (int)p.Value,
            CpuMs = p.Value, // 1 sample ~= 1ms by default sampler rate
        }).ToList();

    // Identify scenario from PerfHarnessEventSource events, if any.
    string scenarioName = "";
    double scenarioStart = 0, scenarioEnd = 0;
    var stepTimings = new Dictionary<string, (double start, double end)>();
    var idleDetections = new List<IdleDetection>();

    // Deep WPF instrumentation aggregates (events 7-13 in PerfHarnessEventSource).
    bool wpfInstrumented = false;
    var renderFrameMicros = new List<long>();
    var layoutPassMicros = new List<long>();
    var elementLoaded = new Dictionary<string, int>(StringComparer.Ordinal);
    int dispatcherOpCount = 0;
    long dispatcherOpTotalMicros = 0;
    int dispatcherIdleCount = 0;
    var presentationSnapshots = new List<PresentationSnapshot>();

    // Per-element slow Measure/Arrange aggregates (WPF fork's LayoutPerfTraceLogger).
    // Tracks both inclusive (incl. children) and exclusive (self) time.
    var measureSlow = new Dictionary<string, (int count, long total, long max, long self, long maxSelf)>(StringComparer.Ordinal);
    var arrangeSlow = new Dictionary<string, (int count, long total, long max, long self, long maxSelf)>(StringComparer.Ordinal);
    int measureSlowEventCount = 0;
    int arrangeSlowEventCount = 0;

    // Per-frame slow render aggregates (WPF fork's LayoutPerfTraceLogger.LogSlowRenderFrame).
    // Captures elapsed/callbacks/renderingEvent/targets/commit per frame so we can
    // see WHICH phase dominates the worst frame.
    var renderFrameSlow = new List<(double atMs, long total, long callbacks, long renderingEvent, long targets, long commit, int tickLoop, int regTargets)>();

    foreach (var ev in traceLog.Events)
    {
        if (ev.ProviderName == "Microsoft.DOTNET.WPF.PresentationCore")
        {
            switch (ev.EventName)
            {
                case "WpfLayoutMeasureSlow":
                {
                    var (t, us, selfUs) = ExtractElementTiming(ev);
                    measureSlow.TryGetValue(t, out var e);
                    measureSlow[t] = (e.count + 1, e.total + us, Math.Max(e.max, us),
                                      e.self + selfUs, Math.Max(e.maxSelf, selfUs));
                    measureSlowEventCount++;
                    wpfInstrumented = true;
                    break;
                }
                case "WpfLayoutArrangeSlow":
                {
                    var (t, us, selfUs) = ExtractElementTiming(ev);
                    arrangeSlow.TryGetValue(t, out var e);
                    arrangeSlow[t] = (e.count + 1, e.total + us, Math.Max(e.max, us),
                                      e.self + selfUs, Math.Max(e.maxSelf, selfUs));
                    arrangeSlowEventCount++;
                    wpfInstrumented = true;
                    break;
                }
                case "WpfRenderFrameSlow":
                {
                    var f = ExtractRenderFrameTiming(ev);
                    renderFrameSlow.Add(f);
                    wpfInstrumented = true;
                    break;
                }
            }
            continue;
        }
        if (ev.ProviderName != "MotionCatalyst-PerfHarness") continue;
        var ms = ev.TimeStampRelativeMSec;
        switch (ev.EventName)
        {
            case "ScenarioStart":
                scenarioName = ev.PayloadByName("name") as string ?? "";
                scenarioStart = ms;
                break;
            case "ScenarioEnd":
                scenarioEnd = ms;
                break;
            case "StepStart":
                {
                    var n = ev.PayloadByName("name") as string ?? "";
                    if (!string.IsNullOrEmpty(n))
                        stepTimings[n] = (ms, 0);
                }
                break;
            case "StepEnd":
                {
                    var n = ev.PayloadByName("name") as string ?? "";
                    if (stepTimings.TryGetValue(n, out var t))
                        stepTimings[n] = (t.start, ms);
                }
                break;
            case "IdleDetected":
                idleDetections.Add(new IdleDetection
                {
                    Step = ev.PayloadByName("step") as string ?? "",
                    AtMs = ms,
                });
                break;
            case "WpfRenderFrameTick":
                wpfInstrumented = true;
                renderFrameMicros.Add(ToLong(ev.PayloadByName("microsSinceLast")));
                break;
            case "WpfLayoutUpdated":
                wpfInstrumented = true;
                layoutPassMicros.Add(ToLong(ev.PayloadByName("microsSinceLast")));
                break;
            case "WpfElementLoaded":
                wpfInstrumented = true;
                {
                    var t = ev.PayloadByName("typeName") as string ?? "";
                    if (!string.IsNullOrEmpty(t))
                        elementLoaded[t] = elementLoaded.GetValueOrDefault(t) + 1;
                }
                break;
            case "WpfDispatcherIdle":
                wpfInstrumented = true;
                dispatcherIdleCount++;
                break;
            case "WpfDispatcherOperationEnd":
                wpfInstrumented = true;
                dispatcherOpCount++;
                dispatcherOpTotalMicros += ToLong(ev.PayloadByName("elapsedMicros"));
                break;
            case "WpfPresentationModeSnapshot":
                wpfInstrumented = true;
                {
                    long pa = ToLong(ev.PayloadByName("packedA"));
                    long pb = ToLong(ev.PayloadByName("packedB"));
                    presentationSnapshots.Add(new PresentationSnapshot
                    {
                        FrameNumber = (int)(pa & 0xFFFFFFFFL),
                        AnimationRenderRate = (int)((pa >> 32) & 0xFFFFFFFFL),
                        DisplayRefreshRate = (int)(pb & 0xFFFFL),
                        InterlockState = (int)((pb >> 16) & 0xFFFFL),
                        LastPresentationResults = (int)((pb >> 32) & 0xFFFFFFFFL),
                    });
                }
                break;
        }
    }

    var result = new AnalysisResult
    {
        EtlPath = path,
        FileSizeBytes = fileSize,
        CaptureSpanMs = traceLog.SessionDuration.TotalMilliseconds,
        Process = mcProcess == null ? null : new ProcessInfo
        {
            Name = mcProcess.Name ?? "",
            Pid = mcProcess.ProcessID,
            ExeVersion = "",
            WallClockMs = (mcProcess.EndTimeRelativeMsec - mcProcess.StartTimeRelativeMsec),
            CpuTimeMs = mcProcess.CPUMSec,
            UserCpuMs = 0,
            KernelCpuMs = 0,
        },
        Gc = new GcMetrics
        {
            TotalAllocBytes = allocBytes,
            TotalGcCount = gcCount,
            Gen0Count = gen0,
            Gen1Count = gen1,
            Gen2Count = gen2,
            TotalPauseTimeMs = totalGcPause,
            MaxPauseTimeMs = maxGcPause,
            LohAllocBytes = lohBytes,
            PohAllocBytes = pohBytes,
        },
        TopAllocators = allocByType
            .OrderByDescending(p => p.Value)
            .Take(topN)
            .Select(p => new AllocEntry
            {
                Method = p.Key,
                Module = "",
                AllocBytes = p.Value,
                AllocCount = 0,
            })
            .ToList(),
        Jit = new JitMetrics
        {
            MethodsJitted = methodsJitted,
            TotalJitTimeMs = totalJitMs,
        },
        Wpf = BuildWpfMetrics(
            wpfInclusive,
            wpfInstrumented,
            renderFrameMicros,
            layoutPassMicros,
            elementLoaded,
            dispatcherOpCount,
            dispatcherOpTotalMicros,
            dispatcherIdleCount,
            measureSlow,
            arrangeSlow,
            measureSlowEventCount,
            arrangeSlowEventCount,
            renderFrameSlow,
            presentationSnapshots),
        TopCpuMethods = topCpu,
        PerfHarnessEvents = string.IsNullOrEmpty(scenarioName) ? null : new PerfHarnessMetrics
        {
            Scenario = scenarioName,
            ScenarioStartTimestampMs = scenarioStart,
            ScenarioEndTimestampMs = scenarioEnd,
            StepTimings = stepTimings.Select(p => new StepTiming
            {
                Name = p.Key,
                StartMs = p.Value.start,
                EndMs = p.Value.end,
                ElapsedMs = p.Value.end - p.Value.start,
            }).OrderBy(s => s.StartMs).ToList(),
            IdleDetections = idleDetections,
        },
        Exceptions = exceptionCount == 0 ? null : new ExceptionMetrics
        {
            TotalCount = exceptionCount,
            ByType = exceptionByType
                .OrderByDescending(p => p.Value)
                .Take(20)
                .Select(p => new ExceptionTypeEntry { Type = p.Key, Count = p.Value })
                .ToList(),
        },
        AnalysisWarnings = traceLog.EventsLost > 0
            ? [$"{traceLog.EventsLost} events lost during capture"]
            : [],
    };

    var json = JsonSerializer.Serialize(result, new JsonSerializerOptions
    {
        WriteIndented = true,
    });
    File.WriteAllText(jsonPath, json);
    Console.WriteLine();
    Console.WriteLine($"=== JSON written: {jsonPath} ({new FileInfo(jsonPath).Length} bytes) ===");
}

return 0;

static WpfMetrics BuildWpfMetrics(
    Dictionary<string, long> wpfInclusive,
    bool instrumented,
    List<long> renderFrameMicros,
    List<long> layoutPassMicros,
    Dictionary<string, int> elementLoaded,
    int dispatcherOpCount,
    long dispatcherOpTotalMicros,
    int dispatcherIdleCount,
    Dictionary<string, (int count, long total, long max, long self, long maxSelf)> measureSlow,
    Dictionary<string, (int count, long total, long max, long self, long maxSelf)> arrangeSlow,
    int measureSlowEventCount,
    int arrangeSlowEventCount,
    List<(double atMs, long total, long callbacks, long renderingEvent, long targets, long commit, int tickLoop, int regTargets)> renderFrameSlow,
    List<PresentationSnapshot> presentationSnapshots)
{
    var w = new WpfMetrics
    {
        Instrumented = instrumented,
        DispatcherOpCount = dispatcherOpCount,
        DispatcherOpTotalMs = dispatcherOpTotalMicros / 1000.0,
        DispatcherIdleCount = dispatcherIdleCount,
        TopElementsLoaded = elementLoaded
            .OrderByDescending(p => p.Value)
            .Take(20)
            .Select(p => new ElementLoadEntry { TypeName = p.Key, Count = p.Value })
            .ToList(),
        MeasureSlowEventCount = measureSlowEventCount,
        ArrangeSlowEventCount = arrangeSlowEventCount,
        RenderFrameSlowEventCount = renderFrameSlow.Count,
        RenderFrameSlowMaxMs = renderFrameSlow.Count == 0 ? 0 : renderFrameSlow.Max(f => f.total) / 1000.0,
        RenderFrameSlowSumMs = renderFrameSlow.Sum(f => f.total) / 1000.0,
        TopRenderFrameSlow = renderFrameSlow
            .OrderByDescending(f => f.total)
            .Take(20)
            .Select(f => new RenderFrameSlowEntry
            {
                AtMs = f.atMs,
                ElapsedMs = f.total / 1000.0,
                CallbacksMs = f.callbacks / 1000.0,
                RenderingEventMs = f.renderingEvent / 1000.0,
                TargetsMs = f.targets / 1000.0,
                CommitMs = f.commit / 1000.0,
                TickLoopCount = f.tickLoop,
                RegisteredTargets = f.regTargets,
            })
            .ToList(),
        TopMeasureSlow = measureSlow
            .OrderByDescending(p => p.Value.self)
            .Take(20)
            .Select(p => new ElementLayoutTimingEntry
            {
                TypeName = p.Key,
                Count = p.Value.count,
                TotalMicros = p.Value.total,
                MaxMicros = p.Value.max,
                SelfMicros = p.Value.self,
                MaxSelfMicros = p.Value.maxSelf,
            })
            .ToList(),
        TopArrangeSlow = arrangeSlow
            .OrderByDescending(p => p.Value.self)
            .Take(20)
            .Select(p => new ElementLayoutTimingEntry
            {
                TypeName = p.Key,
                Count = p.Value.count,
                TotalMicros = p.Value.total,
                MaxMicros = p.Value.max,
                SelfMicros = p.Value.self,
                MaxSelfMicros = p.Value.maxSelf,
            })
            .ToList(),
    };

    if (instrumented)
    {
        w.LayoutPassCount = layoutPassMicros.Count;
        w.LayoutPassTotalMs = layoutPassMicros.Sum() / 1000.0;
        w.LayoutPassMaxMs = layoutPassMicros.Count == 0 ? 0 : layoutPassMicros.Max() / 1000.0;
        w.RenderPassCount = renderFrameMicros.Count;
        w.RenderPassTotalMs = renderFrameMicros.Sum() / 1000.0;
        w.RenderFrameMaxMs = renderFrameMicros.Count == 0 ? 0 : renderFrameMicros.Max() / 1000.0;
        w.RenderFrameP50Ms = Percentile(renderFrameMicros, 0.50) / 1000.0;
        w.RenderFrameP95Ms = Percentile(renderFrameMicros, 0.95) / 1000.0;
        w.RenderFrameP99Ms = Percentile(renderFrameMicros, 0.99) / 1000.0;
        w.MissedFrames16Count = renderFrameMicros.Count(m => m > 16700);
        w.MissedFrames33Count = renderFrameMicros.Count(m => m > 33333);
        w.MissedFrames50Count = renderFrameMicros.Count(m => m > 50000);
        w.BamlLoadCount = elementLoaded.Values.Sum();

        // Steady-state subset: skip the first 30 frames (warmup, JIT, layout cascade,
        // VBlank channel registration race, etc.). The remaining samples reflect
        // user-perceived frame cadence in the DWM-locked steady state.
        const int skipFrames = 30;
        if (renderFrameMicros.Count > skipFrames)
        {
            var ss = renderFrameMicros.Skip(skipFrames).ToList();
            w.SteadyState = new SteadyStateMetrics
            {
                SkippedFrames = skipFrames,
                RenderPassCount = ss.Count,
                RenderFrameP50Ms = Percentile(ss, 0.50) / 1000.0,
                RenderFrameP95Ms = Percentile(ss, 0.95) / 1000.0,
                RenderFrameP99Ms = Percentile(ss, 0.99) / 1000.0,
                RenderFrameMaxMs = ss.Max() / 1000.0,
                MissedFrames16Count = ss.Count(m => m > 16700),
                MissedFrames33Count = ss.Count(m => m > 33333),
                MissedFrames50Count = ss.Count(m => m > 50000),
            };
        }

        w.PresentationSnapshots = presentationSnapshots;
    }
    else
    {
        // Fall back to CPU-sample inclusive counts as a coarse proxy.
        w.LayoutPassCount = (int)wpfInclusive.GetValueOrDefault(
            "System.Windows.ContextLayoutManager.UpdateLayout()");
        w.RenderPassCount = (int)wpfInclusive.GetValueOrDefault(
            "System.Windows.Media.MediaContext.RenderMessageHandlerCore(class System.Object)");
    }

    return w;
}

static double Percentile(List<long> samples, double p)
{
    if (samples.Count == 0) return 0;
    var sorted = samples.OrderBy(x => x).ToList();
    var idx = (int)Math.Clamp(Math.Round(p * (sorted.Count - 1)), 0, sorted.Count - 1);
    return sorted[idx];
}
