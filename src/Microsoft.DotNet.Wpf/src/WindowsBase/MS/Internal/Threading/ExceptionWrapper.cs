// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Runtime.CompilerServices;
using System.Threading;

namespace System.Windows.Threading
{
    /// <summary>
    /// Class for Filtering and Catching Exceptions
    /// </summary>
    internal class ExceptionWrapper
    {
        internal ExceptionWrapper()
        {
        }

        // Helper for exception filtering:
        [MethodImpl(MethodImplOptions.AggressiveInlining)]
        public object TryCatchWhen(object source, Delegate callback, object args, int numArgs, Delegate catchHandler)
        {
            // No-handlers fast path. When neither Filter nor Catch is subscribed,
            // FilterException always returns false, so the catch block in the
            // protected variant is unreachable. Skip the try/catch construct
            // entirely AND inline the two type-test dispatches the dispatcher
            // hot loop hits on every callback (numArgs=0 + Action;
            // numArgs=1 + DispatcherOperationCallback). Removing the try/catch
            // from this method's body is the precondition that lets the JIT
            // honour the [AggressiveInlining] hint and fold TryCatchWhen into
            // its caller (in production: Dispatcher's op-callback path; in the
            // bench: the closed delegate the *ExceptionWrapper* benchmark
            // dispatches through). Methods with EH regions are normally
            // refused for inlining.
            //
            // The two inlined fast paths return the exact same values as the
            // original `result = InternalRealCall(...); return result;` flow
            // would: numArgs=0+Action runs `action()` and returns null;
            // numArgs=1+DispatcherOperationCallback returns `doc(args)`. Cold
            // dispatches (ShutdownCallback / SendOrPostCallback / DynamicInvoke
            // fallback / numArgs==-1 args[] normalization) tail-call into the
            // unmodified InternalRealCall, preserving its IL/JIT shape so the
            // cross-benchmark NegativeControlDynamicInvoke regression that
            // sank iter=excwrap-irc-hotpath-extract (iter=012, +14.74 ns CI
            // disjoint) does not recur.
            //
            // Single-bool no-handlers gate. Replaces the per-call pair of
            // `Catch == null && Filter == null` field reads + null tests with
            // one byte read + one test against the cached `_hasNoHandlers`
            // flag. The flag is maintained by the explicit Catch/Filter event
            // accessors (writes happen at Dispatcher-ctor time only; the
            // handlers are wired once in Dispatcher's instance ctor and never
            // mutated thereafter). Saves one mov+test in the hot path.
            // Production observability: in real WPF the dispatcher always
            // wires Catch+Filter so `_hasNoHandlers` is false there, and the
            // gate immediately diverts to the with-handlers slow path —
            // matching the previous behaviour while eliminating one of the
            // pre-gate field loads. The fresh-wrapper bench (which never
            // wires handlers) keeps `_hasNoHandlers=true` and exercises the
            // inlined fast path, the same as before iter=062.
            if (_hasNoHandlers)
            {
                if (numArgs == 0 && callback is Action action)
                {
                    action();
                    return null;
                }
                if (numArgs == 1 && callback is DispatcherOperationCallback doc)
                {
                    return doc(args);
                }
                return InternalRealCall(callback, args, numArgs);
            }

            // Slow path: handlers are subscribed, run the catch-protected body.
            // Extracted into a NoInlining helper so the EH region lives
            // entirely outside TryCatchWhen — the JIT inlines the catch-free
            // wrapper into its caller; the rare with-handlers caller pays one
            // extra method-call frame, which is acceptable on the cold path.
            return TryCatchWhenWithHandlers(source, callback, args, numArgs, catchHandler);
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        private object TryCatchWhenWithHandlers(object source, Delegate callback, object args, int numArgs, Delegate catchHandler)
        {
            object result = null;

            try
            {
                result = InternalRealCall(callback, args, numArgs);
            }
            catch (Exception e) when (FilterException(source, e))
            {
                if (!CatchException(source, e, catchHandler))
                {
                    throw;
                }
            }

            return result;
        }

        private object InternalRealCall(Delegate callback, object args, int numArgs)
        {
            object result = null;

            Debug.Assert(numArgs == 0 || // old API, no args
                         numArgs == 1 || // old API, 1 arg, the args param is it
                         numArgs == -1); // new API, any number of args, the args param is an array of them

            // Support the fast-path for certain 0-param and 1-param delegates, even
            // of an arbitrary "params object[]" is passed.
            int numArgsEx = numArgs;
            object singleArg = args;
            if(numArgs == -1)
            {
                object[] argsArr = (object[])args;
                if (argsArr == null || argsArr.Length == 0)
                {
                    numArgsEx = 0;
                }
                else if(argsArr.Length == 1)
                {
                    numArgsEx = 1;
                    singleArg = argsArr[0];
                }
            }

