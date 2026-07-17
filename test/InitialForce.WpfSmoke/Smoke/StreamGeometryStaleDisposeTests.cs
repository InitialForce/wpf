using NUnit.Framework;
using System;
using System.Globalization;
using System.Reflection;
using System.Threading;
using System.Windows;
using System.Windows.Media;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for the [ThreadStatic] StreamGeometryCallbackContext pool.
/// Public StreamGeometry.Open() must hand callers a fresh, never-pooled context,
/// so a stale Dispose of a previously-closed context can never revive a pooled
/// instance a later Open() is still populating. Pooling is reserved for the
/// internal, strictly-scoped parse path (OpenPooled).
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class StreamGeometryStaleDisposeTests : SmokeBase
{
    /// <summary>
    /// Skips on environments where WPF is not loadable (non-Windows), matching
    /// the rest of the smoke suite.
    /// </summary>
    private static void AssumeWpfAvailable()
    {
        try
        {
            _ = new StreamGeometry();
        }
        catch (Exception ex) when (
            ex is TypeLoadException or DllNotFoundException or PlatformNotSupportedException)
        {
            Assume.That(false, "WPF not available in this environment — skip on non-Windows.");
        }
    }

    /// <summary>
    /// The scenario the [ThreadStatic] pool broke: retain a closed context from one
    /// Open(), open a second geometry, then Dispose the first (stale) context while
    /// the second is still being built. On the pooled-public build the stale Dispose
    /// committed g2's half-built buffer and re-pooled the live instance, throwing
    /// ObjectDisposedException on the next g2 call and truncating g2. With fresh
    /// public contexts the stale Dispose no-ops on its own dead instance.
    /// </summary>
    [Test]
    public void StaleContextDispose_DoesNotCorruptSubsequentGeometry()
    {
        AssumeWpfAvailable();

        var g1 = new StreamGeometry();
        StreamGeometryContext ctx1 = g1.Open();
        ctx1.BeginFigure(new Point(0, 0), true, true);
        ctx1.LineTo(new Point(10, 0), true, false);
        ctx1.Close();

        var g2 = new StreamGeometry();
        StreamGeometryContext ctx2 = g2.Open();
        ctx2.BeginFigure(new Point(0, 0), true, true);
        ctx2.LineTo(new Point(20, 0), true, false);

        ((IDisposable)ctx1).Dispose();

        ctx2.LineTo(new Point(20, 20), true, false);
        ctx2.Close();

        var expected = new StreamGeometry();
        using (StreamGeometryContext c = expected.Open())
        {
            c.BeginFigure(new Point(0, 0), true, true);
            c.LineTo(new Point(20, 0), true, false);
            c.LineTo(new Point(20, 20), true, false);
        }

        Assert.That(
            g2.ToString(CultureInfo.InvariantCulture),
            Is.EqualTo(expected.ToString(CultureInfo.InvariantCulture)),
            "second geometry was truncated or clobbered by the stale dispose");
    }

    /// <summary>
    /// The internal parse path still pools contexts across calls. Two sequential
    /// parses through it must both produce correct geometry — pins that Acquire /
    /// ResetForReuse / pool-return remain sound across cycles.
    /// </summary>
    [Test]
    public void ParsePath_ProducesCorrectGeometryAcrossPooledReuse()
    {
        AssumeWpfAvailable();

        Geometry p1 = Geometry.Parse("M0,0 L10,10");
        Geometry p2 = Geometry.Parse("M0,0 L20,0 20,20 Z");

        Assert.That(p1.Bounds, Is.EqualTo(new Rect(0, 0, 10, 10)),
            "first pooled parse produced wrong bounds");
        Assert.That(p2.Bounds, Is.EqualTo(new Rect(0, 0, 20, 20)),
            "second pooled parse was corrupted by the first parse's pool return");
    }

    /// <summary>
    /// A context handed to public Open() must never be published to the
    /// [ThreadStatic] pool slot, or a later Open() could revive it under a stale
    /// reference.
    /// </summary>
    [Test]
    public void PublicOpen_ContextNeverEntersThreadStaticPool()
    {
        AssumeWpfAvailable();

        Type sgccType = typeof(StreamGeometry).Assembly.GetType(
            "System.Windows.Media.StreamGeometryCallbackContext", throwOnError: true)!;
        FieldInfo pooledField = sgccType.GetField(
            "_pooled", BindingFlags.NonPublic | BindingFlags.Static)!;

        var g = new StreamGeometry();
        StreamGeometryContext ctx = g.Open();
        ctx.BeginFigure(new Point(0, 0), true, true);
        ctx.LineTo(new Point(5, 5), true, false);
        ctx.Close();
        ((IDisposable)ctx).Dispose();

        object? slot = pooledField.GetValue(null);

        Assert.That(ReferenceEquals(slot, ctx), Is.False,
            "public Open() context leaked into the [ThreadStatic] pool");
    }
}
