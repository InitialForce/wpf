using NUnit.Framework;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for the transform AdornerLayer hands to <see cref="Adorner.GetDesiredTransform"/>.
///
/// On the simple-affine fast path the layer materialises a MatrixTransform from the cached
/// ancestor matrix and passes it to the adorner's (user-overridable) GetDesiredTransform.
/// Stock WPF obtains that transform from TransformToAncestor, which returns a frozen instance;
/// a caller that mutates it throws. The fast path must match that contract and hand out a frozen
/// transform, otherwise adorner code observes a mutable instance where stock gives an immutable
/// one (IsFrozen differs; a mutation that stock rejects silently succeeds).
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class AdornerLayerFrozenTransformTests : SmokeBase
{
    [Test]
    public void GetDesiredTransform_OnSimpleAffinePath_ReceivesFrozenTransform()
    {
        RunOnStaThread(() =>
        {
            var adorned = new Border
            {
                Width = 100,
                Height = 100,
                Background = Brushes.White,
                // A non-identity pure-affine render transform keeps the layer on the simple
                // TryTransformToAncestorAsMatrix path and forces a non-identity materialised
                // MatrixTransform (identity short-circuits to the frozen Transform.Identity).
                RenderTransform = new ScaleTransform(1.5, 2.0),
            };
            var decorator = new AdornerDecorator { Child = adorned };

            HostAndRender(decorator, () =>
            {
                AdornerLayer layer = AdornerLayer.GetAdornerLayer(adorned);
                Assert.That(layer, Is.Not.Null, "no AdornerLayer available for the adorned element");

                var adorner = new CapturingAdorner(adorned);
                layer!.Add(adorner);
                DrainToRender(decorator.Dispatcher);
                decorator.UpdateLayout();

                Assert.That(adorner.Captured, Is.Not.Null,
                    "GetDesiredTransform was never invoked with a transform.");
                Assert.That(adorner.Captured, Is.InstanceOf<MatrixTransform>(),
                    "expected the simple-affine path to materialise a MatrixTransform.");
                Assert.That(((Transform)adorner.Captured!).IsFrozen, Is.True,
                    "the materialised MatrixTransform escaped to GetDesiredTransform unfrozen.");
            });
        });
    }

    private sealed class CapturingAdorner : Adorner
    {
        public GeneralTransform? Captured { get; private set; }

        public CapturingAdorner(UIElement adornedElement) : base(adornedElement) { }

        public override GeneralTransform GetDesiredTransform(GeneralTransform transform)
        {
            Captured = transform;
            return base.GetDesiredTransform(transform)!;
        }

        protected override void OnRender(DrawingContext drawingContext) { }
    }
}
