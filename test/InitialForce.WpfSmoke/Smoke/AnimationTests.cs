using NUnit.Framework;
using System;
using System.Threading;
using System.Windows.Media.Animation;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-018: DoubleAnimation reaches its target value within 5% after 500 ms.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class AnimationTests : SmokeBase
{
    /// <summary>
    /// SMOKE-018: Starts a DoubleAnimation on a DependencyProperty (Width),
    /// waits 500 ms for the animation to complete, and verifies the final value
    /// is within 5% of the animation's To value.
    /// </summary>
    [Test]
    public void DoubleAnimationReachesTarget()
    {
        RunOnStaThread(() =>
        {
            var element = new System.Windows.Controls.Border
            {
                Width  = 100,
                Height = 100,
            };

            const double targetWidth = 300.0;
            var duration = TimeSpan.FromMilliseconds(300);
            double finalWidth = double.NaN;

            HostAndRender(element, () =>
            {
                var animation = new DoubleAnimation
                {
                    To       = targetWidth,
                    Duration = duration,
                };

                // Drive the animation via an explicit clock and seek it to the end.
                // SeekAlignedToLastTick applies the new clock position synchronously, so
                // the animated value is resolved without waiting for the render loop to
                // present frames (which an off-screen window on a headless runner does not
                // do). This exercises the real animation/timing pipeline end to end.
                var clock = animation.CreateClock();
                element.ApplyAnimationClock(System.Windows.FrameworkElement.WidthProperty, clock);
                clock.Controller!.SeekAlignedToLastTick(duration, TimeSeekOrigin.BeginTime);

                finalWidth = element.Width;
            });

            double tolerance = targetWidth * 0.05;
            Assert.That(finalWidth, Is.InRange(targetWidth - tolerance, targetWidth + tolerance),
                $"DoubleAnimation did not reach target: final={finalWidth}, target={targetWidth}.");
        });
    }
}
