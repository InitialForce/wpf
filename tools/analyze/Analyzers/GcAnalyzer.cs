using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Events;
using WpfPerfAnalyze.Models;

namespace WpfPerfAnalyze.Analyzers;

/// <summary>
/// Extracts GC metrics from Microsoft-Windows-DotNETRuntime generic events.
///
/// Event IDs used (from the CLR event manifest):
///   GCStart_V2        = 1   (keywords: GC=0x1)
///   GCSuspendEE_V1    = 9   — stop-the-world begin
///   GCRestartEE_V2    = 3   — stop-the-world end
///   GCAllocationTick  = 10  — fires every ~100 KB allocated
///
/// NOTE: Detailed alloc tracking requires the GCAllocHigh / GCAllocLow
/// keywords which are expensive. The standard "AllocationTick" (every ~100KB)
/// is used to estimate totalAllocBytes.  The field "AllocationAmount64" (v2)
/// or "AllocationAmount" (v1) carries the bytes so far in that chunk.
///
/// LOH/POH allocation is indicated by AllocationKind field (1=large, 2=pinned).
/// </summary>
internal sealed class GcAnalyzer
{
    // Microsoft-Windows-DotNETRuntime provider GUID
    private static readonly Guid s_dotNetRuntimeProviderId =
        new Guid("e13c0d23-ccbc-4e12-931b-d9cc2eee27e4");

    // Event IDs
    private const int EventIdGcStart = 1;
    private const int EventIdGcRestartEE = 3;
    private const int EventIdGcSuspendEE = 9;
    private const int EventIdGcAllocationTick = 10;

    public static GcMetrics Analyze(
        IPendingResult<IGenericEventDataSource> genericEventsPending,
        int targetPid,
        List<string> warnings)
    {
        var metrics = new GcMetrics();

        if (!genericEventsPending.HasResult)
        {
            warnings.Add("GenericEventDataSource not available — no GC/JIT data.");
            return metrics;
        }

        var events = genericEventsPending.Result.Events;

        // Track suspend start times keyed by suspend reason
        var suspendStarts = new Dictionary<int, double>();
        double totalPauseMs = 0;
        double maxPauseMs = 0;
        long totalAllocBytes = 0;
        long lohAllocBytes = 0;
        long pohAllocBytes = 0;

        foreach (var evt in events)
        {
            if (evt.ProviderId != s_dotNetRuntimeProviderId)
                continue;
            if (evt.ProcessId != targetPid)
                continue;

            switch (evt.Id)
            {
                case EventIdGcStart:
                    HandleGcStart(evt, metrics);
                    break;

                case EventIdGcSuspendEE:
                    {
                        double tsMs = (double)evt.Timestamp.TotalMilliseconds;
                        int key = GetFieldInt32(evt, "Reason", 0);
                        suspendStarts[key] = tsMs;
                        break;
                    }

                case EventIdGcRestartEE:
                    {
                        double tsMs = (double)evt.Timestamp.TotalMilliseconds;
                        if (suspendStarts.Count > 0)
                        {
                            var firstKey = suspendStarts.Keys.First();
                            double startMs = suspendStarts[firstKey];
                            suspendStarts.Remove(firstKey);
                            double pauseMs = tsMs - startMs;
                            if (pauseMs > 0)
                            {
                                totalPauseMs += pauseMs;
                                if (pauseMs > maxPauseMs)
                                    maxPauseMs = pauseMs;
                            }
                        }
                        break;
                    }

                case EventIdGcAllocationTick:
                    {
                        long amount = GetFieldInt64(evt, "AllocationAmount64",
                                        GetFieldInt32(evt, "AllocationAmount", 0));
                        totalAllocBytes += amount;

                        int allocKind = GetFieldInt32(evt, "AllocationKind", 0);
                        if (allocKind == 1) lohAllocBytes += amount;
                        else if (allocKind == 2) pohAllocBytes += amount;
                        break;
                    }
            }
        }

        metrics.TotalPauseTimeMs = totalPauseMs;
        metrics.MaxPauseTimeMs = maxPauseMs;
        metrics.TotalAllocBytes = totalAllocBytes;
        metrics.LohAllocBytes = lohAllocBytes;
        metrics.PohAllocBytes = pohAllocBytes;

        if (metrics.TotalGcCount == 0)
            warnings.Add("No GC events found. Ensure Microsoft-Windows-DotNETRuntime:0x1:4 was captured.");

        return metrics;
    }

    private static void HandleGcStart(IGenericEvent evt, GcMetrics metrics)
    {
        metrics.TotalGcCount++;
        int depth = GetFieldInt32(evt, "Depth", 0);
        switch (depth)
        {
            case 0: metrics.Gen0Count++; break;
            case 1: metrics.Gen1Count++; break;
            case 2: metrics.Gen2Count++; break;
        }
    }

    private static int GetFieldInt32(IGenericEvent evt, string fieldName, int defaultValue)
    {
        try
        {
            var field = evt.Fields.FirstOrDefault(f => f.Name == fieldName);
            return field != null ? (int)field.AsInt32 : defaultValue;
        }
        catch { return defaultValue; }
    }

    private static long GetFieldInt64(IGenericEvent evt, string fieldName, long defaultValue)
    {
        try
        {
            var field = evt.Fields.FirstOrDefault(f => f.Name == fieldName);
            return field != null ? field.AsInt64 : defaultValue;
        }
        catch { return defaultValue; }
    }
}
