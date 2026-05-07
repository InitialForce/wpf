// AllocParser — AllocationTick attribution from .nettrace files.
//
// Usage:
//   AllocParser.exe <nettrace-path> --output <json-path> [--top N]
//
// Opens a .nettrace (or pre-converted .etlx) via TraceLog.OpenOrConvert,
// walks every AllocationTick event emitted by the CLR provider, and
// attributes each allocation's bytes to every frame in its call stack
// (inclusive attribution — same logic as profile.py's aggregate_speedscope).
// The result is filtered to WPF namespaces, sorted descending by bytes,
// and written as a compact JSON array:
//   [{"frame": "<full method name>", "alloc_bytes": N}, ...]
//
// WPF filter mirrors WPF_NAMESPACE_PATTERNS / WPF_MODULE_NAMES in
// autoresearch/profile.py — keep both in sync when editing namespaces.
//
// AllocationTick noise floor: CLR fires one event per ~100 KB of allocation.
// Methods that allocate < 100 KB total in the trace window may be invisible.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Etlx;
using Microsoft.Diagnostics.Tracing.Parsers.Clr;

namespace AllocParser;

internal static class Program
{
    // ── WPF filter ────────────────────────────────────────────────────────────
    // Mirrors WPF_NAMESPACE_PATTERNS in autoresearch/profile.py.
    // Keep in sync when adding new namespaces.
    private static readonly string[] WpfNamespacePrefixes =
    [
        "System.Windows.",
        "MS.Internal.",
        "System.Xaml.",
    ];

    // Mirrors WPF_MODULE_NAMES in autoresearch/profile.py.
    // Used for frames in Module!Method form where the namespace is absent.
    private static readonly string[] WpfModuleNames =
    [
        "PresentationCore",
        "PresentationFramework",
        "WindowsBase",
        "System.Xaml",
        "PresentationUI",
        "ReachFramework",
    ];

    // ── Entry point ───────────────────────────────────────────────────────────

