using NUnit.Framework;
using System;
using System.Diagnostics;
using System.Reflection;
using System.Threading;
using System.Windows.Threading;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for the pooled cross-thread <c>DispatcherOperationEvent</c>.
///
/// An external thread that blocks on <see cref="DispatcherOperation.Wait(TimeSpan)"/>
/// acquires a thread-static pooled wrapper that subscribes an
/// <c>OnCompletedOrAborted</c> handler to the operation's Aborted/Completed events,
/// blocks on a kernel event, and on wake unsubscribes and returns the wrapper to the
/// pool. Both raise sites capture the handler invocation list and then invoke it
/// outside the wrapper's control (Completed is raised outside the dispatcher lock,
/// Abort captures the list lock-free), so a captured handler can fire AFTER the waiter
/// has already woken and parked or recycled the wrapper.
///
/// Two failure modes this exercises:
///  * Parked wrapper — the stale callback must not dereference the now-null operation
///    (that NREs on the raising thread).
///  * Recycled wrapper — the stale callback (whose sender is the old operation) must
///    not signal the event now owned by a different operation (that spuriously wakes
///    the new wait, which then reports a premature non-terminal status).
///
/// The blocking first-position Aborted subscriber freezes the captured snapshot after
/// the raise site captured it and before the waiter's handler (second in list) runs,
/// giving a deterministic window. Registration is observed by reflecting the
/// operation's <c>_aborted</c> invocation-list length — a bounded spin on observable
/// state, not a sleep.
/// </summary>
[TestFixture]
public class DispatcherOperationEventPoolTests : SmokeBase
{
    private static readonly FieldInfo AbortedField =
        typeof(DispatcherOperation).GetField("_aborted", BindingFlags.NonPublic | BindingFlags.Instance)
        ?? throw new InvalidOperationException("DispatcherOperation._aborted field not found.");

    private static int AbortedSubscriberCount(DispatcherOperation op)
    {
        var handler = (Delegate?)AbortedField.GetValue(op);
        return handler?.GetInvocationList().Length ?? 0;
    }

    private static Dispatcher StartDispatcherThread(out Thread thread)
    {
        Dispatcher? dispatcher = null;
        using var ready = new ManualResetEventSlim(false);
        var t = new Thread(() =>
        {
            dispatcher = Dispatcher.CurrentDispatcher;
            ready.Set();
            Dispatcher.Run();
        })
        { IsBackground = true };
        t.SetApartmentState(ApartmentState.STA);
        t.Start();
        ready.Wait();
        thread = t;
        return dispatcher!;
    }

    // An Inactive operation is enqueued but never dispatched, so its status stays
    // Pending under test control and Abort can always remove it from the queue.
    private static DispatcherOperation QueueInactive(Dispatcher dispatcher) =>
        dispatcher.BeginInvoke(DispatcherPriority.Inactive, new Action(() => { }));

    /// <summary>
    /// Variant 1 — a stale completion raised against a PARKED wrapper must not throw.
    /// On the unfixed pool the callback dereferences the parked wrapper's null
    /// operation via the DispatcherLock getter and NREs on the raising thread.
    /// </summary>
    [Test]
    public void StaleCompletionAgainstParkedWrapperDoesNotThrow()
    {
        Dispatcher dispatcher = StartDispatcherThread(out Thread dispatcherThread);
        var uEntered = new ManualResetEventSlim(false);
        var gate = new ManualResetEventSlim(false);
        Exception? waitException = null;
        Exception? abortException = null;

        try
        {
            DispatcherOperation op = QueueInactive(dispatcher);

            // First-position blocking Aborted subscriber: after Abort captures the
            // snapshot [U, waiterHandler], U runs first and freezes the invocation
            // list here until the gate is released.
            op.Aborted += (sender, e) => { uEntered.Set(); gate.Wait(); };

            var w = new Thread(() =>
            {
                try { op.Wait(TimeSpan.FromSeconds(1)); }
                catch (Exception ex) { waitException = ex; }
            })
            { IsBackground = true };

            var sw = Stopwatch.StartNew();
            w.Start();

            if (!SpinWait.SpinUntil(() => AbortedSubscriberCount(op) >= 2, 2000))
            {
                gate.Set();
                Assert.Inconclusive("Waiter never registered its DispatcherOperationEvent handler.");
            }

            var a = new Thread(() =>
            {
                try { op.Abort(); }
                catch (Exception ex) { abortException = ex; }
            })
            { IsBackground = true };
            a.Start();

            if (!uEntered.Wait(2000))
            {
                gate.Set();
                a.Join();
                w.Join();
                Assert.Inconclusive("Abort did not enter the blocking handler; snapshot not captured.");
            }

            // The snapshot was captured before U ran (uEntered). If that happened too
            // close to the waiter's 1 s timeout we cannot be sure the waiter's handler
            // was still subscribed at capture time — refuse to false-pass.
            if (sw.ElapsedMilliseconds >= 900)
            {
                gate.Set();
                a.Join();
                w.Join();
                Assert.Inconclusive($"Setup overran the 1 s wait bound ({sw.ElapsedMilliseconds} ms).");
            }

            // Let the waiter time out and park its wrapper (operation -> null).
            w.Join();

            // Release the frozen snapshot; the stale callback now runs against the
            // parked wrapper on thread A.
            gate.Set();
            a.Join();

            Assert.That(waitException, Is.Null, "op.Wait threw unexpectedly.");
            Assert.That(abortException, Is.Null,
                "op.Abort threw — a stale completion dereferenced a parked pooled wrapper.");
        }
        finally
        {
            gate.Set();
            dispatcher.InvokeShutdown();
            dispatcherThread.Join(2000);
        }
    }

