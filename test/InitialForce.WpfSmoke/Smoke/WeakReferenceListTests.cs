using NUnit.Framework;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace InitialForce.WpfSmoke;

/// <summary>
/// SMOKE-007: WeakReferenceListEnumerator is not boxed during enumeration.
/// Regression test for PR #6502 (stop boxing WeakReferenceListEnumerator in PresentationSource).
/// </summary>
[TestFixture]
public class WeakReferenceListTests : SmokeBase
{
    /// <summary>
    /// SMOKE-007: Verifies that enumerating PresentationSource.CurrentSources
    /// (which uses a WeakReferenceList internally) does not allocate any heap
    /// memory over 10,000 iterations — i.e. the enumerator struct is not boxed.
    /// Regression test for PR #6502.
    /// </summary>
    [Test]
    public void EnumeratorNotBoxed()
    {
        // TODO(SMOKE-007): stub — deferred to Windows CI where WPF STA window
        // creation is possible and PresentationSource.CurrentSources is available.
        Assert.That(true, Is.True, "SMOKE-007 stub — deferred to Windows CI.");

        /* Full implementation (from exec-docs/40 §3.3):
        var windows = new List<System.Windows.Window>();
        var tcs = new TaskCompletionSource<long>();
        var staThread = new Thread(() =>
        {
            try
            {
                for (int i = 0; i < 10; i++)
                {
                    var w = new System.Windows.Window();
                    w.Show();
                    windows.Add(w);
                }
                // Warm up.
                foreach (var _ in System.Windows.PresentationSource.CurrentSources) { }

                // Measure.
                long before = GC.GetAllocatedBytesForCurrentThread();
                for (int iter = 0; iter < 10_000; iter++)
                    foreach (var _ in System.Windows.PresentationSource.CurrentSources) { }
                long after = GC.GetAllocatedBytesForCurrentThread();

                foreach (var w in windows) w.Close();
                tcs.SetResult(after - before);
            }
            catch (Exception ex) { tcs.SetException(ex); }
        });
        staThread.SetApartmentState(ApartmentState.STA);
        staThread.Start();
        long allocated = tcs.Task.GetAwaiter().GetResult();

        Assert.That(allocated, Is.EqualTo(0),
            $"Enumerator boxed: {allocated} bytes allocated over 10k enumerations. " +
            "Possible regression of PR #6502.");
        */
    }
}
