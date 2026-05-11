// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Threading;
using System.Threading.Tasks;

namespace System.Windows.Threading
{
    // DispatcherOperation uses this class to access a TaskCompletionSource<T>
    // without being a generic iteself.
    internal abstract class DispatcherOperationTaskSource
    {
        public abstract void Initialize(DispatcherOperation operation);
        public abstract Task GetTask();
        public abstract void SetCanceled();
        public abstract void SetResult(object result);
        public abstract void SetException(Exception exception);

        // True iff the underlying TaskCompletionSource has been materialized
        // (i.e. someone called GetTask). DispatcherOperation.Wait / Result use
        // this to avoid forcing the TCS+Task+Mapping triplet into existence
        // on the fire-and-forget path (the dominant cross-thread Invoke(Action,...)
        // scenario, where neither op.Task nor op.Result is ever observed).
        public abstract bool HasTask { get; }
    }

    // Lazy-init of the TaskCompletionSource. Most BeginInvoke and synchronous
    // cross-thread Invoke callers never read .Task / .Result / `await op`, so
    // allocating the TaskCompletionSource (which itself allocates a Task<TResult>)
    // and the DispatcherOperationTaskMapping wrapper per DispatcherOperation
    // ctor is wasted work on the dominant path. Initialize() now just records
    // the owning operation, and the TCS is constructed on first GetTask access.
    // Completion (SetCanceled / SetResult / SetException) is staged into local
    // fields when TCS does not yet exist, then replayed onto the TCS the first
    // time Task is accessed.
    //
    // Synchronous Invoke(Action,...) / Invoke<TResult>(Func<TResult>,...) paths
    // do not access op.Task themselves any more — DispatcherOperation.Wait and
    // DispatcherOperation.Result read _exception / _result / _status directly
    // (gated on HasTask above) to mirror Task.GetAwaiter().GetResult()'s
    // throw-or-return semantics without forcing TCS creation. That is the
    // primary alloc-axis savings: the cross-thread Invoke wait path no longer
    // allocates TaskCompletionSource<object> + Task<object> +
    // DispatcherOperationTaskMapping triplet per call.
    //
    // Thread safety: SetX is called from the dispatcher thread (Abort +
    // InvokeCompletions); Initialize is called from the construction thread
    // (which is also the queueing thread for the op); GetTask may be called
    // from any thread via the public op.Task property. We use a per-instance
    // lock (lock(this)) to linearize the (stage <-> allocate-and-replay)
    // critical section, so GetTask racing concurrently with SetX cannot leave
    // the TCS empty when a terminal state was staged. The TCS itself is
    // thread-safe internally for transitions once it has been allocated, so
    // the fast paths (TCS already created on both sides) require no
    // additional synchronization.
    internal class DispatcherOperationTaskSource<TResult> : DispatcherOperationTaskSource
    {
        public override void Initialize(DispatcherOperation operation)
        {
            if (_operation != null)
            {
                throw new InvalidOperationException();
            }

            _operation = operation;
        }

        public override Task GetTask()
        {
            if (_operation == null)
            {
                throw new InvalidOperationException();
            }

            TaskCompletionSource<TResult> tcs = Volatile.Read(ref _taskCompletionSource);
            if (tcs == null)
            {
                lock (this)
                {
                    tcs = _taskCompletionSource;
                    if (tcs == null)
                    {
                        tcs = new TaskCompletionSource<TResult>(new DispatcherOperationTaskMapping(_operation));

                        // Replay any terminal state staged by SetCanceled / SetResult /
                        // SetException calls that ran while _taskCompletionSource was null.
                        if (_hasPendingCompletion)
                        {
                            if (_pendingCanceled)
                            {
                                tcs.SetCanceled();
                            }
                            else if (_pendingException != null)
                            {
                                tcs.SetException(_pendingException);
                                _pendingException = null;
                            }
                            else
                            {
                                tcs.SetResult((TResult)_pendingResult);
                                _pendingResult = null;
                            }
                            _hasPendingCompletion = false;
                        }

                        // Publish AFTER replay so any concurrent SetX seeing the
                        // non-null _taskCompletionSource (via the fast path) acts on
                        // the fully-replayed TCS, not a half-initialized one.
                        Volatile.Write(ref _taskCompletionSource, tcs);
                    }
                }
            }
            return tcs.Task;
        }

        public override void SetCanceled()
        {
            if (_operation == null)
            {
                throw new InvalidOperationException();
            }

            TaskCompletionSource<TResult> tcs = Volatile.Read(ref _taskCompletionSource);
            if (tcs != null)
            {
                tcs.SetCanceled();
                return;
            }

            lock (this)
            {
                tcs = _taskCompletionSource;
                if (tcs != null)
                {
                    tcs.SetCanceled();
                }
                else
                {
                    _hasPendingCompletion = true;
                    _pendingCanceled = true;
                }
            }
        }

        public override void SetResult(object result)
        {
            if (_operation == null)
            {
                throw new InvalidOperationException();
            }

            TaskCompletionSource<TResult> tcs = Volatile.Read(ref _taskCompletionSource);
            if (tcs != null)
            {
                tcs.SetResult((TResult)result);
                return;
            }

            lock (this)
            {
                tcs = _taskCompletionSource;
                if (tcs != null)
                {
                    tcs.SetResult((TResult)result);
                }
                else
                {
                    _hasPendingCompletion = true;
                    _pendingResult = result;
                }
            }
        }

        public override void SetException(Exception exception)
        {
            if (_operation == null)
            {
                throw new InvalidOperationException();
            }

            TaskCompletionSource<TResult> tcs = Volatile.Read(ref _taskCompletionSource);
            if (tcs != null)
            {
                tcs.SetException(exception);
                return;
            }

            lock (this)
            {
                tcs = _taskCompletionSource;
                if (tcs != null)
                {
                    tcs.SetException(exception);
                }
                else
                {
                    _hasPendingCompletion = true;
                    _pendingException = exception;
                }
            }
        }

        public override bool HasTask
        {
            // Volatile.Read pairs with the Volatile.Write in GetTask's publish step,
            // so a non-null observation here implies the TCS has been fully replayed.
            get { return Volatile.Read(ref _taskCompletionSource) != null; }
        }

        private TaskCompletionSource<TResult> _taskCompletionSource;
        private DispatcherOperation _operation;

        // Staged completion state — populated by SetCanceled / SetResult / SetException
        // when GetTask has not yet been called. Replayed onto the TCS the first time
        // GetTask runs so awaiters see the same final state they would have on the
        // eager path. Guarded by lock(this).
        private bool _hasPendingCompletion;
        private bool _pendingCanceled;
        private Exception _pendingException;
        private object _pendingResult;
    }
}
