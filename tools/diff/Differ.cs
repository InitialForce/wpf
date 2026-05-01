using WpfPerfDiff.Models;

namespace WpfPerfDiff;

/// <summary>
/// Computes deltas between two aggregated runs and classifies them.
/// </summary>
public static class Differ
{
    // Methods whose absolute CPU delta is below this floor are excluded from top lists.
    private const double CpuFloorMs = 5.0;
    // Methods whose absolute alloc delta is below this floor are excluded from top lists.
    private const double AllocFloorBytes = 10_000.0;
    // Number of top entries per regression/win list.
    private const int TopN = 10;
    // If stddev > this fraction of median, emit a noise warning.
    private const double NoiseThresholdFraction = 0.05;

    public static DiffResult Compute(AggregatedRun baseline, AggregatedRun experimental, double thresholdPct)
    {
        var result = new DiffResult
        {
            Baseline = baseline,
            Experimental = experimental,
            ThresholdPct = thresholdPct,
            GeneratedUtc = DateTime.UtcNow,

            HeadlineMetrics = BuildHeadlineMetrics(baseline, experimental, thresholdPct),
            StepTimings = BuildStepTimings(baseline, experimental, thresholdPct),
            TopCpuRegressions = TopMethodDeltas(baseline.MethodCpuMs, experimental.MethodCpuMs,
                thresholdPct, CpuFloorMs, regression: true),
            TopCpuWins = TopMethodDeltas(baseline.MethodCpuMs, experimental.MethodCpuMs,
                thresholdPct, CpuFloorMs, regression: false),
            TopAllocRegressions = TopMethodDeltas(baseline.MethodAllocBytes, experimental.MethodAllocBytes,
                thresholdPct, AllocFloorBytes, regression: true),
            TopAllocWins = TopMethodDeltas(baseline.MethodAllocBytes, experimental.MethodAllocBytes,
                thresholdPct, AllocFloorBytes, regression: false),
            MethodsOnlyInBaseline = MethodsOnlyIn(baseline.MethodCpuMs, experimental.MethodCpuMs),
            MethodsOnlyInExperimental = MethodsOnlyIn(experimental.MethodCpuMs, baseline.MethodCpuMs),
            NoiseWarnings = BuildNoiseWarnings(baseline, experimental, NoiseThresholdFraction),
        };

        result.Verdict = GenerateVerdict(result, thresholdPct);
        return result;
    }

    // ── Headline metrics ───────────────────────────────────────────────────────

    private static List<MetricDelta> BuildHeadlineMetrics(
        AggregatedRun b, AggregatedRun e, double thresholdPct)
    {
        // Lower is better for all of these (duration, CPU, alloc, GC pauses, etc.)
        // Higher is better: none of the current metrics.
        var metrics = new (string Name, string Unit, MetricStat B, MetricStat E, bool HigherBetter)[]
        {
            ("Scenario duration", "ms",   b.ScenarioDurationMs,  e.ScenarioDurationMs,  false),
            ("Total CPU",         "ms",   b.TotalCpuMs,          e.TotalCpuMs,          false),
            ("Total alloc",       "MB",   ScaleStat(b.TotalAllocBytes, 1.0/1_048_576.0),
                                          ScaleStat(e.TotalAllocBytes, 1.0/1_048_576.0), false),
            ("GC gen0 count",     "",     b.Gen0Count,           e.Gen0Count,           false),
            ("GC gen1 count",     "",     b.Gen1Count,           e.Gen1Count,           false),
            ("GC gen2 count",     "",     b.Gen2Count,           e.Gen2Count,           false),
            ("GC pause",          "ms",   b.GcPauseMs,           e.GcPauseMs,           false),
            ("JIT methods",       "",     b.JitMethods,          e.JitMethods,          false),
            ("JIT time",          "ms",   b.JitTimeMs,           e.JitTimeMs,           false),
            ("Layout passes",     "",     b.LayoutPassCount,     e.LayoutPassCount,     false),
            ("Layout time",       "ms",   b.LayoutPassTotalMs,   e.LayoutPassTotalMs,   false),
            ("Render passes",     "",     b.RenderPassCount,     e.RenderPassCount,     false),
            ("Render time",       "ms",   b.RenderPassTotalMs,   e.RenderPassTotalMs,   false),
        };

        var result = new List<MetricDelta>(metrics.Length);
        foreach (var (name, unit, bStat, eStat, higherBetter) in metrics)
        {
            if (!bStat.IsAvailable && !eStat.IsAvailable) continue;
            result.Add(BuildDelta(name, unit, bStat, eStat, higherBetter, thresholdPct));
        }
        return result;
    }

