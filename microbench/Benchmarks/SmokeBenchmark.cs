using BenchmarkDotNet.Attributes;

namespace WpfMicrobenchmarks.Benchmarks;

/// <summary>
/// Smoke benchmark — exercises a single PresentationCore type so we can verify
/// the build pipeline + DLL swap + BDN runner end-to-end before authoring real
/// hot-path benchmarks. Should produce a deterministic, near-zero-alloc result.
/// </summary>
[Config(typeof(AutoresearchConfig))]
public class SmokeBenchmark
{
    private System.Windows.Size _a;
    private System.Windows.Size _b;

    [GlobalSetup]
    public void Setup()
    {
        _a = new System.Windows.Size(100.0, 50.0);
        _b = new System.Windows.Size(200.0, 75.0);
    }

    [Benchmark(Description = "smoke: Size.Equals (struct, no alloc)")]
    public bool SizeEquals() => _a.Equals(_b);
}
