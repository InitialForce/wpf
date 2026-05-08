// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

//
//
//
//  Description: Wrapper for System.Threading.ExecutionContext that allows
//               custom management of information relevant to a logical thread
//               of execution.
//
//               Starting .NET 4.6, ExecutionContext tracks
//               Thread.CurrentCulture and Thread.CurrentUICulture,
//               which would be restored to their respective previous values
//               after a call to ExecutionContext.Run.
//               This behavior is undesirable within the Dispatcher - various dispatcher
//               operations can run user code that can in turn set Thread.CurrentCulture or
//               Thread.CurrentUICulture, and we do not want those values to be overwritten
//               with their respective previous values.
//
//               This wrapper forwards all calls to ExecutionContext, and manages the
//               values of Thread.CurrentCulture and Thread.CurrentUICulture carefully
//               during Run and Dispose.


using System.Globalization;
using System.Threading;

namespace MS.Internal
{
    /// <summary>
    /// An encapsulation of ExecutionContext that preserves thread culture infos
    /// during DispatcherOperations
    /// </summary>
    /// <remarks>
    ///     On applications targeting 4.6 and later, the flow of execution durign a DispatcherOperation
    ///     would go like this:
    ///
    ///         DispatcherOperation ctor
    ///             EC.Capture                  // EC saves culture info $1
    ///        (other code runs)                // Modifies culture info to $2
    ///         DispatcherOperation is scheduled
    ///             EC.Run(callback)            // callback will run under $1 (not $2)
    ///                 callback()              // callback modifies culture info to $3
    ///             EC.Run terminates           // EC reverts culture info to $1 (we lose $3)
    ///
    ///     With the use of CulturePreservingExecutionContext, the flow is modified as follows:
    ///
    ///         DispatcherOperation ctor
    ///             CPEC.Capture                // EC saves culture info $1
    ///         (other code runs)               // Modifies culture info to $2
    ///         DispatcherOperation is scheduled
    ///             CPEC.Run(callback)          // CPEC saves culture info $2 directly into
    ///                                         // its own _culture / _uICulture fields
    ///                 Calls EC.Run(CallbackWrapper, executionContext)
    ///                     CallbackWrapper()   // EC will run this under $1
    ///                         CallbackWrapper will restore culture info $2
    ///                         callback()      // callback is run under $2, it modifies culture info to $3
    ///                         CallbackWrapper saves $3 into the CPEC fields
    ///                 EC.Run terminates       // EC reverts culture info to $1
    ///             CPEC.Run restores $3 from its own fields
    ///         DispatcherOperation completes - current culture info is set to $3
    ///
    ///     This flow is similar to the default behavior on .NET 4.5.2 and earlier.
    /// </remarks>
    internal class CulturePreservingExecutionContext: IDisposable
    {
        #region ExecutionContext Forwarders

        /// <summary>
        ///     Captures the execution context from the current thread.
        /// </summary>
        /// <returns>
        ///     An <see cref="CulturePreservingExecutionContext"/> object representing
        ///     the <see cref="ExecutionContext"/> for the current thread.
        /// </returns>
        /// <remarks>
        ///     If ExecutionContext.SuppressFlow had been previously called,
        ///     then this method would return null;
        /// </remarks>
        public static CulturePreservingExecutionContext Capture()
        {
            // ExecutionContext.Capture() already returns null when flow is suppressed
            // (its implementation reads the current EC and returns null if either
            // the EC ref is null or its _isFlowSuppressed is true), so the explicit
            // ExecutionContext.IsFlowSuppressed() precheck does the same TLS read
            // twice. Drop it.
            var ec = ExecutionContext.Capture();
            if (ec == null)
            {
                return null;
            }

            // Reuse a thread-local pooled instance when available. The pool is
            // refilled by Run()'s finally block, so the dominant Capture-Run-
            // Capture-Run pattern on the dispatcher thread (and the bench) hits
            // the pool on every cycle after warm-up, killing the per-Run heap
            // allocation. Cross-thread Capture (producer thread) -> Run
            // (dispatcher thread) misses the pool harmlessly because the
            // pool is [ThreadStatic].
            var pooled = s_pooled;
            if (pooled != null)
            {
                s_pooled = null;
                pooled._context = ec;
                pooled._disposed = false;
                return pooled;
            }

            return new CulturePreservingExecutionContext(ec);
        }