    private static MetricStat ScaleStat(MetricStat s, double factor) =>
        s.IsAvailable
            ? new MetricStat(s.Median * factor, s.StdDev.HasValue ? s.StdDev.Value * factor : null, true)
            : MetricStat.Zero;

    private static List<MetricDelta> BuildStepTimings(AggregatedRun b, AggregatedRun e, double thresholdPct)
    {
        var stepNames = b.StepTimingsMs.Keys.Union(e.StepTimingsMs.Keys).OrderBy(k => k).ToList();
        var result = new List<MetricDelta>(stepNames.Count);
        foreach (var step in stepNames)
        {
            var bStat = b.StepTimingsMs.GetValueOrDefault(step, MetricStat.Zero);
            var eStat = e.StepTimingsMs.GetValueOrDefault(step, MetricStat.Zero);
            result.Add(BuildDelta(step, "ms", bStat, eStat, higherBetter: false, thresholdPct));
        }
        return result;
    }

    private static MetricDelta BuildDelta(
        string name, string unit,
        MetricStat bStat, MetricStat eStat,
        bool higherBetter, double thresholdPct)
    {
        double delta = eStat.Median - bStat.Median;
        double deltaRelative = bStat.Median == 0
            ? (delta == 0 ? 0 : double.PositiveInfinity)
            : delta / bStat.Median;
        double deltaPct = deltaRelative * 100.0;

        // "improvement" = lower is better means negative delta = win
        // "improvement" = higher is better means positive delta = win
        double improvementPct = higherBetter ? deltaPct : -deltaPct;

        DeltaFlag flag = improvementPct >= 2 * thresholdPct ? DeltaFlag.BigWin
            : improvementPct >= thresholdPct ? DeltaFlag.Win
            : improvementPct <= -2 * thresholdPct ? DeltaFlag.BigRegression
            : improvementPct <= -thresholdPct ? DeltaFlag.Regression
            : DeltaFlag.Neutral;

        return new MetricDelta
        {
            Name = name,
            Unit = unit,
            BaselineStat = bStat,
            ExperimentalStat = eStat,
            Delta = delta,
            DeltaPct = deltaPct,
            Flag = flag,
            IsHigherBetter = higherBetter,
        };
    }

    // ── Method-level deltas ────────────────────────────────────────────────────

    private static List<MethodDelta> TopMethodDeltas(
        Dictionary<string, MetricStat> baseMap,
        Dictionary<string, MetricStat> expMap,
        double thresholdPct,
        double floor,
        bool regression)
    {
        var commonKeys = baseMap.Keys.Intersect(expMap.Keys).ToList();

        var deltas = commonKeys
            .Select(key =>
            {
                double bVal = baseMap[key].Median;
                double eVal = expMap[key].Median;
                double delta = eVal - bVal;
                double pct = bVal == 0 ? 0 : delta / bVal * 100.0;
                var (module, method) = SplitKey(key);
                return new MethodDelta
                {
                    Method = method,
                    Module = module,
                    BaselineValue = bVal,
                    ExperimentalValue = eVal,
                    Delta = delta,
                    DeltaPct = pct,
                };
            })
            .Where(d =>
            {
                bool isRegression = d.Delta > 0; // higher CPU/alloc = regression
                if (regression != isRegression) return false;
                if (Math.Abs(d.Delta) < floor) return false;
                if (Math.Abs(d.DeltaPct) < thresholdPct) return false;
                return true;
            })
            .OrderByDescending(d => Math.Abs(d.Delta))
            .Take(TopN)
            .ToList();

        return deltas;
    }

