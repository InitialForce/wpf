using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Events;
using WpfPerfAnalyze.Models;

namespace WpfPerfAnalyze.Analyzers;

/// <summary>
/// Extracts WPF-specific metrics from the Microsoft-Windows-WPF ETW provider.
///
/// Provider GUID: e45aeb82-ac69-44f9-bf7a-35bcd41ab10d (Microsoft-Windows-WPF)
///
/// Key events (from WPF source / TRACE_EVENT_SOURCE_WPF manifest):
///   Layout pass:  UpdateLayoutBegin (Opcode=Start) / UpdateLayoutEnd (Opcode=Stop) — keyword 0x8
///   Render pass:  RenderHandlerBegin / RenderHandlerEnd — keyword 0x10
///   BAML load:    BamlAssociatedClrTypeBegin / BamlAssociatedClrTypeEnd — keyword 0x80
///
/// Duration is computed by pairing Start/Stop opcodes per thread.
/// V1 limitation: if the WPF provider manifest is not available to TraceProcessor,
/// IGenericEvent.Fields will be empty and we can only count events.
/// </summary>
internal sealed class WpfEventAnalyzer
{
    private static readonly Guid s_wpfProviderId =
        new Guid("e45aeb82-ac69-44f9-bf7a-35bcd41ab10d");

    // ETW opcode values
    private const int OpcodeStart = 1;
    private const int OpcodeStop = 2;

    // WPF keyword bits (from WPF ETW manifest)
    private const long KeywordLayout = 0x8;
    private const long KeywordRender = 0x10;
    private const long KeywordBaml = 0x80;

    public static WpfMetrics Analyze(
        IPendingResult<IGenericEventDataSource> genericEventsPending,
        int targetPid,
        List<string> warnings)
    {
        var metrics = new WpfMetrics();

        if (!genericEventsPending.HasResult)
            return metrics;

        var events = genericEventsPending.Result.Events;

        // Per-thread start timestamps keyed by threadId
        var layoutStarts = new Dictionary<int, double>();
        var renderStarts = new Dictionary<int, double>();
        var bamlStarts = new Dictionary<int, double>();

        double layoutMs = 0;
        double renderMs = 0;
        double bamlMs = 0;
        int layoutCount = 0;
        int renderCount = 0;
        int bamlCount = 0;
        bool seenAnyWpfEvent = false;

        foreach (var evt in events)
        {
            if (evt.ProviderId != s_wpfProviderId)
                continue;
            if (evt.ProcessId != targetPid)
                continue;

            seenAnyWpfEvent = true;
            long keyword = evt.Keyword;
            int tid = evt.ThreadId;
            int opcode = evt.Opcode;

            if ((keyword & KeywordLayout) != 0)
            {
                if (opcode == OpcodeStart)
                {
                    layoutStarts[tid] = (double)evt.Timestamp.TotalMilliseconds;
                }
                else if (opcode == OpcodeStop && layoutStarts.TryGetValue(tid, out double startMs))
                {
                    layoutStarts.Remove(tid);
                    double dur = (double)evt.Timestamp.TotalMilliseconds - startMs;
                    if (dur >= 0)
                    {
                        layoutMs += dur;
                        layoutCount++;
                    }
                }
            }
            else if ((keyword & KeywordRender) != 0)
            {
                if (opcode == OpcodeStart)
                {
                    renderStarts[tid] = (double)evt.Timestamp.TotalMilliseconds;
                }
                else if (opcode == OpcodeStop && renderStarts.TryGetValue(tid, out double startMs))
                {
                    renderStarts.Remove(tid);
                    double dur = (double)evt.Timestamp.TotalMilliseconds - startMs;
                    if (dur >= 0)
                    {
                        renderMs += dur;
                        renderCount++;
                    }
                }
            }
            else if ((keyword & KeywordBaml) != 0)
            {
                if (opcode == OpcodeStart)
                {
                    bamlStarts[tid] = (double)evt.Timestamp.TotalMilliseconds;
                }
                else if (opcode == OpcodeStop && bamlStarts.TryGetValue(tid, out double startMs))
                {
                    bamlStarts.Remove(tid);
                    double dur = (double)evt.Timestamp.TotalMilliseconds - startMs;
                    if (dur >= 0)
                    {
                        bamlMs += dur;
                        bamlCount++;
                    }
                }
            }
        }

        metrics.LayoutPassCount = layoutCount;
        metrics.LayoutPassTotalMs = layoutMs;
        metrics.RenderPassCount = renderCount;
        metrics.RenderPassTotalMs = renderMs;
        metrics.BamlLoadCount = bamlCount;
        metrics.BamlLoadTotalMs = bamlMs;

        if (!seenAnyWpfEvent)
            warnings.Add("No Microsoft-Windows-WPF events found. Ensure 'Microsoft-Windows-WPF' provider was enabled during capture.");
        else if (layoutCount == 0 && renderCount == 0 && bamlCount == 0)
            warnings.Add("WPF events found but no layout/render/baml pairs matched. " +
                         "Keyword values may differ from manifest; raw event names need manifest for parsing.");

        return metrics;
    }
}
