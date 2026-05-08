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
    ///             CPEC.Run(callback)          // CPEC saves culture info $2 by
    ///                                         // reading it directly into CPEC fields
    ///                 Calls EC.Run(CallbackWrapper, state: this CPEC)
    ///                     CallbackWrapper()   // EC will run this under $1
    ///                         CallbackWrapper will restore culture info $2
    ///                         callback()      // callback is run under $2, it modifies culture info to $3
    ///                         CallbackWrapper saves $3 for later use
    ///                 EC.Run terminates       // EC reverts culture info to $1
    ///             CPEC.Run restores $3 which was saved by CallbackWrapper
    ///         DispatcherOperation completes - current culture info is set to $3
    ///
    ///     This flow is similar to the default behavior on .NET 4.5.2 and earlier.
    /// </remarks>
    internal class CulturePreservingExecutionContext : IDisposable
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
            // ExecutionContext.SuppressFlow had been called - we expect
            // ExecutionContext.Capture() to return null, so match that
            // behavior and return null.
            if (ExecutionContext.IsFlowSuppressed())
            {
                return null;
            }

            ExecutionContext ec = ExecutionContext.Capture();
            if (ec == null)
            {
                // If ExecutionContext.Capture() returns null for any other
                // reason besides IsFlowSuppressed, then match that behavior
                // and return null
                return null;
            }

            // Reuse a per-thread pooled CPEC instance if one is available.
            // Capture and Dispose may run on different threads (Capture on the
            // calling thread, Dispose on the dispatcher thread); the pool slot
            // is per-thread, so each thread maintains at most one cached CPEC
            // and the steady-state heap allocation rate drops to zero after warm-up.
            CulturePreservingExecutionContext pooled = s_pooledInstance;
            if (pooled != null)
            {
                s_pooledInstance = null;
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
            ArgumentNullException.ThrowIfNull(executionContext);

            if (callback == null) return; // Bail out early if callback is null

            // Compat switch is set, defer directly to EC.Run
            if (BaseAppContextSwitches.DoNotUseCulturePreservingDispatcherOperations)
            {
                ExecutionContext.Run(executionContext._context, callback, state);
                return;
            }

            // Save callback, state, and current-thread culture into the CPEC
            // instance itself, then pass the CPEC as the state object to
            // ExecutionContext.Run. This avoids the per-call allocation of a
            // CultureAndContextManager wrapper that the previous implementation
            // used; the CPEC instance is already a heap object reachable across
            // the EC.Run invocation, so reusing it as the state carrier costs
            // nothing additional.
            executionContext._callback = callback;
            executionContext._state = state;
            executionContext.ReadCultureInfosFromCurrentThread();

            try
            {
                ExecutionContext.Run(
                    executionContext._context,
                    CulturePreservingExecutionContext.CallbackWrapperDelegate,
                    executionContext);
            }
            finally
            {
                // Restore culture information - it might have been
                // modified during the callback execution.
                executionContext.WriteCultureInfosToCurrentThread();

                // Drop references to user-supplied callback/state so that a
                // subsequently pooled instance cannot pin them in the GC graph.
                executionContext._callback = null;
                executionContext._state = null;
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
        ///     The CulturePreservingExecutionContext instance whose Run set up
        ///     the callback/state/culture fields. Passed through ExecutionContext.Run
        ///     as the state argument so we avoid the per-call wrapper allocation.
        /// </param>
        private static void CallbackWrapper(object obj)
        {
            CulturePreservingExecutionContext cpec = (CulturePreservingExecutionContext)obj;

            ContextCallback callback = cpec._callback;
            object state = cpec._state;

            // Restore culture information previously saved from the call site,
            // call into the callback, and recapture culture information which
            // might have been updated by the callback.
            //
            // The callback is guaranteed to be non-null by Run, so an explicit
            // check is not needed here.

            cpec.WriteCultureInfosToCurrentThread();
            callback.Invoke(state);
            cpec.ReadCultureInfosFromCurrentThread();
        }

        private void ReadCultureInfosFromCurrentThread()
        {
            Thread thread = Thread.CurrentThread;
            _culture = thread.CurrentCulture;
            _uICulture = thread.CurrentUICulture;
        }

        private void WriteCultureInfosToCurrentThread()
        {
            Thread thread = Thread.CurrentThread;
            thread.CurrentCulture = _culture;
            thread.CurrentUICulture = _uICulture;
        }

        #endregion

        #region Constructors

        private CulturePreservingExecutionContext(ExecutionContext context)
        {
            _context = context;
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
                _context = null;
                // _callback / _state are already cleared in Run's finally; clear
                // _culture / _uICulture so the pooled instance does not retain
                // references to potentially user-thread-specific CultureInfo objects.
                _culture = null;
                _uICulture = null;
                _disposed = true;

                // Return to the per-thread pool slot if it is empty. The slot
                // is per-thread (ThreadStatic), so the instance ends up cached
                // on whichever thread happened to run Dispose - typically the
                // dispatcher thread, which is also the thread that will next
                // call Capture for the next dispatcher operation, giving full
                // pool reuse in the common single-UI-thread case.
                if (s_pooledInstance == null)
                {
                    s_pooledInstance = this;
                }
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

        // Per-Run scratch fields. Folded in from the previous CultureAndContextManager
        // helper class so that Run() does not need to allocate a wrapper object for
        // the duration of the EC.Run callback - the CPEC itself carries the data.
        private ContextCallback _callback;
        private object _state;
        private CultureInfo _culture;
        private CultureInfo _uICulture;

        // Static delegate to prevent repeated implicit allocations during Run.
        private static readonly ContextCallback CallbackWrapperDelegate =
            new ContextCallback(CulturePreservingExecutionContext.CallbackWrapper);

        // Per-thread pool slot. Holds at most one disposed-and-reset CPEC instance
        // ready for the next Capture on this thread. Capture and Dispose may run
        // on different threads; in that case the instance simply ends up in
        // whichever thread's pool ran Dispose - benign, just less reuse on threads
        // that only Capture-without-Dispose or vice versa. ThreadStatic reads as
        // null on threads that have not yet stored anything, which is the desired
        // initial state - no static-ctor or per-thread initialization needed.
        [ThreadStatic]
        private static CulturePreservingExecutionContext s_pooledInstance;

        #endregion
    }
}
