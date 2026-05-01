using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Cpu;
using Microsoft.Windows.EventTracing.Symbols;
using WpfPerfAnalyze.Models;

namespace WpfPerfAnalyze.Analyzers;

/// <summary>
/// Builds a top-N CPU method flame list from CPU sampling data.
///
/// Uses ICpuSampleDataSource.Samples to enumerate all CPU samples, filters to the
/// target process, and aggregates Weight (sample duration) by top stack frame.
/// Symbol resolution is attempted via ISymbolDataSource; if symbols are not
/// available the frame is reported as "???" with the module name where known.
/// </summary>
internal sealed class CpuSamplingAnalyzer
{
    private readonly int _topN;

    public CpuSamplingAnalyzer(int topN)
    {
        _topN = topN;
    }

    public List<CpuMethodEntry> Analyze(
        IPendingResult<ICpuSampleDataSource> cpuSamplingPending,
        int targetPid,
        List<string> warnings)
    {
        if (!cpuSamplingPending.HasResult)
        {
            warnings.Add("CpuSampleDataSource not available — no CPU sampling data.");
            return [];
        }

        var samples = cpuSamplingPending.Result.Samples;

        // Key: (moduleName, symbolName) → (totalWeightMs, sampleCount)
        var aggregated = new Dictionary<(string module, string symbol), (double weightMs, int count)>(
            TupleStringComparer.s_ordinalIgnoreCase);

        foreach (var sample in samples)
        {
            if (sample.Process?.Id != targetPid)
                continue;

            double weightMs = (double)sample.Weight.TotalMilliseconds;
            if (weightMs <= 0)
                continue;

            var (module, symbol) = ResolveTopFrame(sample);

            var key = (module, symbol);
            if (aggregated.TryGetValue(key, out var existing))
                aggregated[key] = (existing.weightMs + weightMs, existing.count + 1);
            else
                aggregated[key] = (weightMs, 1);
        }

        return [.. aggregated
            .OrderByDescending(kvp => kvp.Value.weightMs)
            .Take(_topN)
            .Select(kvp => new CpuMethodEntry
            {
                Module = kvp.Key.module,
                Method = kvp.Key.symbol,
                CpuMs = Math.Round(kvp.Value.weightMs, 3),
                Samples = kvp.Value.count,
            })];
    }

    private static (string module, string symbol) ResolveTopFrame(ICpuSample sample)
    {
        // Try the first frame in the call stack if available
        var stack = sample.Stack;
        if (stack != null)
        {
            foreach (var frame in stack.Frames)
            {
                return ResolveStackFrame(frame);
            }
        }

        string imageName = sample.Image?.FileName ?? "?";
        if (imageName.Contains('\\') || imageName.Contains('/'))
            imageName = Path.GetFileName(imageName);
        return (imageName, "???");
    }

    private static (string module, string symbol) ResolveStackFrame(StackFrame frame)
    {
        string module = frame.Image?.FileName ?? "?";
        if (module.Contains('\\') || module.Contains('/'))
            module = Path.GetFileName(module);
        string symbol = frame.Symbol?.FunctionName ?? "???";
        return (module, symbol);
    }

    private sealed class TupleStringComparer : IEqualityComparer<(string, string)>
    {
        public static readonly TupleStringComparer s_ordinalIgnoreCase =
            new TupleStringComparer(StringComparer.OrdinalIgnoreCase);

        private readonly StringComparer _inner;

        private TupleStringComparer(StringComparer inner) => _inner = inner;

        public bool Equals((string, string) x, (string, string) y) =>
            _inner.Equals(x.Item1, y.Item1) && _inner.Equals(x.Item2, y.Item2);

        public int GetHashCode((string, string) obj) =>
            HashCode.Combine(_inner.GetHashCode(obj.Item1), _inner.GetHashCode(obj.Item2));
    }
}
