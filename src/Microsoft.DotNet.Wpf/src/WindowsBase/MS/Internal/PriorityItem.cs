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

        // Re-arm a node that was previously popped from PriorityQueue<T>'s thread-local
        // (per-Dispatcher = per-thread, _instanceLock-guarded) free list and is about to
        // be re-inserted as a fresh queue node. The pool only ever holds nodes that were
        // detached by RemoveItem, which has already nulled the four linked-list pointers
        // and the chain reference; the assertions in InsertItemInSequentialChain /
        // InsertItemInPriorityChain therefore continue to hold after Reset just like they
        // did after `new PriorityItem<T>(data)`. The only mutation Reset needs to make is
        // restamping the data slot — which ClearForPool nulled out when the node was
        // returned to the pool — to point at the new owning DispatcherOperation.
        internal void Reset(T data)
        {
            _data = data;
        }

        // Inverse of Reset: called by PriorityQueue<T>.RemoveItem immediately before the
        // node is pushed onto the free list. Drops the data back-reference so a long-lived
        // pooled node cannot keep a completed DispatcherOperation (and its captured
        // delegate / arg graph) alive across cycles when steady-state queue depth is much
        // smaller than the pool capacity.
        internal void ClearForPool()
        {
            _data = default(T);
        }

        public T Data {get{return _data;}}
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

