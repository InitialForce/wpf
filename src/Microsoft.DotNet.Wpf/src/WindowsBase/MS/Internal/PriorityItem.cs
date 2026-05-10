// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace System.Windows.Threading
{
    internal class PriorityItem<T>
    {
        public PriorityItem(T data)
        {
            _data = data;
        }

        // _data is mutable so PriorityQueue can return cleared items to its
        // reusable-item pool (set to default(T) on remove) and re-bind a fresh
        // payload when popping from the pool on Enqueue.
        public T Data { get { return _data; } internal set { _data = value; } }
        public bool IsQueued { get { return _chain != null; } }

        // Note: not used
        // public DispatcherPriority Priority { get { return _chain.Priority; } } // NOTE: should be Priority

        internal PriorityItem<T> SequentialPrev {get{return _sequentialPrev;} set{_sequentialPrev=value;}}
        internal PriorityItem<T> SequentialNext {get{return _sequentialNext;} set{_sequentialNext=value;}}

        internal PriorityChain<T> Chain {get{return _chain;} set{_chain=value;}}
        internal PriorityItem<T> PriorityPrev {get{return _priorityPrev;} set{_priorityPrev=value;}}
        internal PriorityItem<T> PriorityNext {get{return _priorityNext;} set{_priorityNext=value;}}

        private T _data;
        
        private PriorityItem<T> _sequentialPrev;
        private PriorityItem<T> _sequentialNext;

        private PriorityChain<T> _chain;
        private PriorityItem<T> _priorityPrev;
        private PriorityItem<T> _priorityNext;
    }
}

