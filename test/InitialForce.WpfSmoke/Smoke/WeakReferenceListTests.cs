using NUnit.Framework;
using System;
using System.Collections;
using System.Linq;
using System.Reflection;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-007: WeakReferenceListEnumerator is a struct enumerator, so iterating a
/// WeakReferenceList does not box.
/// Regression test for PR #6502 (stop boxing WeakReferenceListEnumerator in PresentationSource).
/// </summary>
[TestFixture]
public class WeakReferenceListTests : SmokeBase
{
    /// <summary>
    /// SMOKE-007: Regression guard for PR #6502.
    ///
    /// The fix makes <c>MS.Internal.WeakReferenceList&lt;T&gt;</c> expose a public,
    /// strongly-typed <c>GetEnumerator()</c> that returns a value-type
    /// (<c>WeakReferenceListEnumerator&lt;T&gt;</c>) enumerator. A <c>foreach</c> over the
    /// concrete list type then binds to that struct enumerator and allocates nothing —
    /// no boxed <c>IEnumerator</c> per enumeration.
    ///
    /// This is verified structurally rather than by measuring
    /// <c>PresentationSource.CurrentSources</c>: that public property is typed
    /// <c>IEnumerable</c>, so <c>foreach</c> over it goes through
    /// <c>IEnumerable.GetEnumerator()</c> and boxes the struct on every pass regardless
    /// of warmup — the internal non-boxing path the fix optimizes is not observable
    /// through it. Asserting that <c>GetEnumerator()</c> returns a value type catches a
    /// revert to a boxed (class / interface-returning) enumerator deterministically.
    /// </summary>
    [Test]
    public void EnumeratorNotBoxed()
    {
        // WeakReferenceList<T> is shared source compiled into WindowsBase (and consumed by
        // PresentationCore via InternalsVisibleTo). Resolve it from whichever WPF assembly
        // defines it rather than assuming a single one.
        var candidateAssemblies = new[]
        {
            typeof(System.Windows.DependencyObject).Assembly,  // WindowsBase
            typeof(System.Windows.Media.Visual).Assembly,      // PresentationCore
            typeof(System.Windows.Window).Assembly,            // PresentationFramework
        };

        Type? weakRefListOpen = candidateAssemblies
            .Select(a => a.GetType("MS.Internal.WeakReferenceList`1"))
            .FirstOrDefault(t => t is not null);

        Assert.That(weakRefListOpen, Is.Not.Null,
            "MS.Internal.WeakReferenceList<T> must exist in a patched WPF assembly.");

        // The public, strongly-typed GetEnumerator() — not the explicit
        // IEnumerable[<T>].GetEnumerator() interface implementations, which are private.
        MethodInfo? getEnumerator = weakRefListOpen!.GetMethod(
            "GetEnumerator", BindingFlags.Public | BindingFlags.Instance, Type.EmptyTypes);

        Assert.That(getEnumerator, Is.Not.Null,
            "WeakReferenceList<T> must expose a public parameterless GetEnumerator().");

        Type enumeratorType = getEnumerator!.ReturnType;
        Assert.That(enumeratorType.IsValueType, Is.True,
            $"WeakReferenceList<T>.GetEnumerator() must return a value type (struct) so " +
            $"foreach does not box the enumerator; returned '{enumeratorType.Name}' " +
            "(reference type). Possible regression of PR #6502.");

        Assert.That(typeof(IEnumerator).IsAssignableFrom(enumeratorType), Is.True,
            $"The struct enumerator '{enumeratorType.Name}' must implement IEnumerator so it " +
            "is usable in a non-boxing foreach.");
    }
}
