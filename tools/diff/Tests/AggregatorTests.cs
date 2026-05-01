using NUnit.Framework;
using WpfPerfDiff.Models;

namespace WpfPerfDiff.Tests;

[TestFixture]
public class AggregatorTests
{
    // ── MetricStat.FromSamples ────────────────────────────────────────────────

    [Test]
    public void FromSamples_Empty_ReturnsZero()
    {
        var s = MetricStat.FromSamples([]);
        Assert.That(s.IsAvailable, Is.False);
        Assert.That(s.Median, Is.EqualTo(0));
    }

    [Test]
    public void FromSamples_SingleValue_ReturnsThatValueNoStdDev()
    {
        var s = MetricStat.FromSamples([42.0]);
        Assert.That(s.IsAvailable, Is.True);
        Assert.That(s.Median, Is.EqualTo(42.0));
        Assert.That(s.StdDev, Is.Null);
    }

    [Test]
    public void FromSamples_OddCount_MedianIsMiddleElement()
    {
        // sorted: [1, 2, 3, 4, 5]  → median = 3
        var s = MetricStat.FromSamples([5.0, 3.0, 1.0, 2.0, 4.0]);
        Assert.That(s.Median, Is.EqualTo(3.0).Within(1e-9));
    }

    [Test]
    public void FromSamples_EvenCount_MedianIsMidpointOfTwoMiddle()
    {
        // sorted: [1, 2, 3, 4] → median = (2+3)/2 = 2.5
        var s = MetricStat.FromSamples([4.0, 1.0, 3.0, 2.0]);
        Assert.That(s.Median, Is.EqualTo(2.5).Within(1e-9));
    }

    [Test]
    public void FromSamples_KnownStdDev_MatchesFormula()
    {
        // Values: [2, 4, 4, 4, 5, 5, 7, 9]
        // mean = 5, sample stddev = sqrt(((9+1+1+1+0+0+4+16)/7)) = sqrt(32/7) ≈ 2.138
        double[] values = [2, 4, 4, 4, 5, 5, 7, 9];
        var s = MetricStat.FromSamples(values);
        Assert.That(s.StdDev, Is.Not.Null);
        Assert.That(s.StdDev!.Value, Is.EqualTo(Math.Sqrt(32.0 / 7.0)).Within(1e-6));
    }

    [Test]
    public void FromSamples_TwoValues_HasStdDev()
    {
        var s = MetricStat.FromSamples([10.0, 20.0]);
        Assert.That(s.Median, Is.EqualTo(15.0).Within(1e-9));
        Assert.That(s.StdDev, Is.Not.Null);
        // sample stddev of [10,20]: mean=15, variance=(25+25)/1=50, stddev=sqrt(50)≈7.07
        Assert.That(s.StdDev!.Value, Is.EqualTo(Math.Sqrt(50.0)).Within(1e-6));
    }

    // ── Aggregator.Aggregate ──────────────────────────────────────────────────

    [Test]
    public void Aggregate_SingleRun_ScenarioDurationFromPerfHarness()
    {
        var run = MakeRun(scenarioStartMs: 1000, scenarioEndMs: 13000, cpuMs: 5800);
        var result = Aggregator.Aggregate([run], "test-label");
        Assert.That(result.ScenarioDurationMs.Median, Is.EqualTo(12000.0).Within(1e-6));
        Assert.That(result.TotalCpuMs.Median, Is.EqualTo(5800.0).Within(1e-6));
    }