        /// <summary>
        /// Runs a method in a specified execution context on the current thread by
        /// delegating the call to <see cref="CallbackWrapper(object)"/>, which will save
        /// relevant CultureInfo values before returning.
        /// </summary>
        /// <param name="executionContext">
        ///     The <see cref="ExecutionContext"/> to set, represeted by
        ///     the <see cref="CulturePreservingExecutionContext"/> instance.
        /// </param>
        /// <param name="callback">
        ///     A <see cref="ContextCallback"/> delegate that represents the
        ///     method to be run in the provided execution context.
        /// </param>
        /// <param name="state">
        ///     The object to pass to the callback method.
        /// </param>
        /// <remarks>
        /// BaseAppContextSwitches.DoNotUseCulturePreservingDispatcherOperations indicates whether
        /// CulturePreservingExecutionContext should do extra work to preserve culture infos, or not.
        ///
        /// Generally set to true when target framework version is less than or equals 4.5.2, and false
        /// on 4.6 and above.
        ///
        /// On 4.5.2 and earlier frameworks, ExecutionContext does not include culture infos
        /// in its state, nor does it restore them after ExecutionContext.Run. Thus WPF
        /// does not have to do extra work to propagate culture infos modified within a
        /// call to ExecutionContext.Run (typically, this happens within a DispatcherOperation). In this
        /// case, we can simply defer all the work to ExecutionContext.Run directly.
        ///
        /// On 4.6 and above, the design is to do some extra work to preserve culture infos.
        ///
        /// This switch can be overridden by the application by calling
        /// AppContext.SetSwitch("Switch.MS.Internal.DoNotUseCulturePreservingDispatcherOperations", true|false)
        /// or by setting the switch in app.config in the runtime section like this:
        /// <code
        ///   <runtime>
        ///     <AppContextSwitchOverrides value="Switch.MS.Internal.DoNotUseCulturePreservingDispatcherOperations=true|false"/>
        ///   </runtime>
        /// />
        /// </remarks>
        public static void Run(CulturePreservingExecutionContext executionContext, ContextCallback callback, object state)
        {
            // Both production callers (DispatcherOperation.Invoke and
            // Dispatcher.ShutdownImpl) explicitly null-check executionContext and
            // pass non-null ContextCallback delegates created in their ctors —
            // the redundant ArgumentNullException + `if(callback == null) return`
            // guards are dead branches on the hot path. Drop them; a faulty caller
            // gets the same NRE one line later.

            // Compat switch is set, defer directly to EC.Run
            if (BaseAppContextSwitches.DoNotUseCulturePreservingDispatcherOperations)
            {
                ExecutionContext.Run(executionContext._context, callback, state);
                ReturnToPool(executionContext);
                return;
            }

            // Stash the user callback + state on the CPEC itself and snapshot the
            // current culture infos. CallbackWrapper will restore them just before
            // invoking the user callback. (Single-Run-per-CPEC lifecycle assumed.)
            //
            // Reuse a single Thread.CurrentThread local for the snapshot read and
            // the finally restore — the JIT does not CSE the [Intrinsic] TLS access
            // across method boundaries, so the original ReadCultureInfosFromCurrentThread
            // / WriteCultureInfosToCurrentThread helper-method version paid two
            // Thread.CurrentThread reads per call. Local-only (vs iter=030's CPEC
            // _thread field) keeps the value in a register and avoids the field
            // write/read overhead that swamped the savings last time.
            Thread t = Thread.CurrentThread;
            executionContext._callback = callback;
            executionContext._state = state;
            executionContext._culture = t.CurrentCulture;
            executionContext._uICulture = t.CurrentUICulture;

            try
            {
                ExecutionContext.Run(
                    executionContext._context,
                    CulturePreservingExecutionContext.CallbackWrapperDelegate,
                    executionContext);
            }
            finally
            {
                // Restore culture information - it might have been modified during
                // the callback execution. EC.Run cannot hop threads, so the cached
                // Thread reference is still valid here.
                t.CurrentCulture = executionContext._culture;
                t.CurrentUICulture = executionContext._uICulture;
            }

            ReturnToPool(executionContext);
}

        // Single-Run-per-CPEC lifecycle: dispose the inner EC, clear the captured
        // state, and stash the (now empty) instance into the thread-local pool so
        // the next Capture() on this thread can reuse it. Skipped on the
        // exception path (the CPEC just GCs in that case).
        private static void ReturnToPool(CulturePreservingExecutionContext ctx)
        {
            ctx._context?.Dispose();
            ctx._context = null;
            ctx._callback = null;
            ctx._state = null;
            ctx._culture = null;
            ctx._uICulture = null;
            ctx._disposed = true;

            if (s_pooled == null)
            {
                s_pooled = ctx;
            }
        }

