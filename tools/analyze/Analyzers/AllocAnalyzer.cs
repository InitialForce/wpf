using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Events;
using WpfPerfAnalyze.Models;

namespace WpfPerfAnalyze.Analyzers;

/// <summary>
/// Builds a top-N allocator list using GCAllocationTick events from
/// Microsoft-Windows-DotNETRuntime.
///
/// GCAllocationTick fires every ~100KB of allocations per thread. The
/// "AllocationAmount64" field (v2) gives bytes for that tick. The
/// "TypeName" field identifies the allocated type.
///
/// This is a sampling approximation — it misses allocations under ~100KB total
/// per thread. For exact allocation tracking, the GCAlloc keyword
/// (0x80000) is needed but is very high overhead.
/// </summary>
internal sealed class AllocAnalyzer
{
    private static readonly Guid s_dotNetRuntimeProviderId =
        new Guid("e13c0d23-ccbc-4e12-931b-d9cc2eee27e4");

    private const int EventIdGcAllocationTick = 10;

    private readonly int _topN;

    public AllocAnalyzer(int topN)
    {
        _topN = topN;
    }

    public List<AllocEntry> Analyze(
        IPendingResult<IGenericEventDataSource> genericEventsPending,
        int targetPid,
        List<string> warnings)
    {
        if (!genericEventsPending.HasResult)
            return [];

        var events = genericEventsPending.Result.Events;

        // key: typeName → (totalBytes, tickCount)
        var byType = new Dictionary<string, (long bytes, int count)>(StringComparer.Ordinal);

        foreach (var evt in events)
        {
            if (evt.ProviderId != s_dotNetRuntimeProviderId)
                continue;
            if (evt.Id != EventIdGcAllocationTick)
                continue;
            if (evt.ProcessId != targetPid)
                continue;

            long amount = GetFieldInt64(evt, "AllocationAmount64",
                            GetFieldInt32(evt, "AllocationAmount", 0));
            string typeName = GetFieldString(evt, "TypeName", "<unknown>");

            if (byType.TryGetValue(typeName, out var existing))
                byType[typeName] = (existing.bytes + amount, existing.count + 1);
            else
                byType[typeName] = (amount, 1);
        }

        if (byType.Count == 0)
            warnings.Add("No GCAllocationTick events found. Capture needs Microsoft-Windows-DotNETRuntime:0x1 keyword.");

        return [.. byType
            .OrderByDescending(kvp => kvp.Value.bytes)
            .Take(_topN)
            .Select(kvp => new AllocEntry
            {
                Method = kvp.Key,
                Module = ExtractModuleFromType(kvp.Key),
                AllocBytes = kvp.Value.bytes,
                AllocCount = kvp.Value.count,
            })];
    }

    private static string ExtractModuleFromType(string typeName)
    {
        // Heuristic: top-level namespace as module
        int dot = typeName.IndexOf('.');
        return dot > 0 ? typeName[..dot] : typeName;
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

    private static string GetFieldString(IGenericEvent evt, string fieldName, string defaultValue)
    {
        try
        {
            var field = evt.Fields.FirstOrDefault(f => f.Name == fieldName);
            return field?.AsString ?? defaultValue;
        }
        catch { return defaultValue; }
    }
}
