using NUnit.Framework;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression coverage for the pooled <see cref="System.Windows.UIElement"/> hit-test
/// infrastructure: a re-entrant <c>InputHitTest</c> fired from a custom
/// <c>HitTestCore</c> must not share, and clobber, the outer walk's pooled
/// <c>PointHitTestParameters</c>.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class ReentrantInputHitTestTests : SmokeBase
{
    /// <summary>
    /// A 200x100 Canvas holds a probe at x[0,100] and a border at x[100,200]. The probe's
    /// <c>HitTestCore</c> re-enters the root's <c>InputHitTest</c> at a point inside the
    /// border before delegating to its base bounds check. When the two hit-tests share one
    /// pooled parameter object, the nested call's <c>SetHitPoint</c> rewrites the outer
    /// walk's hit point, the probe's own bounds check then reads the border's coordinates
    /// and misses, and the outer result collapses onto the canvas background. Emptying the
    /// pool slot for the duration of each walk isolates the two, so the nested call resolves
    /// to the border and the outer call resolves to the probe.
    /// </summary>
    [Test]
    public void NestedInputHitTest_DoesNotClobberOuterWalk()
    {
        var canvas = new Canvas
        {
            Width = 200,
            Height = 100,
            Background = Brushes.White,
        };

        var border = new Border
        {
            Width = 100,
            Height = 100,
            Background = Brushes.Red,
        };
        Canvas.SetLeft(border, 100);
        Canvas.SetTop(border, 0);

        var probe = new ReentrantProbe(canvas, new Point(150, 50))
        {
            Width = 100,
            Height = 100,
        };
        Canvas.SetLeft(probe, 0);
        Canvas.SetTop(probe, 0);

        canvas.Children.Add(probe);
        canvas.Children.Add(border);

        // Host the canvas in a live, rendered window so InputHitTest actually walks the
        // tree: a detached Measure/Arrange leaves the visual without a PresentationSource
        // and InputHitTest returns null, so the re-entrancy path is never exercised.
        IInputElement? outer = null;
        HostAndRender(canvas, () =>
        {
            outer = canvas.InputHitTest(new Point(50, 50));
        });

        Assert.That(probe.NestedResult, Is.SameAs(border),
            "nested InputHitTest at (150,50) should resolve to the border");
        Assert.That(outer, Is.SameAs(probe),
            "outer InputHitTest at (50,50) should resolve to the probe; a shared pooled " +
            "PointHitTestParameters would clobber the probe's hit point and fall back to the canvas");
    }

    /// <summary>
    /// A hit-testable element whose <c>HitTestCore</c> re-enters the root's
    /// <c>InputHitTest</c> at a fixed point before performing its own bounds check.
    /// </summary>
    private sealed class ReentrantProbe : FrameworkElement
    {
        private readonly UIElement _root;
        private readonly Point _nestedPoint;

        public ReentrantProbe(UIElement root, Point nestedPoint)
        {
            _root = root;
            _nestedPoint = nestedPoint;
        }

        public IInputElement? NestedResult { get; private set; }

        protected override void OnRender(DrawingContext drawingContext)
        {
            // Draw over the full bounds so base.HitTestCore has geometry to test against.
            drawingContext.DrawRectangle(Brushes.LightBlue, null, new Rect(new Point(), RenderSize));
        }

        protected override HitTestResult HitTestCore(PointHitTestParameters hitTestParameters)
        {
            NestedResult = _root.InputHitTest(_nestedPoint);
            return base.HitTestCore(hitTestParameters);
        }
    }
}
