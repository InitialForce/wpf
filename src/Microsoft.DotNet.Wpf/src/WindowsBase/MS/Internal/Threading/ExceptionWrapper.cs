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
        public object TryCatchWhen(object source, Delegate callback, object args, int numArgs, Delegate catchHandler)
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

        // Hot-path entry point. The two checks below cover the dominant dispatcher
        // workloads: numArgs==0 + Action (the Dispatcher.Invoke(Action) /
        // BeginInvoke(Action) flows) and numArgs==1 + DispatcherOperationCallback
        // (the legacy Invoke(DispatcherOperationCallback, object) / BeginInvoke
        // flows). Everything else — Dispatcher.ShutdownCallback, SendOrPostCallback,
        // arbitrary delegates, and the numArgs==-1 params-array unwrap path — falls
        // into InternalRealCallSlow, which is kept out-of-line so the hot path's
        // emitted machine code stays tight and inlines cleanly into TryCatchWhen.
        // AggressiveInlining is required because the method is moderate-sized when
        // the slow body is included; with it extracted, the hot body is small enough
        // that inlining into TryCatchWhen lets the JIT fold checks across the
        // try/catch frame and avoid a second method-call frame on every dispatch.
        [MethodImpl(MethodImplOptions.AggressiveInlining)]
        private object InternalRealCall(Delegate callback, object args, int numArgs)
        {
            // Fast path: numArgs == 0 with an Action callback (dispatcher's most
            // frequent workload). Direct invoke skips the args/numArgs decode
            // cascade and the InternalRealCallSlow method call.
            if (numArgs == 0 && callback is Action action)
            {
                action();
                return null;
            }

            // Fast path: numArgs == 1 with a DispatcherOperationCallback (second
            // most frequent — legacy DispatcherOperationCallback signature).
            if (numArgs == 1 && callback is DispatcherOperationCallback dispatcherOperationCallback)
            {
                return dispatcherOperationCallback(args);
            }

            return InternalRealCallSlow(callback, args, numArgs);
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        private static object InternalRealCallSlow(Delegate callback, object args, int numArgs)
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
                    // Reachable when numArgs==-1 unwraps to a 0-arg call.
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
                    // Reachable when numArgs==-1 unwraps to a 1-arg call.
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


