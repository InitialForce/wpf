using NUnit.Framework;
using System;
using System.Globalization;
using System.Threading;
using System.Windows.Threading;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for CulturePreservingExecutionContext culture pinning.
///
/// Stock WPF runs an unconditional <c>Thread.CurrentCulture = value</c> on every dispatcher
/// operation. On .NET 10 that setter converts the thread's implicit/default culture into an
/// explicitly pinned AsyncLocal value, so once any dispatcher operation has run, a later change
/// to <see cref="CultureInfo.DefaultThreadCurrentCulture"/> no longer retargets the UI thread.
/// The allocation-avoiding fast path in CPEC skips the setter when the value is reference-equal,
/// which would leave the culture implicit and let a later default change leak through. The first
/// operation per thread must still pin.
///
/// This runs a queued operation (which goes through CPEC.Run) on a fresh dispatcher thread whose
/// culture came from DefaultThreadCurrentCulture, then changes the default and asserts the thread
/// culture stayed pinned to the original — the stock-observable behavior.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class DispatcherCulturePinningTests : SmokeBase
{
    [Test]
    [CancelAfter(30_000)]
    public void QueuedOperation_PinsThreadCulture_AgainstLaterDefaultChange()
    {
        RunOnStaThread(() =>
        {
            CultureInfo originalDefault = CultureInfo.DefaultThreadCurrentCulture!;
            try
            {
                var pinned = CultureInfo.GetCultureInfo("en-US");
                var later = CultureInfo.GetCultureInfo("de-DE");

                // The fresh STA thread has no explicitly-set culture yet: it reads the process
                // default via fallback without pinning.
                CultureInfo.DefaultThreadCurrentCulture = pinned;

                Dispatcher dispatcher = Dispatcher.CurrentDispatcher;

                // A queued operation captures an ExecutionContext and dispatches through
                // CulturePreservingExecutionContext.Run, whose finally performs the pin.
                var frame = new DispatcherFrame();
                DispatcherOperation op = dispatcher.BeginInvoke(
                    DispatcherPriority.Normal,
                    new Action(() => { }));
                op.Completed += (_, _) => frame.Continue = false;
                op.Aborted += (_, _) => frame.Continue = false;
                Dispatcher.PushFrame(frame);

                // Retarget the process default. A pinned thread ignores it (stock behavior);
                // an unpinned thread would silently follow it.
                CultureInfo.DefaultThreadCurrentCulture = later;

                Assert.That(Thread.CurrentThread.CurrentCulture, Is.EqualTo(pinned),
                    "the dispatcher operation did not pin the thread culture; a later " +
                    "DefaultThreadCurrentCulture change leaked through, diverging from stock.");
            }
            finally
            {
                CultureInfo.DefaultThreadCurrentCulture = originalDefault;
            }
        });
    }
}