    private static List<string> MethodsOnlyIn(
        Dictionary<string, MetricStat> present,
        Dictionary<string, MetricStat> absent)
    {
        return [.. present.Keys.Except(absent.Keys).OrderBy(k => k)];
    }

    // ── Noise warnings ─────────────────────────────────────────────────────────

    private static List<NoiseWarning> BuildNoiseWarnings(
        AggregatedRun b, AggregatedRun e, double fraction)
    {
        var warnings = new List<NoiseWarning>();

        CheckStat(b.ScenarioDurationMs, "Scenario duration", "baseline");
        CheckStat(e.ScenarioDurationMs, "Scenario duration", "experimental");
        CheckStat(b.TotalCpuMs, "Total CPU", "baseline");
        CheckStat(e.TotalCpuMs, "Total CPU", "experimental");
        CheckStat(b.TotalAllocBytes, "Total alloc bytes", "baseline");
        CheckStat(e.TotalAllocBytes, "Total alloc bytes", "experimental");

        return warnings;

        void CheckStat(MetricStat s, string name, string side)
        {
            if (!s.IsAvailable || !s.StdDev.HasValue || s.Median == 0) return;
            if (Math.Abs(s.StdDev.Value / s.Median) > fraction)
            {
                warnings.Add(new NoiseWarning
                {
                    MetricName = name,
                    Side = side,
                    StdDev = s.StdDev.Value,
                    Median = s.Median,
                });
            }
        }
    }

    // ── Verdict ───────────────────────────────────────────────────────────────

    private static string GenerateVerdict(DiffResult r, double thresholdPct)
    {
        var duration = r.HeadlineMetrics.FirstOrDefault(m => m.Name == "Scenario duration");
        var cpu = r.HeadlineMetrics.FirstOrDefault(m => m.Name == "Total CPU");
        var alloc = r.HeadlineMetrics.FirstOrDefault(m => m.Name == "Total alloc");

        bool hasDurationData = duration != null && duration.BaselineStat.IsAvailable;
        bool noiseIssue = r.NoiseWarnings.Any(w => w.MetricName == "Scenario duration");

        if (!hasDurationData)
            return "Insufficient data to produce a verdict — scenario duration metrics are unavailable.";

        if (noiseIssue)
            return $"Run-to-run noise exceeds 5% on scenario duration — results may not be reliable. Increase run count or suppress noise sources before drawing conclusions.";

        int wins = r.HeadlineMetrics.Count(m => m.Flag is DeltaFlag.Win or DeltaFlag.BigWin);
        int regressions = r.HeadlineMetrics.Count(m => m.Flag is DeltaFlag.Regression or DeltaFlag.BigRegression);

        string durationSummary = duration!.Flag switch
        {
            DeltaFlag.BigWin => $"Experimental wins strongly on scenario duration ({duration.DeltaPct:F1}%).",
            DeltaFlag.Win => $"Experimental wins on scenario duration ({duration.DeltaPct:F1}%).",
            DeltaFlag.Neutral => $"Scenario duration delta ({duration.DeltaPct:F1}%) is within the {thresholdPct:F0}% noise threshold.",
            DeltaFlag.Regression => $"Experimental regresses on scenario duration ({duration.DeltaPct:F1}%).",
            DeltaFlag.BigRegression => $"Experimental regresses strongly on scenario duration ({duration.DeltaPct:F1}%).",
            _ => ""
        };

        if (regressions == 0 && wins > 0)
            return $"{durationSummary} {wins} metric(s) improved with no significant regressions detected.";

        if (wins == 0 && regressions > 0)
            return $"{durationSummary} {regressions} metric(s) regressed with no significant wins detected.";

        if (wins > 0 && regressions > 0)
            return $"{durationSummary} Mixed result: {wins} win(s), {regressions} regression(s) — review per-metric details.";

        return $"{durationSummary} No metrics crossed the {thresholdPct:F0}% threshold in either direction.";
    }

    private static (string Module, string Method) SplitKey(string key)
    {
        var idx = key.IndexOf("::", StringComparison.Ordinal);
        if (idx < 0) return ("", key);
        return (key[..idx], key[(idx + 2)..]);
    }
}
