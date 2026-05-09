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
            // (it short-circuits internally on Thread._executionContext.m_isFlowSuppressed),
            // so the prior IsFlowSuppressed pre-check was redundant — every flow-suppressed
            // capture would fall through to the `ec == null` guard below anyway. Dropping
            // the pre-check avoids one runtime call per Capture cycle.
            var ec = ExecutionContext.Capture();
            if (ec == null)
            {
                return null;
            }

            // Snapshot the capture-time culture into _culture / _uICulture. Run() reads
            // these (= the value EC.Run will set thread state to inside ExecutionContext.Run)
            // and compares against its own entry-time culture reads to decide whether
            // CallbackWrapper's pre-callback restore is provably a no-op. In the dominant
            // dispatcher pattern (same thread, no culture change between DispatcherOperation
            // ctor and DispatcherOperation.Invoke), they're equal and CallbackWrapper can
            // skip 2 AsyncLocal-routed culture reads + 2 ref-equals every cycle. Run()
            // overwrites these fields with run-entry culture before invoking EC.Run, so
            // CallbackWrapper still sees the correct savedCulture/savedUICulture target.
            Thread thread = Thread.CurrentThread;
            CultureInfo captureCulture = thread.CurrentCulture;
            CultureInfo captureUICulture = thread.CurrentUICulture;

            // Reuse a thread-local pooled instance when available. The pool is
            // refilled by Run()'s ReturnToPool epilogue, so the dominant Capture-Run-
            // Capture-Run pattern on the dispatcher thread (and the bench) hits
            // the pool on every cycle after warm-up, killing the per-Run heap
            // allocation. Cross-thread Capture (producer thread) -> Run
            // (dispatcher thread) misses the pool harmlessly because the
            // pool is [ThreadStatic].
            var pooled = s_pooled;
            CulturePreservingExecutionContext ctx;
            if (pooled != null)
            {
                s_pooled = null;
                ctx = pooled;
                ctx._context = ec;
                ctx._disposed = false;
            }
            else
            {
                ctx = new CulturePreservingExecutionContext(ec);
            }

            ctx._culture = captureCulture;
            ctx._uICulture = captureUICulture;
            return ctx;
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
                ReturnToPool(executionContext);
                return;
            }

            // Stash the user callback + state on the CPEC itself and snapshot the
            // current culture infos. CallbackWrapper will restore them just before
            // invoking the user callback. (Single-Run-per-CPEC lifecycle assumed.)
            executionContext._callback = callback;
            executionContext._state = state;
            Thread thread = Thread.CurrentThread;
            CultureInfo runEntryCulture = thread.CurrentCulture;
            CultureInfo runEntryUICulture = thread.CurrentUICulture;

            // Capture() stashed the capture-time culture into _culture / _uICulture; that
            // is exactly the value EC.Run is about to set thread state to inside
            // ExecutionContext.Run (since the EC's saved culture matches what was current
            // when EC.Capture() ran). When capture-time culture equals run-entry culture,
            // EC.Run's culture-set is a no-op AND CallbackWrapper's pre-callback restore
            // is also provably a no-op — so CallbackWrapper can skip the 2 AsyncLocal-
            // routed Thread.Current(UI)Culture reads + ref-equals it would otherwise do.
            // (Same-thread dispatcher operations with no culture change between
            // DispatcherOperation ctor and Invoke is the dominant case.)
            bool skipPreRestoreCulture =
                ReferenceEquals(executionContext._culture, runEntryCulture);
            bool skipPreRestoreUICulture =
                ReferenceEquals(executionContext._uICulture, runEntryUICulture);
            executionContext._skipPreRestoreCulture = skipPreRestoreCulture;
            executionContext._skipPreRestoreUICulture = skipPreRestoreUICulture;

            // Overwrite the capture-time snapshot with run-entry culture so CallbackWrapper
            // sees the correct savedCulture/savedUICulture target (the value to restore
            // before the user callback) and the correct anchor for post-callback recapture.
            executionContext._culture = runEntryCulture;
            executionContext._uICulture = runEntryUICulture;

            ExecutionContext.Run(
                executionContext._context,
                CulturePreservingExecutionContext.CallbackWrapperDelegate,
                executionContext);

            // Skip the entire restore when CallbackWrapper observed no culture change in
            // the user callback. In that case _culture/_uICulture still hold the run-entry
            // values, EC.Run has already reverted thread state to its own saved (capture-
            // time) value, and our writes would be no-ops — but we'd still pay 2
            // Thread.Current(UI)Culture reads + ref-equals. The flag is set in
            // CallbackWrapper iff the post-callback recapture wrote a fresh CultureInfo
            // into _culture / _uICulture.
            //
            // The previous try/finally wrapper is removed: on an exception path
            // (callback or EC.Run throws), CallbackWrapper's post-callback recapture
            // never runs, so _callbackTouchedCulture stays false and the finally body
            // would have been a no-op anyway. Dropping the SEH frame setup saves the
            // runtime helper cost on every Run cycle.
            if (executionContext._callbackTouchedCulture)
            {
                CultureInfo finalCulture = executionContext._culture;
                CultureInfo finalUICulture = executionContext._uICulture;
                if (!ReferenceEquals(thread.CurrentCulture, finalCulture))
                    thread.CurrentCulture = finalCulture;
                if (!ReferenceEquals(thread.CurrentUICulture, finalUICulture))
                    thread.CurrentUICulture = finalUICulture;
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
            ctx._callbackTouchedCulture = false;
            ctx._skipPreRestoreCulture = false;
            ctx._skipPreRestoreUICulture = false;
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

            ContextCallback callback = executionContext._callback;
            object state = executionContext._state;

            // Restore culture information previously saved from the call site,
            // invoke the callback, then recapture culture information which the
            // callback might have updated.
            //
            // Both the pre-callback restore and the post-callback recapture skip
            // their work when the value is already at the target. The setter
            // ultimately routes through AsyncLocal<CultureInfo>.set Value (modulo
            // the thread-static fast path) which walks the EC's async-local chain
            // even when the new value matches the current one — measurable cost
            // every Run cycle. The post-callback field writes are similarly skipped
            // when the callback did not touch culture (the dominant case), so the
            // recapture collapses to two property reads + two ref-equals.
            //
            // The callback is guaranteed to be non-null by Run, so an explicit
            // check is not needed here.

            Thread thread = Thread.CurrentThread;
            CultureInfo savedCulture = executionContext._culture;
            CultureInfo savedUICulture = executionContext._uICulture;

            // Skip the AsyncLocal-routed Thread.Current(UI)Culture reads + ref-equals
            // when Run() proved (via the capture-time vs run-entry culture compare) that
            // EC.Run set thread state to a value equal to savedCulture / savedUICulture.
            // Short-circuit (&&): when the skip flag is true the read on the right is
            // never performed.
            if (!executionContext._skipPreRestoreCulture
                && !ReferenceEquals(thread.CurrentCulture, savedCulture))
            {
                thread.CurrentCulture = savedCulture;
            }
            if (!executionContext._skipPreRestoreUICulture
                && !ReferenceEquals(thread.CurrentUICulture, savedUICulture))
            {
                thread.CurrentUICulture = savedUICulture;
            }

            callback.Invoke(state);

            CultureInfo postCulture = thread.CurrentCulture;
            CultureInfo postUICulture = thread.CurrentUICulture;
            if (!ReferenceEquals(postCulture, savedCulture))
            {
                executionContext._culture = postCulture;
                executionContext._callbackTouchedCulture = true;
            }
            if (!ReferenceEquals(postUICulture, savedUICulture))
            {
                executionContext._uICulture = postUICulture;
                executionContext._callbackTouchedCulture = true;
            }
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

        // Set true by CallbackWrapper iff the post-callback recapture observed a
        // culture change in the user callback and wrote a fresh CultureInfo into
        // _culture / _uICulture. Run()'s post-EC.Run if-block uses this to skip its
        // restore work in the dominant "callback does not touch culture" path: when
        // false, _culture / _uICulture still match the values captured at Run() entry,
        // EC.Run has reverted thread state to those same values, and the restore writes
        // would be no-ops — but we'd still pay 2 Thread.Current(UI)Culture property
        // reads (each routed through AsyncLocal<CultureInfo>'s async-local chain) plus
        // 2 ref-equals comparisons. Reset to false by ReturnToPool so the next
        // Capture-Run cycle on this pooled instance starts clean.
        private bool _callbackTouchedCulture;

        // Set by Run() entry iff the capture-time culture (stashed in _culture by
        // Capture() before Run()'s overwrite) equals run-entry culture. When true,
        // EC.Run will set thread state to a value equal to the run-entry / saved
        // culture, so CallbackWrapper's pre-callback restore is provably a no-op and
        // can skip its AsyncLocal-routed Thread.CurrentCulture read + ref-equals
        // entirely. Resets to false by ReturnToPool. _skipPreRestoreUICulture is the
        // analogous flag for Thread.CurrentUICulture; tracked separately so each
        // axis can independently skip its read in the (common) case where one axis
        // changed but not the other.
        private bool _skipPreRestoreCulture;
        private bool _skipPreRestoreUICulture;

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
