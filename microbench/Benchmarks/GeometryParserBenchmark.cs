using BenchmarkDotNet.Attributes;
using System.Windows.Media;

namespace WpfMicrobenchmarks.Benchmarks;

/// <summary>
/// Hot-path benchmark for AbbreviatedGeometryParser (PresentationCore).
///
/// Geometry.Parse is the SVG-style mini-language parser used wherever XAML
/// embeds path data (Path.Data, Geometry resources, etc.). It is pure
/// compute + allocation, no Dispatcher / WPF init required, so it makes an
/// ideal first real benchmark for the autoresearch loop. The parser
/// allocates throughout: substring slicing, double parsing intermediates,
/// Figure / Segment / PathFigure objects.
///
/// Generates a deterministic corpus of 100 simple paths once in
/// GlobalSetup; benchmark parses the whole corpus once per invocation.
/// Allocation per op should be substantial (100 × figure tree).
/// </summary>
[Config(typeof(AutoresearchConfig))]
public class GeometryParserBenchmark
{
    private string[] _paths = System.Array.Empty<string>();

    [GlobalSetup]
    public void Setup()
    {
        _paths = GeneratePathCorpus(count: 100, seed: 42);
    }

    [Benchmark(Description = "Geometry.Parse 100 simple paths (alloc + cpu)")]
    public int ParseCorpus()
    {
        int parsed = 0;
        foreach (var p in _paths)
        {
            var geo = Geometry.Parse(p);
            if (geo is not null) parsed++;
        }
        return parsed;
    }

    /// <summary>
    /// Deterministic SVG path corpus. Simple M/L/C commands, 8–24 segments
    /// per path. Seeded RNG keeps the input identical across A/B runs so
    /// the benchmark measures the parser, not corpus variability.
    /// </summary>
    private static string[] GeneratePathCorpus(int count, int seed)
    {
        var rnd = new System.Random(seed);
        var corpus = new string[count];
        var sb = new System.Text.StringBuilder(256);
        for (int i = 0; i < count; i++)
        {
            sb.Clear();
            int segments = 8 + rnd.Next(17);
            sb.Append('M').Append(' ').Append(rnd.Next(0, 1000)).Append(' ').Append(rnd.Next(0, 1000));
            for (int j = 0; j < segments; j++)
            {
                char op = (rnd.Next(3)) switch
                {
                    0 => 'L',
                    1 => 'C',
                    _ => 'L',
                };
                sb.Append(' ').Append(op);
                int coordCount = op == 'C' ? 6 : 2;
                for (int k = 0; k < coordCount; k++)
                {
                    sb.Append(' ').Append(rnd.Next(0, 1000));
                }
            }
            corpus[i] = sb.ToString();
        }
        return corpus;
    }
}
