using WpfPerfDiff.Models;

namespace WpfPerfDiff;

/// <summary>
/// Aggregates a list of AnalysisResult runs into a single AggregatedRun.
/// Uses median + stddev when N >= 3, median-only when N == 2, single value when N == 1.
/// </summary>
public static class Aggregator
{
    public static AggregatedRun Aggregate(IReadOnlyList<AnalysisResult> runs, string label)
    {
        if (runs.Count == 0)
            throw new ArgumentException("Cannot aggregate zero runs.", nameof(runs));

        string? scenarioName = runs
            .Select(r => r.PerfHarnessEvents?.Scenario)
            .Where(s => s != null)
            .GroupBy(s => s)
            .OrderByDescending(g => g.Count())
            .FirstOrDefault()?.Key;

        return new AggregatedRun
        {
            Label = label,
            RunCount = runs.Count,
            ScenarioName = scenarioName,

            ScenarioDurationMs = Stat(runs, r => ScenarioDuration(r)),
            TotalCpuMs = Stat(runs, r => r.Process?.CpuTimeMs),
            TotalAllocBytes = Stat(runs, r => r.Gc != null ? (double)r.Gc.TotalAllocBytes : null),
            Gen0Count = Stat(runs, r => r.Gc != null ? (double)r.Gc.Gen0Count : null),
            Gen1Count = Stat(runs, r => r.Gc != null ? (double)r.Gc.Gen1Count : null),
            Gen2Count = Stat(runs, r => r.Gc != null ? (double)r.Gc.Gen2Count : null),
            GcPauseMs = Stat(runs, r => r.Gc?.TotalPauseTimeMs),
            JitMethods = Stat(runs, r => r.Jit != null ? (double)r.Jit.MethodsJitted : null),
            JitTimeMs = Stat(runs, r => r.Jit?.TotalJitTimeMs),
            LayoutPassCount = Stat(runs, r => r.Wpf != null ? (double)r.Wpf.LayoutPassCount : null),
            LayoutPassTotalMs = Stat(runs, r => r.Wpf?.LayoutPassTotalMs),
            RenderPassCount = Stat(runs, r => r.Wpf != null ? (double)r.Wpf.RenderPassCount : null),
            RenderPassTotalMs = Stat(runs, r => r.Wpf?.RenderPassTotalMs),

            StepTimingsMs = AggregateSteps(runs),
            MethodCpuMs = AggregateMethods(runs, r => r.TopCpuMethods
                .Select(m => (Key: MethodKey(m.Module, m.Method), Value: m.CpuMs))),
            MethodAllocBytes = AggregateMethods(runs, r => r.TopAllocators
                .Select(a => (Key: MethodKey(a.Module, a.Method), Value: (double)a.AllocBytes))),
        };
    }

    private static double? ScenarioDuration(AnalysisResult r)
    {
        if (r.PerfHarnessEvents != null &&
            r.PerfHarnessEvents.ScenarioEndTimestampMs > r.PerfHarnessEvents.ScenarioStartTimestampMs)
        {
            return r.PerfHarnessEvents.ScenarioEndTimestampMs - r.PerfHarnessEvents.ScenarioStartTimestampMs;
        }
        // Fallback: wall clock of process
        if (r.Process?.WallClockMs > 0)
            return r.Process.WallClockMs;
        return r.CaptureSpanMs > 0 ? r.CaptureSpanMs : null;
    }

    private static MetricStat Stat(IReadOnlyList<AnalysisResult> runs, Func<AnalysisResult, double?> selector)
    {
        var values = runs
            .Select(selector)
            .Where(v => v.HasValue)
            .Select(v => v!.Value)
            .ToList();

        return values.Count == 0 ? MetricStat.Zero : MetricStat.FromSamples(values);
    }

    private static Dictionary<string, MetricStat> AggregateSteps(IReadOnlyList<AnalysisResult> runs)
    {
        // Collect all step names across runs
        var stepNames = runs
            .SelectMany(r => r.PerfHarnessEvents?.StepTimings ?? [])
            .Select(s => s.Name)
            .Distinct()
            .ToList();

        var result = new Dictionary<string, MetricStat>(stepNames.Count);
        foreach (var stepName in stepNames)
        {
            var values = runs
                .SelectMany(r => r.PerfHarnessEvents?.StepTimings ?? [])
                .Where(s => s.Name == stepName)
                .Select(s => s.ElapsedMs)
                .ToList();
            result[stepName] = MetricStat.FromSamples(values);
        }
        return result;
    }

    private static Dictionary<string, MetricStat> AggregateMethods(
        IReadOnlyList<AnalysisResult> runs,
        Func<AnalysisResult, IEnumerable<(string Key, double Value)>> selector)
    {
        // Collect all method keys across runs
        var allKeys = runs.SelectMany(r => selector(r).Select(x => x.Key)).Distinct().ToList();

        var result = new Dictionary<string, MetricStat>(allKeys.Count);
        foreach (var key in allKeys)
        {
            var values = runs
                .SelectMany(r => selector(r))
                .Where(x => x.Key == key)
                .Select(x => x.Value)
                .ToList();
            result[key] = MetricStat.FromSamples(values);
        }
        return result;
    }

    public static string MethodKey(string module, string method) =>
        string.IsNullOrEmpty(module) ? method : $"{module}::{method}";

    /// <summary>Extract the top-level module (first namespace segment) from a method key.</summary>
    public static string TopModule(string key)
    {
        // key format: "Module::Method.Name" or just "Method.Name"
        var colonIdx = key.IndexOf("::", StringComparison.Ordinal);
        var candidate = colonIdx >= 0 ? key[..colonIdx] : key;
        var dotIdx = candidate.IndexOf('.');
        return dotIdx >= 0 ? candidate[..dotIdx] : candidate;
    }
}
