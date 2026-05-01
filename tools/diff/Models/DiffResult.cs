namespace WpfPerfDiff.Models;

/// <summary>Complete A/B diff result ready for report generation.</summary>
public sealed class DiffResult
{
    public AggregatedRun Baseline { get; init; } = new();
    public AggregatedRun Experimental { get; init; } = new();
    public double ThresholdPct { get; init; } = 5.0;
    public DateTime GeneratedUtc { get; init; } = DateTime.UtcNow;

    public List<MetricDelta> HeadlineMetrics { get; init; } = [];
    public List<MetricDelta> StepTimings { get; init; } = [];
    public List<MethodDelta> TopCpuRegressions { get; init; } = [];
    public List<MethodDelta> TopCpuWins { get; init; } = [];
    public List<MethodDelta> TopAllocRegressions { get; init; } = [];
    public List<MethodDelta> TopAllocWins { get; init; } = [];
    public List<string> MethodsOnlyInBaseline { get; init; } = [];
    public List<string> MethodsOnlyInExperimental { get; init; } = [];

    /// <summary>Noise warnings (stddev > 5% of median).</summary>
    public List<NoiseWarning> NoiseWarnings { get; init; } = [];

    public string Verdict { get; set; } = "";
}

/// <summary>Delta for a single headline metric.</summary>
public sealed class MetricDelta
{
    public string Name { get; init; } = "";
    public string Unit { get; init; } = "";
    public MetricStat BaselineStat { get; init; }
    public MetricStat ExperimentalStat { get; init; }
    public double Delta { get; init; }
    public double DeltaPct { get; init; }
    public DeltaFlag Flag { get; init; }
    public bool IsHigherBetter { get; init; }
}

/// <summary>Delta for a method-level CPU or allocation entry.</summary>
public sealed class MethodDelta
{
    public string Method { get; init; } = "";
    public string Module { get; init; } = "";
    public double BaselineValue { get; init; }
    public double ExperimentalValue { get; init; }
    public double Delta { get; init; }
    public double DeltaPct { get; init; }
}

public sealed class NoiseWarning
{
    public string MetricName { get; init; } = "";
    public string Side { get; init; } = "";
    public double StdDev { get; init; }
    public double Median { get; init; }
    public double StdDevPct => Median == 0 ? 0 : Math.Abs(StdDev / Median) * 100.0;
}

public enum DeltaFlag
{
    /// <summary>Within threshold — no significant change.</summary>
    Neutral,

    /// <summary>Improvement above threshold but below 2x threshold.</summary>
    Win,

    /// <summary>Large improvement (>= 2x threshold).</summary>
    BigWin,

    /// <summary>Regression above threshold but below 2x threshold.</summary>
    Regression,

    /// <summary>Large regression (>= 2x threshold).</summary>
    BigRegression,
}
