using NUnit.Framework;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Threading;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for the AdornerLayer collapsed-adorner layout loop.
///
/// UpdateElementAdorners has a branch that re-invalidates an adorner's layout when it is
/// ArrangeDirty but its transform/size/clip are unchanged. A <see cref="Visibility.Collapsed"/>
/// adorner short-circuits Measure/Arrange without ever clearing its ArrangeDirty bit, so the
/// branch re-invalidates the layer on every layout pass, which schedules another pass, which
/// re-enters the branch — a non-decaying loop that pins the UI thread and floods LayoutUpdated
/// until the adorner becomes visible again. The branch must therefore skip collapsed adorners.
///
/// This test adds a collapsed adorner, drives it ArrangeDirty, pumps a bounded number of
/// render cycles to reach steady state, then asserts that further cycles produce no additional
/// layout passes (the loop has decayed). Bounded loops + CancelAfter mean a live regression
/// fails an assertion rather than hanging.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class AdornerLayerCollapsedAdornerLoopTests : SmokeBase
{
    [Test]
    [CancelAfter(30_000)]
    public void CollapsedAdorner_ArrangeDirty_DoesNotLoopLayout()
    {
        RunOnStaThread(() =>
        {
            var adorned = new Border
            {
                Width = 100,
                Height = 100,
                Background = Brushes.White,
            };
            var decorator = new AdornerDecorator { Child = adorned };

            HostAndRender(decorator, () =>
            {
                Dispatcher dispatcher = decorator.Dispatcher;

                AdornerLayer layer = AdornerLayer.GetAdornerLayer(adorned);
                Assert.That(layer, Is.Not.Null, "no AdornerLayer available for the adorned element");

                var adorner = new PassiveAdorner(adorned)
                {
                    Visibility = Visibility.Collapsed,
                };
                layer!.Add(adorner);
                DrainToRender(dispatcher);

                int layoutPasses = 0;
                void OnLayoutUpdated(object? s, System.EventArgs e) => layoutPasses++;
                decorator.LayoutUpdated += OnLayoutUpdated;
                try
                {
                    // Drive the collapsed adorner ArrangeDirty through the real path.
                    adorner.InvalidateArrange();

                    // Pump render cycles to let any legitimate settling finish.
                    for (int i = 0; i < 15; i++)
                    {
                        dispatcher.Invoke(() => { }, DispatcherPriority.Render);
                    }

                    int settled = layoutPasses;

                    // A decayed layout produces no further passes; the loop bug produces one
                    // layout pass per pumped cycle indefinitely.
                    for (int i = 0; i < 10; i++)
                    {
                        dispatcher.Invoke(() => { }, DispatcherPriority.Render);
                    }

                    int extra = layoutPasses - settled;
                    Assert.That(extra, Is.Zero,
                        $"a collapsed, ArrangeDirty adorner kept the layout system looping: " +
                        $"{extra} additional layout passes over 10 idle render cycles.");
                }
                finally
                {
                    decorator.LayoutUpdated -= OnLayoutUpdated;
                }
            });
        });
    }

    /// <summary>Minimal adorner: no rendering, default (identity) desired transform.</summary>
    private sealed class PassiveAdorner : Adorner
    {
        public PassiveAdorner(UIElement adornedElement) : base(adornedElement) { }

        protected override void OnRender(DrawingContext drawingContext) { }
    }
}