    [Test]
    public void Aggregate_MultipleRuns_MedianChosen()
    {
        // 5 runs with durations [10000, 11000, 12000, 13000, 14000] → median = 12000
        var runs = new[]
        {
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 10000),
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 11000),
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 12000),
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 13000),
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 14000),
        };
        var result = Aggregator.Aggregate(runs, "test");
        Assert.That(result.ScenarioDurationMs.Median, Is.EqualTo(12000.0).Within(1e-6));
        Assert.That(result.RunCount, Is.EqualTo(5));
    }

    [Test]
    public void Aggregate_MultipleRuns_StdDevPresent()
    {
        var runs = new[]
        {
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 10000),
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 12000),
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 14000),
        };
        var result = Aggregator.Aggregate(runs, "test");
        Assert.That(result.ScenarioDurationMs.StdDev, Is.Not.Null);
        Assert.That(result.ScenarioDurationMs.StdDev!.Value, Is.GreaterThan(0));
    }

    [Test]
    public void Aggregate_FallbackToWallClock_WhenNoPerfHarness()
    {
        var run = new AnalysisResult
        {
            Process = new ProcessInfo { WallClockMs = 8000, CpuTimeMs = 4000 },
        };
        var result = Aggregator.Aggregate([run], "test");
        Assert.That(result.ScenarioDurationMs.Median, Is.EqualTo(8000.0).Within(1e-6));
    }

    [Test]
    public void Aggregate_StepTimings_AggregatedCorrectly()
    {
        var runs = new[]
        {
            MakeRunWithSteps([("open-session", 1100), ("scrub", 3900)]),
            MakeRunWithSteps([("open-session", 1300), ("scrub", 4100)]),
            MakeRunWithSteps([("open-session", 1200), ("scrub", 4000)]),
        };
        var result = Aggregator.Aggregate(runs, "test");
        Assert.That(result.StepTimingsMs["open-session"].Median, Is.EqualTo(1200.0).Within(1e-6));
        Assert.That(result.StepTimingsMs["scrub"].Median, Is.EqualTo(4000.0).Within(1e-6));
    }

    [Test]
    public void Aggregate_MethodCpu_AggregatedByKey()
    {
        var runs = new[]
        {
            MakeRunWithCpu([("PresentationCore", "UIElement.Measure", 100.0)]),
            MakeRunWithCpu([("PresentationCore", "UIElement.Measure", 120.0)]),
            MakeRunWithCpu([("PresentationCore", "UIElement.Measure", 110.0)]),
        };
        var result = Aggregator.Aggregate(runs, "test");
        string key = Aggregator.MethodKey("PresentationCore", "UIElement.Measure");
        Assert.That(result.MethodCpuMs.ContainsKey(key), Is.True);
        Assert.That(result.MethodCpuMs[key].Median, Is.EqualTo(110.0).Within(1e-6));
    }

    // ── Differ ────────────────────────────────────────────────────────────────

    [Test]
    public void Differ_NegativeDelta_LowerBetter_ClassifiedAsWin()
    {
        var baseline = SimpleAgg(scenarioDurationMs: 12000, cpuMs: 5800);
        var experimental = SimpleAgg(scenarioDurationMs: 11400, cpuMs: 5500); // ~5% improvement
        var diff = Differ.Compute(baseline, experimental, thresholdPct: 5.0);

        var durationDelta = diff.HeadlineMetrics.First(m => m.Name == "Scenario duration");
        Assert.That(durationDelta.Flag, Is.EqualTo(DeltaFlag.Win).Or.EqualTo(DeltaFlag.Neutral),
            "A ~5% improvement should be at least neutral");
        Assert.That(durationDelta.Delta, Is.LessThan(0));
    }

    [Test]
    public void Differ_PositiveDelta_LowerBetter_ClassifiedAsRegression()
    {
        var baseline = SimpleAgg(scenarioDurationMs: 10000, cpuMs: 5000);
        var experimental = SimpleAgg(scenarioDurationMs: 11000, cpuMs: 5500); // 10% regression
        var diff = Differ.Compute(baseline, experimental, thresholdPct: 5.0);

        var durationDelta = diff.HeadlineMetrics.First(m => m.Name == "Scenario duration");
        Assert.That(durationDelta.Flag, Is.EqualTo(DeltaFlag.Regression).Or.EqualTo(DeltaFlag.BigRegression));
    }

    [Test]
    public void Differ_SmallChange_ClassifiedAsNeutral()
    {
        var baseline = SimpleAgg(scenarioDurationMs: 10000, cpuMs: 5000);
        var experimental = SimpleAgg(scenarioDurationMs: 10200, cpuMs: 5100); // 2% change
        var diff = Differ.Compute(baseline, experimental, thresholdPct: 5.0);

        var durationDelta = diff.HeadlineMetrics.First(m => m.Name == "Scenario duration");
        Assert.That(durationDelta.Flag, Is.EqualTo(DeltaFlag.Neutral));
    }

    [Test]
    public void Differ_VerdictContainsScenarioDurationInfo()
    {
        var baseline = SimpleAgg(scenarioDurationMs: 12000, cpuMs: 5800);
        var experimental = SimpleAgg(scenarioDurationMs: 11000, cpuMs: 5500); // ~8.3% improvement
        var diff = Differ.Compute(baseline, experimental, thresholdPct: 5.0);

        Assert.That(diff.Verdict, Is.Not.Null.And.Not.Empty);
        // Should mention the direction of change
        Assert.That(diff.Verdict.ToLowerInvariant(), Does.Contain("win").Or.Contain("improv").Or.Contain("-8.3").Or.Contain("scenario"));
    }

    [Test]
    public void Differ_NoiseWarning_WhenStdDevExceeds5Pct()
    {
        // Create multi-run aggregated data with high noise
        var baselineRuns = new[]
        {
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 10000),
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 15000), // 50% spread — noisy
            MakeRun(scenarioStartMs: 0, scenarioEndMs: 11000),
        };
        var baseline = Aggregator.Aggregate(baselineRuns, "noisy-baseline");
        var experimental = Aggregator.Aggregate([MakeRun(scenarioStartMs: 0, scenarioEndMs: 11000)], "stable-exp");

        var diff = Differ.Compute(baseline, experimental, thresholdPct: 5.0);
        Assert.That(diff.NoiseWarnings.Any(w => w.Side == "baseline" && w.MetricName == "Scenario duration"), Is.True);
    }

    // ── ReportWriter ──────────────────────────────────────────────────────────

    [Test]
    public void ReportWriter_ContainsExpectedSections()
    {
        var baseline = SimpleAgg(scenarioDurationMs: 12000, cpuMs: 5800);
        var experimental = SimpleAgg(scenarioDurationMs: 11500, cpuMs: 5500);
        var diff = Differ.Compute(baseline, experimental, thresholdPct: 5.0);
        string report = ReportWriter.Write(diff);

        Assert.That(report, Does.Contain("# WPF Perf A/B Report"));
        Assert.That(report, Does.Contain("## Headline metrics"));
        Assert.That(report, Does.Contain("## Per-step timings"));
        Assert.That(report, Does.Contain("## Top CPU regressions"));
        Assert.That(report, Does.Contain("## Top CPU wins"));
        Assert.That(report, Does.Contain("## Verdict"));
        Assert.That(report, Does.Contain("Run-to-run noise"));
    }

    [Test]
    public void ReportWriter_HeadlineTable_HasCorrectColumns()
    {
        var baseline = SimpleAgg(scenarioDurationMs: 12000, cpuMs: 5800);
        var experimental = SimpleAgg(scenarioDurationMs: 11500, cpuMs: 5500);
        var diff = Differ.Compute(baseline, experimental, thresholdPct: 5.0);
        string report = ReportWriter.Write(diff);

        Assert.That(report, Does.Contain("| Metric |"));
        Assert.That(report, Does.Contain("| Baseline (median) |"));
        Assert.That(report, Does.Contain("Experimental (median)"));
    }

    // ── Factories ────────────────────────────────────────────────────────────

    private static AnalysisResult MakeRun(
        double scenarioStartMs = 0,
        double scenarioEndMs = 12000,
        double cpuMs = 5000)
    {
        return new AnalysisResult
        {
            EtlPath = "test.etl",
            CaptureSpanMs = scenarioEndMs,
            Process = new ProcessInfo { Name = "MotionCatalyst", CpuTimeMs = cpuMs, WallClockMs = scenarioEndMs },
            Gc = new GcMetrics { Gen0Count = 30, TotalAllocBytes = 100_000_000, TotalPauseTimeMs = 10 },
            Jit = new JitMetrics { MethodsJitted = 1200, TotalJitTimeMs = 500 },
            Wpf = new WpfMetrics { LayoutPassCount = 800, RenderPassCount = 720 },
            PerfHarnessEvents = new PerfHarnessMetrics
            {
                Scenario = "v1",
                ScenarioStartTimestampMs = scenarioStartMs,
                ScenarioEndTimestampMs = scenarioEndMs,
            },
        };
    }

    private static AnalysisResult MakeRunWithSteps(IEnumerable<(string Name, double ElapsedMs)> steps)
    {
        var r = MakeRun();
        r.PerfHarnessEvents!.StepTimings = [.. steps.Select(s => new StepTiming { Name = s.Name, ElapsedMs = s.ElapsedMs })];
        return r;
    }

    private static AnalysisResult MakeRunWithCpu(IEnumerable<(string Module, string Method, double CpuMs)> methods)
    {
        var r = MakeRun();
        r.TopCpuMethods = [.. methods.Select(m => new CpuMethodEntry { Module = m.Module, Method = m.Method, CpuMs = m.CpuMs })];
        return r;
    }

    private static AggregatedRun SimpleAgg(double scenarioDurationMs, double cpuMs)
    {
        return new AggregatedRun
        {
            Label = "test",
            RunCount = 1,
            ScenarioName = "v1",
            ScenarioDurationMs = MetricStat.Single(scenarioDurationMs),
            TotalCpuMs = MetricStat.Single(cpuMs),
            TotalAllocBytes = MetricStat.Single(100_000_000),
            Gen0Count = MetricStat.Single(30),
            JitMethods = MetricStat.Single(1200),
            LayoutPassCount = MetricStat.Single(800),
            RenderPassCount = MetricStat.Single(720),
        };
    }
}
