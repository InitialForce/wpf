using NUnit.Framework;
using System;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Linq;
using System.Reflection;
using System.Windows.Data;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-003: ListCollectionView.SortOf50kItems — sort path under 200 ms.
/// SMOKE-004: PrepareComparerZeroAllocs — PR #6511 delegate allocation regression.
/// </summary>
[TestFixture]
public class ListCollectionViewTests : SmokeBase
{
    /// <summary>
    /// SMOKE-003: Binds 50,000 items to a ListCollectionView with a sort description
    /// and verifies the sort completes in under 200 ms.
    /// </summary>
    [Test]
    public void SortOf50kItems()
    {
        var source = new ObservableCollection<string>(
            Enumerable.Range(0, 50_000).Select(i => $"item-{i:D6}"));
        var view = (ListCollectionView)CollectionViewSource.GetDefaultView(source);
        view.SortDescriptions.Add(
            new SortDescription("", ListSortDirection.Ascending));

        var sw = System.Diagnostics.Stopwatch.StartNew();
        view.Refresh();
        sw.Stop();

        Assert.That(sw.ElapsedMilliseconds, Is.LessThan(200),
            $"Sort of 50k items took {sw.ElapsedMilliseconds} ms (limit: 200 ms).");
    }

    /// <summary>
    /// SMOKE-004: Regression guard for PR #6511 (PrepareComparer delegate allocation fix).
    ///
    /// The fix routes comparer preparation through a <c>static</c> method that threads the
    /// owning view via a non-capturing <c>Func&lt;object, CollectionView&gt;</c> plus an
    /// <c>object state</c>, instead of an instance method whose call site captured
    /// <c>this</c> into a fresh per-refresh closure delegate. A non-capturing static
    /// lambda is cached by the compiler in a static field, so the comparer-prep path
    /// allocates no delegate.
    ///
    /// This invariant is verified structurally rather than by measuring
    /// <c>Refresh()</c>: a full Refresh unavoidably rebuilds its internal sorted array
    /// (<c>new ArrayList(size)</c>) and constructs a <c>SortFieldComparer</c> every call,
    /// so its steady-state allocation is inherently non-zero and would swamp the single
    /// delegate the fix removed. Asserting the refactored signature catches a revert to
    /// the closure-allocating design deterministically.
    /// </summary>
    [Test]
    public void PrepareComparerZeroAllocs()
    {
        MethodInfo? prepare = typeof(ListCollectionView).GetMethod(
            "PrepareComparer",
            BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public);

        Assert.That(prepare, Is.Not.Null,
            "ListCollectionView.PrepareComparer must exist as a static method. A revert to " +
            "an instance method reintroduces the per-refresh closure delegate (PR #6511).");
        Assert.That(prepare!.IsStatic, Is.True,
            "PrepareComparer must be static so its call site can pass a cached, non-capturing " +
            "delegate instead of capturing 'this' into a new closure (PR #6511).");

        ParameterInfo[] parms = prepare.GetParameters();
        bool threadsStateViaDelegate = parms.Any(p =>
            p.ParameterType == typeof(Func<object, CollectionView>))
            && parms.Any(p => p.ParameterType == typeof(object));

        Assert.That(threadsStateViaDelegate, Is.True,
            "PrepareComparer must thread the owning view via a non-capturing " +
            "Func<object, CollectionView> plus an object state parameter, the closure-free " +
            "design of PR #6511. Signature was: " +
            $"({string.Join(", ", parms.Select(p => p.ParameterType.Name))}).");
    }
}
