namespace WpfPerfDiff.Models;

/// <summary>Aggregated metrics across one or more analysis runs (baseline or experimental).</summary>
public sealed class AggregatedRun
{
    /// <summary>Human-readable label for this side (e.g., "if.72 (5 runs, median)").</summary>
    public string Label { get; init; } = "";

    /// <summary>Number of input runs that were aggregated.</summary>
    public int RunCount { get; init; }

    /// <summary>Scenario name (from PerfHarnessEvents), if consistent across runs.</summary>
    public string? ScenarioName { get; init; }

    // ── Headline scalars (all in the median's units) ──────────────────────────

    public MetricStat ScenarioDurationMs { get; init; } = MetricStat.Zero;
    public MetricStat TotalCpuMs { get; init; } = MetricStat.Zero;
    public MetricStat TotalAllocBytes { get; init; } = MetricStat.Zero;
    public MetricStat Gen0Count { get; init; } = MetricStat.Zero;
    public MetricStat Gen1Count { get; init; } = MetricStat.Zero;
    public MetricStat Gen2Count { get; init; } = MetricStat.Zero;
    public MetricStat GcPauseMs { get; init; } = MetricStat.Zero;
    public MetricStat JitMethods { get; init; } = MetricStat.Zero;
    public MetricStat JitTimeMs { get; init; } = MetricStat.Zero;
    public MetricStat LayoutPassCount { get; init; } = MetricStat.Zero;
    public MetricStat LayoutPassTotalMs { get; init; } = MetricStat.Zero;
    public MetricStat RenderPassCount { get; init; } = MetricStat.Zero;
    public MetricStat RenderPassTotalMs { get; init; } = MetricStat.Zero;

    // ── Per-step timings (median across runs, keyed by step name) ────────────
    public Dictionary<string, MetricStat> StepTimingsMs { get; init; } = [];

    // ── Per-method CPU (median across runs, keyed by "Module::Method") ───────
    public Dictionary<string, MetricStat> MethodCpuMs { get; init; } = [];

    // ── Per-method alloc (median across runs, keyed by "Module::Method") ─────
    public Dictionary<string, MetricStat> MethodAllocBytes { get; init; } = [];
}

/// <summary>A single aggregated scalar: median value + optional standard deviation.</summary>
public readonly record struct MetricStat(double Median, double? StdDev, bool IsAvailable)
{
    public static MetricStat Zero => new(0, null, false);

    public static MetricStat Single(double value) => new(value, null, true);

    public static MetricStat FromSamples(IReadOnlyList<double> values)
    {
        if (values.Count == 0) return Zero;
        if (values.Count == 1) return Single(values[0]);

        var sorted = values.OrderBy(v => v).ToArray();
        double median = sorted.Length % 2 == 0
            ? (sorted[sorted.Length / 2 - 1] + sorted[sorted.Length / 2]) / 2.0
            : sorted[sorted.Length / 2];

        double mean = values.Average();
        double variance = values.Sum(v => (v - mean) * (v - mean)) / (values.Count - 1);
        double stdDev = Math.Sqrt(variance);

        return new MetricStat(median, stdDev, true);
    }
}
