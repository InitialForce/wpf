using NUnit.Framework;
using System;
using System.Reflection;
using System.Threading;
using System.Windows;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression coverage for the pooled <c>HwndStyleManager</c> activation invariant:
/// every StartManaging activation must begin with Dirty == false, including when the
/// pooled instance parked dirty on a prior cycle whose final Flush was skipped because
/// the HWND had not materialized (Handle == IntPtr.Zero).
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class PooledStyleManagerDirtyTests : SmokeBase
{
    /// <summary>
    /// A Window that is never shown keeps IsSourceWindowNull == true and Handle == IntPtr.Zero,
    /// so StartManaging takes the source-null branch and Flush is skipped on Dispose. The first
    /// cycle is marked Dirty and disposed, parking the manager dirty into the per-Window pool.
    /// The second StartManaging must hand back that same instance with Dirty reset to false.
    /// </summary>
    [Test]
    public void PooledStyleManager_ReuseStartsClean_AfterDirtyPark()
    {
        var window = new Window();

        Type managerType = typeof(Window).Assembly.GetType(
            "System.Windows.Window+HwndStyleManager", throwOnError: true)!;

        MethodInfo start = managerType.GetMethod(
            "StartManaging",
            BindingFlags.NonPublic | BindingFlags.Static)!;
        PropertyInfo dirty = managerType.GetProperty(
            "Dirty",
            BindingFlags.NonPublic | BindingFlags.Instance)!;

        // Cycle 1: activate, dirty, dispose. Flush is skipped (Handle == IntPtr.Zero),
        // so the instance parks into _freedStyleManager with _fDirty still true.
        object m1 = start.Invoke(null, new object[] { window, 0, 0 })!;
        dirty.SetValue(m1, true);
        ((IDisposable)m1).Dispose();

        // Cycle 2: source still null — reuse takes the IsSourceWindowNull branch.
        object m2 = start.Invoke(null, new object[] { window, 0, 0 })!;
        try
        {
            Assert.That(ReferenceEquals(m1, m2), Is.True,
                "precondition: pool did not hand back the parked instance");
            Assert.That((bool)dirty.GetValue(m2)!, Is.False,
                "reused HwndStyleManager inherited stale Dirty from the previous cycle");
        }
        finally
        {
            ((IDisposable)m2).Dispose();
        }
    }
}
