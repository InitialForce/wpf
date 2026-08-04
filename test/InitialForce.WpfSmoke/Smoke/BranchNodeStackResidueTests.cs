using NUnit.Framework;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;
using System.Windows;
using System.Windows.Controls;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for the thread-static <c>branchNodeStack</c> pooled by
/// <c>MS.Internal.UIElementHelper.InvalidateAutomationAncestors</c>.
///
/// Every FrameworkElement that has both a visual and a model parent pushes itself onto the
/// stack during the automation-ancestor walk, and in a linear tree those nodes are never
/// popped, so the walk ends with the ancestor chain still on the stack. Because the stack is
/// a thread-static pool reused across walks, leftover nodes would root those DependencyObjects
/// (and the trees they reference) until the next automation-driven walk on the thread. The walk
/// must therefore clear the stack on exit; this test drives the internal walk on a small tree
/// and asserts nothing is retained afterward.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class BranchNodeStackResidueTests : SmokeBase
{
    [Test]
    public void InvalidateAutomationAncestors_LeavesNoResidueOnPooledStack()
    {
        RunOnStaThread(() =>
        {
            // Both assignments establish visual AND logical parenting, so each middle element
            // has a non-null visual parent and a non-null model parent -> it gets pushed.
            var leaf = new Button();
            var middle = new StackPanel();
            middle.Children.Add(leaf);
            var root = new Border { Child = middle };

            Type helper = typeof(UIElement).Assembly.GetType("MS.Internal.UIElementHelper");
            Assert.That(helper, Is.Not.Null, "MS.Internal.UIElementHelper not found in PresentationCore.");

            MethodInfo invalidate = helper!.GetMethod(
                "InvalidateAutomationAncestors",
                BindingFlags.NonPublic | BindingFlags.Static);
            Assert.That(invalidate, Is.Not.Null, "InvalidateAutomationAncestors(DependencyObject) not found.");

            FieldInfo cacheField = helper.GetField(
                "_branchNodeStackCache",
                BindingFlags.NonPublic | BindingFlags.Static);
            Assert.That(cacheField, Is.Not.Null, "_branchNodeStackCache thread-static field not found.");

            // Walk up from the leaf; middle + leaf both have two parents so both are pushed.
            invalidate!.Invoke(null, new object[] { leaf });

            var stack = (Stack<DependencyObject>)cacheField!.GetValue(null);
            Assert.That(stack, Is.Not.Null, "the walk should have allocated the pooled stack.");
            Assert.That(stack!.Count, Is.EqualTo(0),
                "the pooled branchNodeStack retained ancestor nodes after the walk; it roots the tree until the next walk.");

            // Keep the tree alive to the end so nothing is collected before the assertion.
            GC.KeepAlive(root);
        });
    }
}
