using NUnit.Framework;
using System;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;

namespace InitialForce.WpfSmoke;

/// <summary>
/// Regression guard for the DispatcherOperation Task.AsyncState mapping contract.
///
/// A queued DispatcherOperation exposes its inner <see cref="Task"/> to any
/// consumer that attaches a <see cref="DispatcherHooks"/> handler. The public
/// <see cref="TaskExtensions"/> discriminator API keys off that Task's
/// AsyncState: <c>IsDispatcherOperationTask()</c> checks for a
/// <c>DispatcherOperationTaskMapping</c>, and <c>DispatcherOperationWait()</c>
/// throws <see cref="NotSupportedException"/> when the mapping is absent.
///
/// Because a hook can attach at any point in the operation's queued lifetime
/// (OperationPosted fires at enqueue, OperationStarted/Completed at dequeue),
/// no construction-time predicate can decide the operation is unobservable and
/// drop the mapping. The mapping must be present on every queued operation.
/// </summary>
[TestFixture]
[Apartment(ApartmentState.STA)]
public class DispatcherTaskMappingTests : SmokeBase
{
    /// <summary>
    /// Drives the synchronous <c>Dispatcher.Invoke(Action, priority)</c> slow
    /// path on a single thread. Background priority (not Send) forces the
    /// operation through the queue, which builds a DispatcherOperation, raises
    /// OperationPosted, and pumps a nested frame to completion. A hook captures
    /// the operation's Task; the Task must carry the DispatcherOperationTaskMapping
    /// so the public TaskExtensions contract holds:
    ///   * IsDispatcherOperationTask() == true
    ///   * DispatcherOperationWait() returns Completed and does NOT throw
    ///     NotSupportedException.
    /// Deterministic: the callback runs synchronously inside Invoke via the
    /// pumped frame, so no polling or sleeps are required.
    /// </summary>
    [Test]
    public void QueuedInvokeTaskCarriesDispatcherOperationMapping()
    {
        RunOnStaThread(() =>
        {
            var dispatcher = Dispatcher.CurrentDispatcher;

            Task? postedTask = null;
            DispatcherHookEventHandler onPosted = (_, e) => postedTask ??= e.Operation.Task;
            dispatcher.Hooks.OperationPosted += onPosted;

            bool ran = false;
            try
            {
                // Background != Send on the calling thread -> queued slow path.
                // Statement body forces binding to Invoke(Action, DispatcherPriority);
                // an expression lambda would bind Invoke<TResult>(Func<TResult>, ...),
                // whose path always kept the mapping and never exercised the bug.
                dispatcher.Invoke(() => { ran = true; }, DispatcherPriority.Background);
            }
            finally
            {
                dispatcher.Hooks.OperationPosted -= onPosted;
            }

            Assert.That(ran, Is.True, "The queued callback did not execute.");
            Assert.That(postedTask, Is.Not.Null,
                "OperationPosted did not fire — the Invoke did not take the queued slow path.");

            Assert.That(postedTask!.IsDispatcherOperationTask(), Is.True,
                "Task.AsyncState does not carry the DispatcherOperationTaskMapping — " +
                "the public discriminator cannot recognise a queued DispatcherOperation's Task.");

            DispatcherOperationStatus status = default;
            Assert.That(() => status = postedTask!.DispatcherOperationWait(),
                Throws.Nothing,
                "DispatcherOperationWait threw — the Task lost its DispatcherOperationTaskMapping AsyncState.");
            Assert.That(status, Is.EqualTo(DispatcherOperationStatus.Completed),
                "DispatcherOperationWait did not report the operation as Completed.");
        });
    }
}
