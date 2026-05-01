using System.Text;
using WpfPerfDiff.Models;

namespace WpfPerfDiff;

/// <summary>
/// Emits a markdown A/B report from a <see cref="DiffResult"/>.
/// </summary>
public static class ReportWriter
{
    public static string Write(DiffResult diff)
    {
        var sb = new StringBuilder(4096);
        var b = diff.Baseline;
        var e = diff.Experimental;

        // ── Header ─────────────────────────────────────────────────────────────
        sb.AppendLine("# WPF Perf A/B Report");
        sb.AppendLine();
        string scenario = b.ScenarioName ?? e.ScenarioName ?? "(unknown)";
        sb.AppendLine($"**Scenario:** {scenario}  ");
        sb.AppendLine($"**Baseline:** {b.Label}  ");
        sb.AppendLine($"**Experimental:** {e.Label}  ");
        sb.AppendLine($"**Generated:** {diff.GeneratedUtc:O}  ");
        sb.AppendLine($"**Threshold for flagging:** {diff.ThresholdPct:F0}%");
        sb.AppendLine();

        // ── Noise warnings ─────────────────────────────────────────────────────
        if (diff.NoiseWarnings.Count > 0)
        {
            sb.AppendLine("> **Noise warnings:**");
            foreach (var w in diff.NoiseWarnings)
                sb.AppendLine($"> - {w.Side} `{w.MetricName}`: stddev={FormatStat(w.StdDev, "")} ({w.StdDevPct:F1}% of median={FormatStat(w.Median, "")})");
            sb.AppendLine();
        }

        // ── Headline metrics ───────────────────────────────────────────────────
        sb.AppendLine("## Headline metrics");
        sb.AppendLine();
        if (diff.HeadlineMetrics.Count > 0)
        {
            sb.AppendLine("| Metric | Baseline (median) | Experimental (median) | Δ | Δ% | Flag |");
            sb.AppendLine("|--------|-------------------|-----------------------|---|-----|------|");
            foreach (var m in diff.HeadlineMetrics)
                sb.AppendLine(HeadlineRow(m));
        }
        else
        {
            sb.AppendLine("_No headline metrics available._");
        }
        sb.AppendLine();
        sb.AppendLine("Flag legend: ✓ = win (>threshold), ⚠ = large change (>2× threshold), ✗ = regression, – = within threshold");
        sb.AppendLine();

        // ── Per-step timings ───────────────────────────────────────────────────
        sb.AppendLine("## Per-step timings");
        sb.AppendLine();
        if (diff.StepTimings.Count > 0)
        {
            sb.AppendLine("| Step | Baseline (ms) | Experimental (ms) | Δ | Δ% | Flag |");
            sb.AppendLine("|------|---------------|-------------------|---|-----|------|");
            foreach (var m in diff.StepTimings)
                sb.AppendLine(HeadlineRow(m));
        }
        else
        {
            sb.AppendLine("_No per-step timings available (requires PerfHarnessEvents in the analyze JSON)._");
        }
        sb.AppendLine();

        // ── CPU regressions ────────────────────────────────────────────────────
        sb.AppendLine($"## Top CPU regressions (>{diff.ThresholdPct:F0}%)");
        sb.AppendLine();
        AppendMethodTable(sb, diff.TopCpuRegressions, "ms");

        // ── CPU wins ──────────────────────────────────────────────────────────
        sb.AppendLine($"## Top CPU wins (>{diff.ThresholdPct:F0}%)");
        sb.AppendLine();
        AppendMethodTable(sb, diff.TopCpuWins, "ms");

        // ── Alloc regressions ──────────────────────────────────────────────────
        sb.AppendLine($"## Top allocation regressions (>{diff.ThresholdPct:F0}%)");
        sb.AppendLine();
        AppendMethodTable(sb, diff.TopAllocRegressions, "B");

        // ── Alloc wins ────────────────────────────────────────────────────────
        sb.AppendLine($"## Top allocation wins (>{diff.ThresholdPct:F0}%)");
        sb.AppendLine();
        AppendMethodTable(sb, diff.TopAllocWins, "B");

        // ── Methods exclusive to one side ──────────────────────────────────────
        if (diff.MethodsOnlyInBaseline.Count > 0 || diff.MethodsOnlyInExperimental.Count > 0)
        {
            sb.AppendLine("## Methods exclusive to one side");
            sb.AppendLine();
            if (diff.MethodsOnlyInBaseline.Count > 0)
            {
                sb.AppendLine($"**Only in baseline** ({diff.MethodsOnlyInBaseline.Count} methods — not present in top CPU list of experimental):");
                sb.AppendLine();
                foreach (var m in diff.MethodsOnlyInBaseline.Take(20))
                    sb.AppendLine($"- `{m}`");
                if (diff.MethodsOnlyInBaseline.Count > 20)
                    sb.AppendLine($"- _(and {diff.MethodsOnlyInBaseline.Count - 20} more)_");
                sb.AppendLine();
            }
            if (diff.MethodsOnlyInExperimental.Count > 0)
            {
                sb.AppendLine($"**Only in experimental** ({diff.MethodsOnlyInExperimental.Count} methods — not present in top CPU list of baseline):");
                sb.AppendLine();
                foreach (var m in diff.MethodsOnlyInExperimental.Take(20))
                    sb.AppendLine($"- `{m}`");
                if (diff.MethodsOnlyInExperimental.Count > 20)
                    sb.AppendLine($"- _(and {diff.MethodsOnlyInExperimental.Count - 20} more)_");
                sb.AppendLine();
            }
        }

        // ── Run-to-run noise ───────────────────────────────────────────────────
        sb.AppendLine("## Run-to-run noise");
        sb.AppendLine();
        if (b.RunCount >= 2 || e.RunCount >= 2)
        {
            sb.AppendLine("| Metric | Baseline stddev | Experimental stddev |");
            sb.AppendLine("|--------|-----------------|---------------------|");
            AppendNoiseRow(sb, "Scenario duration (ms)", b.ScenarioDurationMs, e.ScenarioDurationMs);
            AppendNoiseRow(sb, "Total CPU (ms)", b.TotalCpuMs, e.TotalCpuMs);
            AppendNoiseRow(sb, "Total alloc (MB)", ScaleStat(b.TotalAllocBytes, 1.0/1_048_576.0),
                ScaleStat(e.TotalAllocBytes, 1.0/1_048_576.0));
            AppendNoiseRow(sb, "GC gen0 count", b.Gen0Count, e.Gen0Count);
        }
        else
        {
            sb.AppendLine("_Single run on each side — no stddev available._");
        }
        sb.AppendLine();

        // ── Verdict ────────────────────────────────────────────────────────────
        sb.AppendLine("## Verdict");
        sb.AppendLine();
        sb.AppendLine(diff.Verdict);
        sb.AppendLine();

        return sb.ToString();
    }

