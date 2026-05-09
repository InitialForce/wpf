// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

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
            try
            {
                // Hot path #1: numArgs == 0 with Action delegate. This is the
                // dominant production case — every Dispatcher.Invoke(Action) /
                // BeginInvoke(Action) eventually lands here. Inline directly so
                // the fast path does not pay the InternalRealCall frame, the
                // numArgs==-1 normalization branch, or the
                // ShutdownCallback type-test, and so the JIT can return the
                // null literal directly from a register instead of routing it
                // through a `result` local that must survive the try region.
                if (numArgs == 0 && callback is Action action)
                {
                    action();
                    return null;
                }
                // Hot path #2: numArgs == 1 with DispatcherOperationCallback.
                // Second most common — every HwndSubclass.SubclassWndProc Win32
                // message dispatch hits this via
                // dispatcher.Invoke(Send, _dispatcherOperationCallback, param).
                if (numArgs == 1 && callback is DispatcherOperationCallback doc)
                {
                    return doc(args);
                }
                // Cold path: ShutdownCallback / SendOrPostCallback /
                // DynamicInvoke / numArgs==-1 normalization. Body is unchanged
                // — this preserves InternalRealCall's IL size + JIT shape and
                // avoids the cross-benchmark side effects that sank
                // iter=excwrap-irc-hotpath-extract (NegativeControlDynamicInvoke
                // regressed +14.74 ns when InternalRealCall was restructured).
                return InternalRealCall(callback, args, numArgs);
            }
            catch (Exception e) when (FilterException(source, e))
            {
                if (!CatchException(source, e, catchHandler))
                {
                    throw;
                }
            }

            return null;
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


