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
            // its caller. Methods with EH regions are normally refused for
            // inlining.
            //
            // Body shape policy: keep the fast-path body as small as possible
            // so the JIT inline-budget heuristic accepts TryCatchWhen at its
            // call sites. Both cold tails (rare numArgs=-1 / SendOrPost /
            // ShutdownCallback / DynamicInvoke dispatch AND the with-handlers
            // try/catch path) route through TryCatchWhenSlow — that's a single
            // NoInlining call site at the bottom of the function instead of
            // two separate cold-path call sites + an inline `return
            // InternalRealCall(...)`. Fewer call instructions and a smaller
            // function body give the JIT more headroom for the [AggressiveInlining]
            // hint at the dispatcher op-callback call site, and let the IRC /
            // with-handlers paths share their NoInlining frame. Cold tails
            // tail-call through TryCatchWhenSlow; the unmodified InternalRealCall
            // IL shape is preserved inside TryCatchWhenSlow to keep the JIT
            // codegen for that tree byte-identical (this is the precondition
            // that prevented the cross-benchmark NegativeControlDynamicInvoke
            // regression seen in iter=012 from recurring under iter=062).
            if (Catch == null && Filter == null)
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
            }

            return TryCatchWhenSlow(source, callback, args, numArgs, catchHandler);
        }

        // Combined slow / cold-tail helper. Two callers fall here from
        // TryCatchWhen's fast path:
        //   1. No-handlers + dispatch-fallback (numArgs=-1 / args[] normalization,
        //      SendOrPostCallback, Dispatcher.ShutdownCallback, DynamicInvoke
        //      generic fallback) — runs InternalRealCall directly.
        //   2. With-handlers (Catch != null OR Filter != null) — runs
        //      InternalRealCall inside a try/catch-when filter that delegates
        //      to FilterException and CatchException as before.
        // NoInlining keeps the EH region out of TryCatchWhen's body so the JIT
        // remains free to inline the catch-free wrapper into its caller. The
        // rare with-handlers caller pays one extra method-call frame, which is
        // acceptable on the cold path.
        [MethodImpl(MethodImplOptions.NoInlining)]
        private object TryCatchWhenSlow(object source, Delegate callback, object args, int numArgs, Delegate catchHandler)
        {
            if (Catch == null && Filter == null)
            {
                return InternalRealCall(callback, args, numArgs);
            }

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
            bool shouldCatch = (null != Catch);
            if(null != Filter)
            {
                shouldCatch = Filter(source, e);
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

            if(null != Catch)
                return Catch(source, e);

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
        public event CatchHandler Catch;

        public delegate bool FilterHandler(object source, Exception e);
        public event FilterHandler Filter;
    }
}


