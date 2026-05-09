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
            // Invert the gate so the slow (with-handlers) tail call is the
            // out-of-line jump and the fast path is the fall-through. Same
            // assembly shape as `if (==null && ==null) { fast } else slow`,
            // but reads more directly as "exit early; rest is hot".
            if (Catch != null || Filter != null)
            {
                return TryCatchWhenWithHandlers(source, callback, args, numArgs, catchHandler);
            }

            // Fast path: dispatch by numArgs+type. Switch on numArgs lets
            // the JIT lower the dual-shape selection to a single cmp/jne
            // pair (with default→InternalRealCall as the fall-through),
            // versus the prior `if (numArgs==0 ...) ... if (numArgs==1 ...)`
            // structure which paid a numArgs==0 cmp+jne on every Doc-path
            // call before falling into the numArgs==1 branch. Both Action
            // and DispatcherOperationCallback are sealed delegate types so
            // the JIT lowers `is X x` directly to a method-table compare
            // (no isinst/cast bookkeeping); a successful match binds the
            // typed local without a copy.
            switch (numArgs)
            {
                case 0:
                    if (callback is Action action)
                    {
                        action();
                        return null;
                    }
                    break;
                case 1:
                    if (callback is DispatcherOperationCallback doc)
                    {
                        return doc(args);
                    }
                    break;
            }
            return InternalRealCall(callback, args, numArgs);
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