    /// <summary>
    /// Variant 2 — a stale completion raised against a RECYCLED wrapper must not
    /// signal the event now owned by a different operation. On the unfixed pool the
    /// stale Set wakes the second wait prematurely, which then reports a non-terminal
    /// Pending status instead of the real terminal status.
    /// </summary>
    [Test]
    public void StaleCompletionAgainstRecycledWrapperDoesNotSpuriouslyWake()
    {
        Dispatcher dispatcher = StartDispatcherThread(out Thread dispatcherThread);
        var uEntered = new ManualResetEventSlim(false);
        var gate = new ManualResetEventSlim(false);
        var phase1Done = new ManualResetEventSlim(false);
        Exception? waitException = null;
        Exception? abortException = null;
        DispatcherOperationStatus wait2Result = DispatcherOperationStatus.Pending;

        try
        {
            DispatcherOperation op1 = QueueInactive(dispatcher);
            DispatcherOperation op2 = QueueInactive(dispatcher);

            op1.Aborted += (sender, e) => { uEntered.Set(); gate.Wait(); };

            // The pool is thread-static, so both phases run on ONE thread: phase 1
            // parks the poisoned wrapper, phase 2 recycles the same wrapper onto op2.
            var w = new Thread(() =>
            {
                try
                {
                    op1.Wait(TimeSpan.FromSeconds(1));
                    phase1Done.Set();
                    wait2Result = op2.Wait(TimeSpan.FromSeconds(10));
                }
                catch (Exception ex) { waitException = ex; }
            })
            { IsBackground = true };

            var sw = Stopwatch.StartNew();
            w.Start();

            if (!SpinWait.SpinUntil(() => AbortedSubscriberCount(op1) >= 2, 2000))
            {
                gate.Set();
                Assert.Inconclusive("Phase-1 waiter never registered on op1.");
            }

            var a = new Thread(() =>
            {
                try { op1.Abort(); }
                catch (Exception ex) { abortException = ex; }
            })
            { IsBackground = true };
            a.Start();

            if (!uEntered.Wait(2000))
            {
                gate.Set();
                a.Join();
                w.Join();
                Assert.Inconclusive("Abort did not enter the blocking handler; op1 snapshot not captured.");
            }

            if (sw.ElapsedMilliseconds >= 900)
            {
                gate.Set();
                a.Join();
                w.Join();
                Assert.Inconclusive($"Setup overran the 1 s phase-1 bound ({sw.ElapsedMilliseconds} ms).");
            }

            // Phase 1 times out and parks; phase 2 recycles the wrapper onto op2 and
            // re-subscribes. Wait for op2 to show the waiter's handler.
            if (!phase1Done.Wait(3000) ||
                !SpinWait.SpinUntil(() => AbortedSubscriberCount(op2) >= 1, 3000))
            {
                gate.Set();
                a.Join();
                w.Join();
                Assert.Inconclusive("Wrapper did not recycle onto op2 in time.");
            }

            // Release the frozen op1 snapshot; the stale callback fires with sender
            // == op1 against the wrapper now registered to op2.
            gate.Set();
            a.Join();

            Assert.That(abortException, Is.Null, "op1.Abort threw unexpectedly.");

            // The stale completion must be ignored, so the phase-2 wait stays blocked.
            Assert.That(w.Join(500), Is.False,
                "The waiter woke prematurely — a stale completion signalled a recycled wrapper.");

            // A genuine op2 completion must still wake the wait and report the real
            // terminal status.
            op2.Abort();
            Assert.That(w.Join(TimeSpan.FromSeconds(5)), Is.True,
                "The waiter did not wake on op2.Abort.");
            Assert.That(waitException, Is.Null, "The phase-2 wait threw unexpectedly.");
            Assert.That(wait2Result, Is.EqualTo(DispatcherOperationStatus.Aborted),
                "The phase-2 wait did not observe op2's terminal Aborted status.");
        }
        finally
        {
            gate.Set();
            dispatcher.InvokeShutdown();
            dispatcherThread.Join(2000);
        }
    }
}
