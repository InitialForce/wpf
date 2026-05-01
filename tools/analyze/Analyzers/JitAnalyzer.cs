using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Events;
using WpfPerfAnalyze.Models;

namespace WpfPerfAnalyze.Analyzers;

/// <summary>
/// Extracts JIT compilation metrics from Microsoft-Windows-DotNETRuntime events.
///
/// MethodJittingStarted (Event ID 145): fired when JIT begins for a method.
/// MethodLoad_V2        (Event ID 143): fired when JIT completes.
///
/// Total JIT time is computed by pairing each MethodJittingStarted with the
/// corresponding MethodLoad_V2 for the same MethodID on the same thread.
/// </summary>
internal sealed class JitAnalyzer
{
    private static readonly Guid s_dotNetRuntimeProviderId =
        new Guid("e13c0d23-ccbc-4e12-931b-d9cc2eee27e4");

    // CLR event IDs
    private const int EventIdMethodJittingStarted = 145;
    private const int EventIdMethodLoadVerbose = 143;  // MethodLoad_V2

    public static JitMetrics Analyze(
        IPendingResult<IGenericEventDataSource> genericEventsPending,
        int targetPid,
        List<string> warnings)
    {
        var metrics = new JitMetrics();

        if (!genericEventsPending.HasResult)
            return metrics;

        var events = genericEventsPending.Result.Events;

        // key: (threadId, methodId) → start timestamp ms
        var inFlight = new Dictionary<(int tid, long mid), double>();
        double totalJitMs = 0;
        int methodsJitted = 0;

        foreach (var evt in events)
        {
            if (evt.ProviderId != s_dotNetRuntimeProviderId)
                continue;
            if (evt.ProcessId != targetPid)
                continue;

            switch (evt.Id)
            {
                case EventIdMethodJittingStarted:
                    {
                        long methodId = GetFieldInt64(evt, "MethodID", 0);
                        var key = (evt.ThreadId, methodId);
                        inFlight[key] = (double)evt.Timestamp.TotalMilliseconds;
                        break;
                    }

                case EventIdMethodLoadVerbose:
                    {
                        long methodId = GetFieldInt64(evt, "MethodID", 0);
                        var key = (evt.ThreadId, methodId);
                        if (inFlight.TryGetValue(key, out double startMs))
                        {
                            inFlight.Remove(key);
                            double jitMs = (double)evt.Timestamp.TotalMilliseconds - startMs;
                            if (jitMs >= 0)
                            {
                                totalJitMs += jitMs;
                                methodsJitted++;
                            }
                        }
                        break;
                    }
            }
        }

        metrics.MethodsJitted = methodsJitted;
        metrics.TotalJitTimeMs = totalJitMs;

        if (methodsJitted == 0)
            warnings.Add("No JIT events found. Ensure Microsoft-Windows-DotNETRuntime:0x18:4 keyword was set during capture.");

        return metrics;
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
