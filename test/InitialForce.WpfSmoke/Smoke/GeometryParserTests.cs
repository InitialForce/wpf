using NUnit.Framework;
using System;
using System.Linq;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-001: AbbreviatedGeometryParser round-trip correctness (PR #6272).
/// SMOKE-002: Benchmark is in Perf/PerfHarness.cs (GeometryParserBench.ReadNumberBench).
/// </summary>
[TestFixture]
public class GeometryParserTests : SmokeBase
{
    private static readonly string[] _svgPaths = LoadSvgPaths();

    /// <summary>
    /// SMOKE-001: Parses 10,000 synthetic SVG path strings through Geometry.Parse.
    /// Verifies no exception is thrown and each result is non-null.
    /// Covers AbbreviatedGeometryParser.ReadNumber (PR #6272 span-slice change).
    /// </summary>
    [Test]
    public void RoundTrip10kPaths()
    {
        int errors = 0;
        foreach (var path in _svgPaths)
        {
            try
            {
                var geo = System.Windows.Media.Geometry.Parse(path);
                Assert.That(geo, Is.Not.Null,
                    $"Geometry.Parse returned null for: {path[..Math.Min(40, path.Length)]}");
            }
            catch (Exception ex)
            {
                errors++;
                TestContext.Error.WriteLine(
                    $"Parse failed for path: {path[..Math.Min(40, path.Length)]} — {ex.Message}");
            }
        }
        Assert.That(errors, Is.Zero, $"{errors} of {_svgPaths.Length} paths failed to parse.");
    }

    private static string[] LoadSvgPaths()
        => SvgPathGenerator.Generate(10_000, seed: 42);
}
