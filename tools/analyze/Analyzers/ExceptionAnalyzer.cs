using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Events;
using WpfPerfAnalyze.Models;

namespace WpfPerfAnalyze.Analyzers;

/// <summary>
/// Counts managed exceptions from Microsoft-Windows-DotNETRuntime events.
///
/// ExceptionThrown_V1 (Event ID 80): fired for every first-chance exception.
/// Field "ExceptionType" carries the full CLR type name.
/// </summary>
internal sealed class ExceptionAnalyzer
{
    private static readonly Guid s_dotNetRuntimeProviderId =
        new Guid("e13c0d23-ccbc-4e12-931b-d9cc2eee27e4");

    private const int EventIdExceptionThrown = 80;

    public static ExceptionMetrics Analyze(
        IPendingResult<IGenericEventDataSource> genericEventsPending,
        int targetPid)
    {
        var metrics = new ExceptionMetrics();

        if (!genericEventsPending.HasResult)
            return metrics;

        var events = genericEventsPending.Result.Events;
        var byType = new Dictionary<string, int>(StringComparer.Ordinal);

        foreach (var evt in events)
        {
            if (evt.ProviderId != s_dotNetRuntimeProviderId)
                continue;
            if (evt.Id != EventIdExceptionThrown)
                continue;
            if (evt.ProcessId != targetPid)
                continue;

            string typeName = GetFieldString(evt, "ExceptionType", "<unknown>");
            byType.TryGetValue(typeName, out int count);
            byType[typeName] = count + 1;
            metrics.TotalCount++;
        }

        metrics.ByType = [..byType
            .OrderByDescending(kvp => kvp.Value)
            .Select(kvp => new ExceptionTypeEntry { Type = kvp.Key, Count = kvp.Value })];

        return metrics;
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