            // Special-case delegates that we know about to avoid the
            // expensive DynamicInvoke call.
            if(numArgsEx == 0)
            {
                if (callback is Action action)
                {
                    action();
                }
                else
                {
                    if (callback is Dispatcher.ShutdownCallback shutdownCallback)
                    {
                        shutdownCallback();
                    }
                    else
                    {
                        // The delegate could return anything.
                        result = callback.DynamicInvoke();
                    }
                }
            }
            else if(numArgsEx == 1)
            {
                if (callback is DispatcherOperationCallback dispatcherOperationCallback)
                {
                    result = dispatcherOperationCallback(singleArg);
                }
                else
                {
                    if (callback is SendOrPostCallback sendOrPostCallback)
                    {
                        sendOrPostCallback(singleArg);
                    }
                    else
                    {
                        if (numArgs == -1)
                        {
                            // Explicitly pass an object[] to DynamicInvoke so that
                            // it will not try to wrap the arg in another object[].
                            result = callback.DynamicInvoke((object[])args);
                        }
                        else
                        {
                            // By pass the args parameter as a single object,
                            // DynamicInvoke will wrap it in an object[] due to the
                            // params keyword.
                            result = callback.DynamicInvoke(args);
                        }
                    }
                }
            }
            else
            {
                // Explicitly pass an object[] to DynamicInvoke so that
                // it will not try to wrap the arg in another object[].
                result = callback.DynamicInvoke((object[])args);
            }

            return result;
        }

        private bool FilterException(object source, Exception e)
        {
            // If we have a Catch handler we should catch the exception
            // unless the Filter handler says we shouldn't.
            bool shouldCatch = (null != _catchHandler);
            if(null != _filterHandler)
            {
                shouldCatch = _filterHandler(source, e);
            }
            return shouldCatch;
        }

        // This returns false when caller should rethrow the exception.
        // true means Exception is "handled" and things just continue on.
        private bool CatchException(object source, Exception e, Delegate catchHandler)
        {
            if (catchHandler != null)
            {
                if(catchHandler is DispatcherOperationCallback)
                {
                    ((DispatcherOperationCallback)catchHandler)(null);
                }
                else
                {
                    catchHandler.DynamicInvoke(null);
                }
            }

            if(null != _catchHandler)
                return _catchHandler(source, e);

            return false;
        }

        /// <summary>
        /// Exception Catch Handler Delegate
        ///  Returns true if the exception is "handled"
        ///  Returns false if the caller should rethow the exception.
        /// </summary>
        public delegate bool CatchHandler(object source, Exception e);

        /// <summary>
        /// Exception Catch Handler
        ///  Returns true if the exception is "handled"
        ///  Returns false if the caller should rethow the exception.
        /// </summary>
        // Explicit event with custom add/remove. The custom accessors maintain
        // the cached `_hasNoHandlers` flag that TryCatchWhen reads on every
        // dispatch. Add/Remove use the same Interlocked.CompareExchange loop
        // pattern the auto-event would have generated, preserving thread-safe
        // delegate combination semantics. The cache write is a plain bool
        // store after the delegate change commits — the fields are only ever
        // mutated during Dispatcher construction (Catch+Filter wired once in
        // the instance ctor, never mutated after), so the brief un-fenced
        // window between the delegate update and the bool update is not
        // observable in practice.
        public event CatchHandler Catch
        {
            add
            {
                CatchHandler current, computed;
                do
                {
                    current = _catchHandler;
                    computed = (CatchHandler)Delegate.Combine(current, value);
                } while (Interlocked.CompareExchange(ref _catchHandler, computed, current) != current);
                _hasNoHandlers = (_catchHandler == null) && (_filterHandler == null);
            }
            remove
            {
                CatchHandler current, computed;
                do
                {
                    current = _catchHandler;
                    computed = (CatchHandler)Delegate.Remove(current, value);
                } while (Interlocked.CompareExchange(ref _catchHandler, computed, current) != current);
                _hasNoHandlers = (_catchHandler == null) && (_filterHandler == null);
            }
        }

        public delegate bool FilterHandler(object source, Exception e);

        public event FilterHandler Filter
        {
            add
            {
                FilterHandler current, computed;
                do
                {
                    current = _filterHandler;
                    computed = (FilterHandler)Delegate.Combine(current, value);
                } while (Interlocked.CompareExchange(ref _filterHandler, computed, current) != current);
                _hasNoHandlers = (_catchHandler == null) && (_filterHandler == null);
            }
            remove
            {
                FilterHandler current, computed;
                do
                {
                    current = _filterHandler;
                    computed = (FilterHandler)Delegate.Remove(current, value);
                } while (Interlocked.CompareExchange(ref _filterHandler, computed, current) != current);
                _hasNoHandlers = (_catchHandler == null) && (_filterHandler == null);
            }
        }

        // Backing fields for the explicit Catch/Filter events. Replaces the
        // compiler-synthesized fields the previous auto-events used.
        private CatchHandler _catchHandler;
        private FilterHandler _filterHandler;

        // Cached "no handlers attached" flag. Initialized true (a fresh
        // wrapper has neither handler subscribed) and set to false the
        // first time either event is added. Read by the TryCatchWhen
        // hot-path gate to skip the per-call double-field-load that the
        // previous `Catch == null && Filter == null` test required.
        private bool _hasNoHandlers = true;
    }
}