    internal static int Main(string[] args)
    {
        string? tracePath = null;
        string? outputPath = null;
        int topN = int.MaxValue;

        // Parse args
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--output" when i + 1 < args.Length:
                    outputPath = args[++i];
                    break;
                case "--top" when i + 1 < args.Length:
                    if (!int.TryParse(args[++i], out topN) || topN <= 0)
                    {
                        Console.Error.WriteLine($"[alloc-parser] Invalid --top value: {args[i]}");
                        return 1;
                    }
                    break;
                case "--help":
                case "-h":
                    PrintUsage();
                    return 0;
                default:
                    if (tracePath == null && !args[i].StartsWith("--", StringComparison.Ordinal))
                        tracePath = args[i];
                    else
                    {
                        Console.Error.WriteLine($"[alloc-parser] Unknown argument: {args[i]}");
                        PrintUsage();
                        return 1;
                    }
                    break;
            }
        }

        if (tracePath == null || outputPath == null)
        {
            Console.Error.WriteLine("[alloc-parser] Missing required arguments.");
            PrintUsage();
            return 1;
        }

        if (!File.Exists(tracePath))
        {
            Console.Error.WriteLine($"[alloc-parser] Trace file not found: {tracePath}");
            return 1;
        }

        // Run
        try
        {
            var frames = ParseAllocations(tracePath, topN);
            WriteJson(outputPath, frames);
            Console.WriteLine($"[alloc-parser] Wrote {frames.Count} WPF frames → {outputPath}");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[alloc-parser] Fatal: {ex.Message}");
            Console.Error.WriteLine(ex.StackTrace);
            return 1;
        }
    }

    // ── Core logic ────────────────────────────────────────────────────────────

    private static List<AllocFrame> ParseAllocations(string tracePath, int topN)
    {
        // Convert .nettrace → .etlx if needed; TraceLog handles both extensions.
        // OpenOrConvert writes a .etlx sidecar next to the source file the first
        // time, and reuses it on subsequent runs.
        Console.WriteLine($"[alloc-parser] Opening trace: {tracePath}");
        string etlxPath = tracePath.EndsWith(".etlx", StringComparison.OrdinalIgnoreCase)
            ? tracePath
            : TraceLog.CreateFromEventPipeDataFile(tracePath);

        Console.WriteLine($"[alloc-parser] Reading ETLX: {etlxPath}");

        // Accumulate inclusive alloc_bytes per frame name.
        // Key: full frame name string. Value: total bytes charged inclusively.
        var byFrame = new Dictionary<string, long>(StringComparer.Ordinal);
        int eventCount = 0;

        using (var log = new TraceLog(etlxPath))
        {
            // TraceLog.Events is a TraceEvents (enumerable), not a TraceEventSource.
            // To use event parsers, convert to a TraceEventDispatcher via GetSource().
            var source = log.Events.GetSource();
            var clrParser = new Microsoft.Diagnostics.Tracing.Parsers.ClrTraceEventParser(source);

            clrParser.GCAllocationTick += ev =>
            {
                eventCount++;
                long amount = ev.AllocationAmount64 > 0 ? ev.AllocationAmount64 : ev.AllocationAmount;
                if (amount <= 0)
                    return;

                // Walk call stack — charge amount to every unique frame name
                // (inclusive attribution, same logic as profile.py aggregate_speedscope).
                var callStack = ev.CallStack();
                if (callStack == null)
                    return;

                var seen = new HashSet<string>(StringComparer.Ordinal);
                var frame = callStack;
                while (frame != null)
                {
                    var codeAddr = frame.CodeAddress;
                    string name = codeAddr.FullMethodName;
                    if (!string.IsNullOrEmpty(name) && seen.Add(name))
                    {
                        byFrame.TryGetValue(name, out long prev);
                        byFrame[name] = prev + amount;
                    }
                    frame = frame.Caller;
                }
            };

            source.Process();
        }

        Console.WriteLine($"[alloc-parser] Processed {eventCount} AllocationTick event(s); {byFrame.Count} unique frames.");

        // Filter to WPF namespaces only.
        var wpfFrames = byFrame
            .Where(kv => IsWpfFrame(kv.Key))
            .OrderByDescending(kv => kv.Value)
            .Take(topN)
            .Select(kv => new AllocFrame(kv.Key, kv.Value))
            .ToList();

        Console.WriteLine($"[alloc-parser] {wpfFrames.Count} WPF frame(s) after filter.");
        return wpfFrames;
    }

    // ── WPF filter ────────────────────────────────────────────────────────────

    private static bool IsWpfFrame(string frameName)
    {
        // Namespace prefix match (mirrors is_wpf_method in profile.py).
        foreach (var ns in WpfNamespacePrefixes)
        {
            if (frameName.StartsWith(ns, StringComparison.Ordinal))
                return true;
        }

        // Module!Method form — check module name (mirrors WPF_MODULE_NAMES in profile.py).
        int bang = frameName.IndexOf('!');
        if (bang > 0)
        {
            string module = frameName[..bang];
            foreach (var mod in WpfModuleNames)
            {
                if (string.Equals(module, mod, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
        }

        return false;
    }

    // ── JSON output ───────────────────────────────────────────────────────────

    private static void WriteJson(string outputPath, List<AllocFrame> frames)
    {
        string? dir = Path.GetDirectoryName(outputPath);
        if (!string.IsNullOrEmpty(dir))
            Directory.CreateDirectory(dir);

        var options = new JsonSerializerOptions { WriteIndented = false };
        string json = JsonSerializer.Serialize(frames, options);

        // Use UTF-8 without BOM so Python json.load() can read it directly.
        File.WriteAllText(outputPath, json, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private static void PrintUsage()
    {
        Console.WriteLine("Usage: AllocParser.exe <nettrace-path> --output <json-path> [--top N]");
        Console.WriteLine();
        Console.WriteLine("  <nettrace-path>   Path to .nettrace or .etlx file");
        Console.WriteLine("  --output <path>   JSON output file path");
        Console.WriteLine("  --top N           Truncate to top N frames (optional)");
        Console.WriteLine();
        Console.WriteLine("Output JSON: [{\"frame\": \"...\", \"alloc_bytes\": N}, ...]");
        Console.WriteLine("Writes [] if no WPF AllocationTick frames found. Exits 0.");
    }
}

// ── Data model ────────────────────────────────────────────────────────────────

internal sealed record AllocFrame(
    [property: System.Text.Json.Serialization.JsonPropertyName("frame")]
    string Frame,
    [property: System.Text.Json.Serialization.JsonPropertyName("alloc_bytes")]
    long AllocBytes
);
