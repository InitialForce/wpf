using NUnit.Framework;
using System.Threading;
using System.Windows;
using System.Windows.Media;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for the GeometryGroup serialized-data memoization (DESKTOP-12279).
///
/// GeometryGroup caches the serialized form it hands to the native bounds routine so the
/// per-frame hit-test-bounds precompute stops re-materializing the whole group. The cache
/// must be invalidated by Freezable.OnChanged for every change that alters the serialized
/// subtree, and must not change any observable bound. These tests exercise the public
/// bounds API (no stock DLLs required): every mutation must be reflected in the next Bounds
/// read, repeated reads must be stable, and a group's stroked bounds must match the
/// equivalent non-group geometry (a group Transform is geometry-only and must not scale the
/// pen).
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class GeometryGroupBoundsCacheTests : SmokeBase
{
    private const double Tol = 1e-6;

    private static void AssertRectEqual(Rect expected, Rect actual, string because)
    {
        Assert.Multiple(() =>
        {
            Assert.That(actual.X, Is.EqualTo(expected.X).Within(Tol), $"{because} (X)");
            Assert.That(actual.Y, Is.EqualTo(expected.Y).Within(Tol), $"{because} (Y)");
            Assert.That(actual.Width, Is.EqualTo(expected.Width).Within(Tol), $"{because} (Width)");
            Assert.That(actual.Height, Is.EqualTo(expected.Height).Within(Tol), $"{because} (Height)");
        });
    }

    [Test]
    public void ChildGeometryChange_InvalidatesCachedBounds()
    {
        RunOnStaThread(() =>
        {
            var child = new RectangleGeometry(new Rect(0, 0, 10, 10));
            var group = new GeometryGroup();
            group.Children.Add(child);

            AssertRectEqual(new Rect(0, 0, 10, 10), group.Bounds, "initial bounds");

            child.Rect = new Rect(0, 0, 30, 20);
            AssertRectEqual(new Rect(0, 0, 30, 20), group.Bounds,
                "a child geometry change did not invalidate the cached bounds");
        });
    }

    [Test]
    public void ChildrenCollectionChange_InvalidatesCachedBounds()
    {
        RunOnStaThread(() =>
        {
            var group = new GeometryGroup();
            group.Children.Add(new RectangleGeometry(new Rect(0, 0, 10, 10)));

            AssertRectEqual(new Rect(0, 0, 10, 10), group.Bounds, "initial bounds");

            group.Children.Add(new RectangleGeometry(new Rect(20, 20, 10, 10)));
            AssertRectEqual(new Rect(0, 0, 30, 30), group.Bounds,
                "adding a child did not invalidate the cached bounds");

            group.Children.RemoveAt(1);
            AssertRectEqual(new Rect(0, 0, 10, 10), group.Bounds,
                "removing a child did not invalidate the cached bounds");
        });
    }

    [Test]
    public void GroupTransformChange_InvalidatesCachedBounds()
    {
        RunOnStaThread(() =>
        {
            var group = new GeometryGroup();
            group.Children.Add(new RectangleGeometry(new Rect(0, 0, 10, 10)));

            AssertRectEqual(new Rect(0, 0, 10, 10), group.Bounds, "initial bounds");

            // The group transform is baked into the serialized points, so a change to it
            // must invalidate the cache.
            group.Transform = new ScaleTransform(2, 3);
            AssertRectEqual(new Rect(0, 0, 20, 30), group.Bounds,
                "a group Transform change did not invalidate the cached bounds");
        });
    }

    [Test]
    public void RepeatedBoundsReads_AreStable()
    {
        RunOnStaThread(() =>
        {
            var group = new GeometryGroup();
            group.Children.Add(new RectangleGeometry(new Rect(1, 2, 10, 20)));
            group.Children.Add(new EllipseGeometry(new Point(30, 30), 5, 5));

            Rect first = group.Bounds;
            for (int i = 0; i < 5; i++)
            {
                AssertRectEqual(first, group.Bounds, "repeated Bounds reads diverged");
            }
        });
    }

    [Test]
    public void StrokedBounds_MatchNonGroupEquivalent_PenNotScaledByGroupTransform()
    {
        RunOnStaThread(() =>
        {
            var pen = new Pen(Brushes.Black, 4);

            // No-transform group vs the child alone: the memoized group must produce the same
            // stroked bounds as the standalone geometry.
            var plainChild = new RectangleGeometry(new Rect(0, 0, 10, 10));
            var plainGroup = new GeometryGroup();
            plainGroup.Children.Add(new RectangleGeometry(new Rect(0, 0, 10, 10)));
            AssertRectEqual(plainChild.GetRenderBounds(pen), plainGroup.GetRenderBounds(pen),
                "group stroked bounds differ from the standalone geometry");

            // A group Transform is geometry-only: under ScaleTransform(10,10) the stroke radius
            // must NOT scale with it. The equivalent is a single geometry carrying the same
            // transform, whose pen is likewise unscaled.
            var scaledRef = new RectangleGeometry(new Rect(0, 0, 10, 10))
            {
                Transform = new ScaleTransform(10, 10),
            };
            var scaledGroup = new GeometryGroup
            {
                Transform = new ScaleTransform(10, 10),
            };
            scaledGroup.Children.Add(new RectangleGeometry(new Rect(0, 0, 10, 10)));
            AssertRectEqual(scaledRef.GetRenderBounds(pen), scaledGroup.GetRenderBounds(pen),
                "group Transform scaled the pen (stroked bounds diverged from the geometry-only equivalent)");
        });
    }
}
