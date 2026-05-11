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
        // Variant used by the synchronous Dispatcher.Invoke(Action,...) slow path,
        // which never exposes the DispatcherOperation (or its Task) to user code —
        // see DispatcherOperation's internal-sync ctor for the safety argument.
        // Skips the per-op `new DispatcherOperationTaskMapping(operation)` heap
        // allocation that Initialize would otherwise attach as the Task's
        // AsyncState, saving ~24 B/op on every cross-thread or non-Send-priority
        // synchronous Dispatcher.Invoke(Action,...) call.
        public abstract void InitializeWithoutMapping(DispatcherOperation operation);
        public abstract Task GetTask();
        public abstract void SetCanceled();
        public abstract void SetResult(object result);
        public abstract void SetException(Exception exception);
    }

    internal class DispatcherOperationTaskSource<TResult> : DispatcherOperationTaskSource
    {
        // Create the underlying TaskCompletionSource and set the
        // DispatcherOperation as the Task's AsyncState.
        public override void Initialize(DispatcherOperation operation)
        {
            if(_taskCompletionSource != null)
            {
                throw new InvalidOperationException();
            }

            _taskCompletionSource = new TaskCompletionSource<TResult>(new DispatcherOperationTaskMapping(operation));
        }

        // Internal-sync variant — no AsyncState. The default TaskCompletionSource<TResult>()
        // ctor leaves Task.AsyncState=null. Internal Wait / InvokeCompletions / SetResult
        // / SetException / SetCanceled don't read AsyncState; the public TaskExtensions
        // discriminator (`IsDispatcherOperationTask`) returns false on this Task, which
        // is harmless because the op is never exposed to user code on this path.
        public override void InitializeWithoutMapping(DispatcherOperation operation)
        {
            if(_taskCompletionSource != null)
            {
                throw new InvalidOperationException();
            }

            _taskCompletionSource = new TaskCompletionSource<TResult>();
        }

        public override Task GetTask()
        {
            if(_taskCompletionSource == null)
            {
                throw new InvalidOperationException();
            }

            return _taskCompletionSource.Task;
        }
        
        public override void SetCanceled()
        {
            if(_taskCompletionSource == null)
            {
                throw new InvalidOperationException();
            }

            _taskCompletionSource.SetCanceled();
        }
        
        public override void SetResult(object result)
        {
            if(_taskCompletionSource == null)
            {
                throw new InvalidOperationException();
            }

            _taskCompletionSource.SetResult((TResult)result);
        }
        
        public override void SetException(Exception exception)
        {
            if(_taskCompletionSource == null)
            {
                throw new InvalidOperationException();
            }

            _taskCompletionSource.SetException(exception);
        }

        private TaskCompletionSource<TResult> _taskCompletionSource;
    }
}