    // ── Formatting helpers ─────────────────────────────────────────────────────

    private static string HeadlineRow(MetricDelta m)
    {
        string bStr = FormatStatWithUnit(m.BaselineStat, m.Unit);
        string eStr = FormatStatWithUnit(m.ExperimentalStat, m.Unit);
        string delta = m.BaselineStat.IsAvailable && m.ExperimentalStat.IsAvailable
            ? FormatDelta(m.Delta, m.Unit) : "–";
        string deltaPct = m.BaselineStat.IsAvailable && m.ExperimentalStat.IsAvailable
            ? $"{m.DeltaPct:+0.0;-0.0}%" : "–";
        string flag = FlagString(m.Flag);
        return $"| {m.Name} | {bStr} | {eStr} | {delta} | {deltaPct} | {flag} |";
    }

    private static void AppendMethodTable(StringBuilder sb, List<MethodDelta> methods, string unit)
    {
        if (methods.Count == 0)
        {
            sb.AppendLine("_None above threshold._");
            sb.AppendLine();
            return;
        }
        sb.AppendLine($"| Method | Module | Baseline ({unit}) | Experimental ({unit}) | Δ | Δ% |");
        sb.AppendLine("|--------|--------|-------------------|-----------------------|---|-----|");
        foreach (var m in methods)
        {
            string bStr = FormatValue(m.BaselineValue, unit);
            string eStr = FormatValue(m.ExperimentalValue, unit);
            string delta = FormatDelta(m.Delta, unit);
            sb.AppendLine($"| `{Escape(m.Method)}` | {Escape(m.Module)} | {bStr} | {eStr} | {delta} | {m.DeltaPct:+0.0;-0.0}% |");
        }
        sb.AppendLine();
    }

    private static void AppendNoiseRow(StringBuilder sb, string name, MetricStat b, MetricStat e)
    {
        string bStd = b.IsAvailable && b.StdDev.HasValue ? $"{b.StdDev.Value:F1}" : "–";
        string eStd = e.IsAvailable && e.StdDev.HasValue ? $"{e.StdDev.Value:F1}" : "–";
        sb.AppendLine($"| {name} | {bStd} | {eStd} |");
    }

    private static string FormatStatWithUnit(MetricStat s, string unit)
    {
        if (!s.IsAvailable) return "–";
        string val = FormatValue(s.Median, unit);
        if (s.StdDev.HasValue)
            val += $" ± {FormatValue(s.StdDev.Value, unit)}";
        return val;
    }

    private static string FormatValue(double v, string unit) =>
        unit == "MB" ? $"{v:F1}" :
        unit == "B" ? FormatBytes(v) :
        unit == "ms" ? $"{v:F0}" :
        // Count metrics (unit = ""): show as integer if whole number, else 1 dp
        v == Math.Floor(v) ? $"{v:F0}" : $"{v:F1}";

    private static string FormatBytes(double b) =>
        b >= 1_073_741_824 ? $"{b / 1_073_741_824.0:F1} GB" :
        b >= 1_048_576 ? $"{b / 1_048_576.0:F1} MB" :
        b >= 1024 ? $"{b / 1024.0:F1} KB" :
        $"{b:F0} B";

    private static string FormatDelta(double d, string unit)
    {
        string s = FormatValue(Math.Abs(d), unit);
        return d >= 0 ? $"+{s}" : $"-{s}";
    }

    private static string FormatStat(double v, string unit) => FormatValue(v, unit);

    private static string FlagString(DeltaFlag flag) => flag switch
    {
        DeltaFlag.BigWin => "⚠ win",
        DeltaFlag.Win => "✓ win",
        DeltaFlag.BigRegression => "⚠ ✗ regression",
        DeltaFlag.Regression => "✗ regression",
        DeltaFlag.Neutral => "–",
        _ => "–",
    };

    private static string Escape(string s) => s.Replace("|", "\\|").Replace("`", "'");

    private static MetricStat ScaleStat(MetricStat s, double factor) =>
        s.IsAvailable
            ? new MetricStat(s.Median * factor, s.StdDev.HasValue ? s.StdDev.Value * factor : null, true)
            : MetricStat.Zero;
}
