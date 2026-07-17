using NUnit.Framework;
using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for await-continuation ordering across sibling dispatcher operations.
///
/// A queued dispatcher operation runs under a <see cref="SynchronizationContext"/> whose
/// instance identity is compared by reference in
/// <c>SynchronizationContextAwaitTaskContinuation.Run</c>: a continuation is inlined into
/// the completer's call stack only when the captured context is reference-equal to
/// <see cref="SynchronizationContext.Current"/> at completion time. Each dispatcher operation
/// must therefore see a distinct <see cref="DispatcherSynchronizationContext"/> instance, so
/// that an <c>await</c> captured in one operation and completed from a sibling operation is
/// posted (runs after the completer returns) rather than inlined (runs inside the completer,
/// mid-statement).
///
/// If a single DispatcherSynchronizationContext instance is shared across operations of the
/// same (dispatcher, priority), the reference comparison flips to true and the continuation
/// runs inline, reordering async continuations relative to the code that completed the task.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class DispatcherAwaitOrderingTests : SmokeBase
{
    /// <summary>
    /// Two Normal-priority operations on one STA dispatcher: operation A awaits a
    /// <see cref="TaskCompletionSource{TResult}"/>, operation B (queued after A) completes it.
    /// Because A and B run under distinct SynchronizationContext instances, A's continuation is
    /// posted behind B rather than inlined into B's <c>SetResult</c>, so the observed order is
    /// [A:before-await, B:before-set, B:after-set, A:continuation].
    ///
    /// With a shared per-(dispatcher, priority) context the continuation inlines inside
    /// <c>SetResult</c>, producing [A:before-await, B:before-set, A:continuation, B:after-set].
    ///
    /// Deterministic: single thread, FIFO same-priority ordering, no sleeps. A DispatcherTimer
    /// backstop and CancelAfter guard exit the pump if a regression ever leaves the frame
    /// running, so a failure is a failed assertion rather than a hang.
    /// </summary>
    [Test]
    [CancelAfter(30_000)]
    public void AwaitContinuation_CompletedFromSiblingOperation_IsPostedNotInlined()
    {
        RunOnStaThread(() =>
        {
            var log = new List<string>();
            var tcs = new TaskCompletionSource<bool>();
            var dispatcher = Dispatcher.CurrentDispatcher;
            var frame = new DispatcherFrame();
            Task? awaiterTask = null;

            async Task AwaiterBody()
            {
                log.Add("A:before-await");
                await tcs.Task;
                log.Add("A:continuation");
                frame.Continue = false;
            }

            // Op A queued first, op B second — same priority => FIFO, so A's await is pending
            // before B runs and completes the task.
            dispatcher.BeginInvoke(DispatcherPriority.Normal, (Action)(() => { awaiterTask = AwaiterBody(); }));
            dispatcher.BeginInvoke(DispatcherPriority.Normal, (Action)(() =>
            {
                log.Add("B:before-set");
                tcs.SetResult(true);
                log.Add("B:after-set");
            }));

            // Backstop: force the pump to exit even if a regression fails to complete the frame,
            // so the test asserts rather than hangs.
            var backstop = new DispatcherTimer(
                TimeSpan.FromSeconds(15), DispatcherPriority.Normal,
                (_, _) => frame.Continue = false, dispatcher);
            backstop.Start();

            try
            {
                Dispatcher.PushFrame(frame);
            }
            finally
            {
                backstop.Stop();
            }

            Assert.That(awaiterTask, Is.Not.Null, "Op A never ran — the queue was not pumped.");
            Assert.That(awaiterTask!.IsCompletedSuccessfully, Is.True,
                "The awaiter task did not complete — its continuation never ran.");
            Assert.That(log, Is.EqualTo(new[]
                {
                    "A:before-await", "B:before-set", "B:after-set", "A:continuation",
                }),
                "An await continuation completed from a sibling dispatcher operation must be " +
                "POSTED (run after the completer returns), not inlined into the completer. " +
                "Inlined order [A:before-await, B:before-set, A:continuation, B:after-set] means " +
                "the two operations shared one SynchronizationContext instance.");
        });
    }

    /// <summary>
    /// Two Normal-priority queued operations must observe distinct SynchronizationContext
    /// instances (fresh DispatcherSynchronizationContext per DispatcherOperation.InvokeImpl).
    /// </summary>
    [Test]
    [CancelAfter(30_000)]
    public void QueuedNormalOperations_SeeDistinctSynchronizationContexts()
    {
        RunOnStaThread(() =>
        {
            var dispatcher = Dispatcher.CurrentDispatcher;
            var frame = new DispatcherFrame();
            SynchronizationContext? sc1 = null;
            SynchronizationContext? sc2 = null;

            dispatcher.BeginInvoke(DispatcherPriority.Normal,
                (Action)(() => sc1 = SynchronizationContext.Current));
            dispatcher.BeginInvoke(DispatcherPriority.Normal,
                (Action)(() => { sc2 = SynchronizationContext.Current; frame.Continue = false; }));

            var backstop = new DispatcherTimer(
                TimeSpan.FromSeconds(15), DispatcherPriority.Normal,
                (_, _) => frame.Continue = false, dispatcher);
            backstop.Start();

            try
            {
                Dispatcher.PushFrame(frame);
            }
            finally
            {
                backstop.Stop();
            }

            Assert.That(sc1, Is.Not.Null.And.InstanceOf<DispatcherSynchronizationContext>());
            Assert.That(sc2, Is.Not.Null.And.InstanceOf<DispatcherSynchronizationContext>());
            Assert.That(sc2, Is.Not.SameAs(sc1),
                "Two sibling Normal-priority queued operations shared one " +
                "DispatcherSynchronizationContext instance.");
        });
    }

    /// <summary>
    /// Two same-thread Send-priority Invokes via the Action overload
    /// (<c>Invoke(Action, DispatcherPriority)</c>) must observe distinct SynchronizationContext
    /// instances. Covers the Send fast path in <c>Dispatcher.Invoke(Action, ...)</c>.
    /// </summary>
    [Test]
    [CancelAfter(30_000)]
    public void SendInvoke_ActionOverload_SeesDistinctSynchronizationContexts()
    {
        RunOnStaThread(() =>
        {
            var dispatcher = Dispatcher.CurrentDispatcher;
            SynchronizationContext? sc1 = null;
            SynchronizationContext? sc2 = null;

            dispatcher.Invoke((Action)(() => sc1 = SynchronizationContext.Current), DispatcherPriority.Send);
            dispatcher.Invoke((Action)(() => sc2 = SynchronizationContext.Current), DispatcherPriority.Send);

            Assert.That(sc1, Is.Not.Null.And.InstanceOf<DispatcherSynchronizationContext>());
            Assert.That(sc2, Is.Not.Null.And.InstanceOf<DispatcherSynchronizationContext>());
            Assert.That(sc2, Is.Not.SameAs(sc1),
                "Two same-thread Send Invokes (Action overload) shared one " +
                "DispatcherSynchronizationContext instance.");
        });
    }

    /// <summary>
    /// Two same-thread Send-priority Invokes via the Func overload
    /// (<c>Invoke&lt;TResult&gt;(Func&lt;TResult&gt;, DispatcherPriority)</c>) must observe distinct
    /// SynchronizationContext instances. Covers the Send fast path in
    /// <c>Dispatcher.Invoke&lt;TResult&gt;(Func&lt;TResult&gt;, ...)</c>.
    /// </summary>
    [Test]
    [CancelAfter(30_000)]
    public void SendInvoke_FuncOverload_SeesDistinctSynchronizationContexts()
    {
        RunOnStaThread(() =>
        {
            var dispatcher = Dispatcher.CurrentDispatcher;

            SynchronizationContext? sc1 = dispatcher.Invoke(
                () => SynchronizationContext.Current, DispatcherPriority.Send);
            SynchronizationContext? sc2 = dispatcher.Invoke(
                () => SynchronizationContext.Current, DispatcherPriority.Send);

            Assert.That(sc1, Is.Not.Null.And.InstanceOf<DispatcherSynchronizationContext>());
            Assert.That(sc2, Is.Not.Null.And.InstanceOf<DispatcherSynchronizationContext>());
            Assert.That(sc2, Is.Not.SameAs(sc1),
                "Two same-thread Send Invokes (Func overload) shared one " +
                "DispatcherSynchronizationContext instance.");
        });
    }

    /// <summary>
    /// Two same-thread Send-priority Invokes via the legacy overload
    /// (<c>Invoke(DispatcherPriority, Delegate)</c>) must observe distinct SynchronizationContext
    /// instances. Covers the Send fast path in <c>Dispatcher.LegacyInvokeImpl</c>, which
    /// <c>HwndSubclass.SubclassWndProc</c> routes every Win32 message through.
    /// </summary>
    [Test]
    [CancelAfter(30_000)]
    public void SendInvoke_LegacyOverload_SeesDistinctSynchronizationContexts()
    {
        RunOnStaThread(() =>
        {
            var dispatcher = Dispatcher.CurrentDispatcher;
            SynchronizationContext? sc1 = null;
            SynchronizationContext? sc2 = null;

            dispatcher.Invoke(DispatcherPriority.Send,
                (Action)(() => sc1 = SynchronizationContext.Current));
            dispatcher.Invoke(DispatcherPriority.Send,
                (Action)(() => sc2 = SynchronizationContext.Current));

            Assert.That(sc1, Is.Not.Null.And.InstanceOf<DispatcherSynchronizationContext>());
            Assert.That(sc2, Is.Not.Null.And.InstanceOf<DispatcherSynchronizationContext>());
            Assert.That(sc2, Is.Not.SameAs(sc1),
                "Two same-thread Send Invokes (legacy overload) shared one " +
                "DispatcherSynchronizationContext instance.");
        });
    }
}