        #endregion

        #region Private Methods

        /// <summary>
        ///     Executes the callback supplied to the <see cref="Run(CulturePreservingExecutionContext, ContextCallback, object)"/> method
        ///     and saves <see cref="Thread.CurrentUICulture"/> and <see cref="Thread.CurrentCulture"/> values immediately
        ///     afterwards.
        /// </summary>
        /// <param name="obj">
        ///     The <see cref="CulturePreservingExecutionContext"/> instance whose <see cref="_callback"/> /
        ///     <see cref="_state"/> fields hold the user callback and its state argument, and whose
        ///     <see cref="_culture"/> / <see cref="_uICulture"/> fields hold the culture snapshot taken
        ///     by <see cref="Run(CulturePreservingExecutionContext, ContextCallback, object)"/>.
        /// </param>
        private static void CallbackWrapper(object obj)
        {
            var executionContext = (CulturePreservingExecutionContext)obj;

            // Single Thread.CurrentThread local for both the pre-callback restore
            // and the post-callback recapture. Avoids three redundant TLS reads
            // (Thread.CurrentThread is an [Intrinsic] but the JIT does not CSE
            // it across the four .CurrentCulture/.CurrentUICulture accesses the
            // original WriteCultureInfosToCurrentThread + ReadCultureInfosFromCurrentThread
            // helper-method pair performed). EC.Run is guaranteed to call this
            // wrapper on the same thread that invoked it, so the local Thread
            // reference is the same one Run() saw at entry.
            Thread t = Thread.CurrentThread;

            // Restore culture information previously saved from the call site,
            // call into the callback, and recapture culture information which
            // might have been updated by the callback.
            //
            // The callback is guaranteed to be non-null by Run, so an explicit
            // check is not needed here.
            t.CurrentCulture = executionContext._culture;
            t.CurrentUICulture = executionContext._uICulture;

            ContextCallback callback = executionContext._callback;
            object state = executionContext._state;
            callback.Invoke(state);

            executionContext._culture = t.CurrentCulture;
            executionContext._uICulture = t.CurrentUICulture;
        }

        #endregion

        #region Constructors

        static CulturePreservingExecutionContext()
        {
            CallbackWrapperDelegate = new ContextCallback(CulturePreservingExecutionContext.CallbackWrapper);
        }


        private CulturePreservingExecutionContext(ExecutionContext ec)
        {
            _context = ec;
        }

        #endregion

        #region IDisposable Support

        /// <summary>
        ///     Disposes the encapsulated <see cref="ExecutionContext"/> instance.
        /// </summary>
        /// <param name="disposing"></param>
        protected virtual void Dispose(bool disposing)
        {
            if (!_disposed && disposing)
            {
                _context?.Dispose();
                _disposed = true;
            }
        }

        /// <summary>
        ///     Releases all resources used by the current instance of the <see cref="CulturePreservingExecutionContext"/>
        ///     class, which will indirectly release the resources held by the encapsulated <see cref="ExecutionContext"/>
        ///     instance.
        /// </summary>
        public void Dispose()
        {
            Dispose(true);
        }

        private bool _disposed = false;

        #endregion

        #region Private Fields

        private ExecutionContext _context;

        // User callback + state stashed by Run() so CallbackWrapper can pull them off
        // the CPEC instance instead of off a separately-allocated CultureAndContextManager.
        private ContextCallback _callback;
        private object _state;

        // Culture snapshot — captured by Run() (host culture entering the dispatch),
        // restored by CallbackWrapper before invoking the user callback, then re-read
        // after the callback so that any culture changes the callback made survive past
        // ExecutionContext.Run's own restore.
        private CultureInfo _culture;
        private CultureInfo _uICulture;

        // static delegate to prevent repeated implicit allocations during Run
        private static ContextCallback CallbackWrapperDelegate;

        // Thread-local single-element pool. Populated by Run()'s ReturnToPool epilogue,
        // drained by Capture() when non-null. Per-thread isolation lets the dispatcher
        // thread's tight Capture-Run-Capture-Run cycle reuse one CPEC instance forever
        // without locking. Producer-thread Capture (e.g. BackgroundWorker enqueuing a
        // dispatcher operation) misses this pool harmlessly because the consumer
        // (dispatcher) thread refills its own [ThreadStatic].
        [ThreadStatic]
        private static CulturePreservingExecutionContext s_pooled;

        #endregion
    }
}
