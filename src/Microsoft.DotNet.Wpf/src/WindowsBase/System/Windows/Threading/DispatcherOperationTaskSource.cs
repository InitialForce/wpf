// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

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
    }

    internal class DispatcherOperationTaskSource<TResult> : DispatcherOperationTaskSource
    {
        // Lazy-init of the TaskCompletionSource. Most BeginInvoke callers
        // fire-and-forget — they never read .Task / .Result / `await op`, so
        // allocating the TaskCompletionSource (which itself allocates a Task<TResult>
        // and holds a DispatcherOperationTaskMapping) per DispatcherOperation ctor
        // is wasted work on the dominant path. Initialize() now just records the
        // owning operation, and the TCS is constructed on first GetTask access.
        // Completion (SetCanceled / SetResult / SetException) is staged into local
        // fields when TCS doesn't exist yet, then replayed onto the TCS the first
        // time Task is accessed.
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

            if (_taskCompletionSource == null)
            {
                _taskCompletionSource = new TaskCompletionSource<TResult>(new DispatcherOperationTaskMapping(_operation));

                if (_hasPendingCompletion)
                {
                    if (_pendingCanceled)
                    {
                        _taskCompletionSource.SetCanceled();
                    }
                    else if (_pendingException != null)
                    {
                        _taskCompletionSource.SetException(_pendingException);
                        _pendingException = null;
                    }
                    else
                    {
                        _taskCompletionSource.SetResult((TResult)_pendingResult);
                        _pendingResult = null;
                    }
                    _hasPendingCompletion = false;
                }
            }

            return _taskCompletionSource.Task;
        }

        public override void SetCanceled()
        {
            if (_operation == null)
            {
                throw new InvalidOperationException();
            }

            if (_taskCompletionSource != null)
            {
                _taskCompletionSource.SetCanceled();
            }
            else
            {
                _hasPendingCompletion = true;
                _pendingCanceled = true;
            }
        }

        public override void SetResult(object result)
        {
            if (_operation == null)
            {
                throw new InvalidOperationException();
            }

            if (_taskCompletionSource != null)
            {
                _taskCompletionSource.SetResult((TResult)result);
            }
            else
            {
                _hasPendingCompletion = true;
                _pendingResult = result;
            }
        }

        public override void SetException(Exception exception)
        {
            if (_operation == null)
            {
                throw new InvalidOperationException();
            }

            if (_taskCompletionSource != null)
            {
                _taskCompletionSource.SetException(exception);
            }
            else
            {
                _hasPendingCompletion = true;
                _pendingException = exception;
            }
        }

        private TaskCompletionSource<TResult> _taskCompletionSource;
        private DispatcherOperation _operation;

        // Staged completion state — populated by SetCanceled / SetResult / SetException
        // when GetTask has not yet been called. Replayed onto the TCS the first time
        // GetTask runs so awaiters see the same final state they would have on the
        // eager path. Only one of (_pendingCanceled, _pendingException, _pendingResult)
        // is meaningful at a time; _hasPendingCompletion gates them all.
        private bool _hasPendingCompletion;
        private bool _pendingCanceled;
        private Exception _pendingException;
        private object _pendingResult;
    }
}
