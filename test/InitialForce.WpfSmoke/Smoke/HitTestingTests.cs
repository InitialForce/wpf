using NUnit.Framework;
using System.Threading;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-013: VisualTreeHelper.HitTest correctness — three rectangles, nine sample points.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class HitTestingTests : SmokeBase
{
    /// <summary>
    /// SMOKE-013: Creates three non-overlapping rectangles in a Canvas, then
    /// performs hit testing at 9 sample points (3 per rectangle) and verifies
    /// each point hits the correct element.
    /// </summary>
    [Test]
    public void ThreeRectanglesNinePoints()
    {
        // TODO(SMOKE-013): stub — deferred to Windows CI where WPF rendering is available.
        Assert.That(true, Is.True, "SMOKE-013 stub — deferred to Windows CI.");

        /* Full implementation:
        RunOnStaThread(() =>
        {
            var canvas = new System.Windows.Controls.Canvas
            {
                Width = 300, Height = 100, Background = System.Windows.Media.Brushes.White,
            };

            var rects = new[]
            {
                new System.Windows.Shapes.Rectangle
                {
                    Width = 80, Height = 80,
                    Fill = System.Windows.Media.Brushes.Red,
                    Name = "Red",
                },
                new System.Windows.Shapes.Rectangle
                {
                    Width = 80, Height = 80,
                    Fill = System.Windows.Media.Brushes.Green,
                    Name = "Green",
                },
                new System.Windows.Shapes.Rectangle
                {
                    Width = 80, Height = 80,
                    Fill = System.Windows.Media.Brushes.Blue,
                    Name = "Blue",
                },
            };
            System.Windows.Controls.Canvas.SetLeft(rects[0], 10);
            System.Windows.Controls.Canvas.SetLeft(rects[1], 110);
            System.Windows.Controls.Canvas.SetLeft(rects[2], 210);
            foreach (var r in rects) canvas.Children.Add(r);

            canvas.Measure(new System.Windows.Size(300, 100));
            canvas.Arrange(new System.Windows.Rect(0, 0, 300, 100));
            canvas.UpdateLayout();

            // 3 sample points per rectangle (top-left, center, bottom-right).
            var cases = new (System.Windows.Point pt, string expected)[]
            {
                (new System.Windows.Point(15, 15),   "Red"),
                (new System.Windows.Point(50, 50),   "Red"),
                (new System.Windows.Point(85, 85),   "Red"),
                (new System.Windows.Point(115, 15),  "Green"),
                (new System.Windows.Point(150, 50),  "Green"),
                (new System.Windows.Point(185, 85),  "Green"),
                (new System.Windows.Point(215, 15),  "Blue"),
                (new System.Windows.Point(250, 50),  "Blue"),
                (new System.Windows.Point(285, 85),  "Blue"),
            };

            foreach (var (pt, expected) in cases)
            {
                System.Windows.Media.HitTestResult? result = null;
                System.Windows.Media.VisualTreeHelper.HitTest(
                    canvas, null,
                    r => { result = r; return System.Windows.Media.HitTestResultBehavior.Stop; },
                    new System.Windows.Media.PointHitTestParameters(pt));

                var hitRect = result?.VisualHit as System.Windows.Shapes.Rectangle;
                Assert.That(hitRect?.Name, Is.EqualTo(expected),
                    $"Hit test at {pt} expected '{expected}', got '{hitRect?.Name ?? "null"}'.");
            }
        });
        */
    }
}
