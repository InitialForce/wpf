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
            Debug.Assert(numArgs == 0 || // old API, no args
                         numArgs == 1 || // old API, 1 arg, the args param is it
                         numArgs == -1); // new API, any number of args, the args param is an array of them

            // Hot paths: skip the variadic (-1) unpack and intermediate locals
            // when numArgs is the common 0 or 1 case from the dispatcher loop.
            if (numArgs == 0)
            {
                if (callback is Action action)
                {
                    action();
                    return null;
                }
                if (callback is Dispatcher.ShutdownCallback shutdownCallback)
                {
                    shutdownCallback();
                    return null;
                }
                return callback.DynamicInvoke();
            }

            if (numArgs == 1)
            {
                if (callback is DispatcherOperationCallback dispatcherOperationCallback)
                {
                    return dispatcherOperationCallback(args);
                }
                if (callback is SendOrPostCallback sendOrPostCallback)
                {
                    sendOrPostCallback(args);
                    return null;
                }
                // By pass the args parameter as a single object,
                // DynamicInvoke will wrap it in an object[] due to the
                // params keyword.
                return callback.DynamicInvoke(args);
            }

            // numArgs == -1: variadic — args is an object[] which may collapse
            // to the 0-arg or 1-arg fast path after unpacking.
            object result = null;
            int numArgsEx = numArgs;
            object singleArg = args;
            {
                object[] argsArr = (object[])args;
                if (argsArr == null || argsArr.Length == 0)
                {
                    numArgsEx = 0;
                }
                else if (argsArr.Length == 1)
                {
                    numArgsEx = 1;
                    singleArg = argsArr[0];
                }
            }

            if (numArgsEx == 0)
            {
                if (callback is Action action)
                {
                    action();
                }
                else if (callback is Dispatcher.ShutdownCallback shutdownCallback)
                {
                    shutdownCallback();
                }
                else
                {
                    // The delegate could return anything.
                    result = callback.DynamicInvoke();
                }
            }
            else if (numArgsEx == 1)
            {
                if (callback is DispatcherOperationCallback dispatcherOperationCallback)
                {
                    result = dispatcherOperationCallback(singleArg);
                }
                else if (callback is SendOrPostCallback sendOrPostCallback)
                {
                    sendOrPostCallback(singleArg);
                }
                else
                {
                    // Explicitly pass an object[] to DynamicInvoke so that
                    // it will not try to wrap the arg in another object[].
                    result = callback.DynamicInvoke((object[])args);
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


