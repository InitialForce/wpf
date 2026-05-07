# DESIGN NOTES — dispatcher cluster

**Generated:** 2026-05-07  
**Designer:** Sonnet 4.6 (bench-author swarm, round 1)

---

## Section 1 — Cluster Summary

The 10 entries cover the full synchronous `Invoke` call chain:
`Dispatcher.Invoke(Action)` → `InvokeImpl` → `DispatcherOperation.Invoke()` →
`InvokeImpl()` (inner), plus `ProcessQueue`, `PushFrameImpl`, and two
`DispatcherOperation.Wait` variants. Almost every entry lands here in the profile
because the application calls `Dispatcher.Invoke` continuously for cross-thread
marshaling. The central difficulty: the slow path requires a running Win32 message pump
(`GetMessage` loop); benchmarks running without it will block forever or crash.
**Critical exception:** `Dispatcher.Invoke(Action)` at `DispatcherPriority.Send` on
the Dispatcher's own thread hits a **fast path** that skips the queue entirely and just
swaps the `SynchronizationContext` + calls the callback. This fast path is fully
benchmarkable without a pump.

---

## Section 2 — Per-Entry Analysis

### 2.1 `Dispatcher.Invoke(Action callback)` (cpu: 2.09%)

**Surface:** `dispatcher.Invoke(callback)` where `dispatcher = Dispatcher.CurrentDispatcher` obtained on an STA thread before the benchmark loop.  
**Dispatcher/STA requirement:** Option A — create a dedicated STA thread, call `Dispatcher.CurrentDispatcher` to auto-create the dispatcher, then run the benchmark body on that same thread. Since this hits the `Send`+same-thread fast path (line 577 of Dispatcher.cs), no message pump is needed: it just swaps SyncContext and calls the callback. The benchmark thread IS the Dispatcher thread.  
**Corpus shape:** 50 distinct `Action` delegates (seeded with `new Random(42)`), cycling round-robin. Delegates perform a trivial but non-inlinable computation (XOR accumulator) to prevent dead-code elimination.  
**BDN filter:** `*DispatcherInvokeActionBenchmark*`  
**Negative control:** Call `callback()` directly (no Dispatcher overhead) — should be measurably faster if the SyncContext swap has non-zero cost.  
**Feasibility:** `runnable` (fast path — no pump needed when same thread + Send priority).  
**Option verdict:** Option A — run on a dedicated STA thread as GlobalSetup; BDN drives the benchmark from that thread via a custom `IConfig` that sets `[ThreadingDiagnoser]`. No message pump required because the fast path is unconditionally taken.

---

### 2.2 `Dispatcher.Invoke(Action callback, DispatcherPriority priority, CancellationToken, TimeSpan)` (cpu: 2.09%)

**Surface:** `dispatcher.Invoke(callback, DispatcherPriority.Send, CancellationToken.None, TimeSpan.FromMilliseconds(-1))` — the 4-arg overload that all other `Invoke(Action)` overloads delegate to.  
**Dispatcher/STA requirement:** Same as 2.1 — fast path taken when priority == Send and same thread. Option A.  
**Corpus shape:** Same 50-delegate rotating corpus as 2.1; this entry shares a benchmark class. Separate `[Benchmark]` method.  
**BDN filter:** `*DispatcherInvokeActionBenchmark*` (same class as 2.1).  
**Negative control:** `dispatcher.Invoke(callback, DispatcherPriority.Normal, ...)` — forces the slow path (queue enqueue + Wait), but since there's no pump, this would deadlock. Use `DispatcherPriority.Send` as the control and measure with an empty lambda instead.  
**Feasibility:** `runnable` (fast path via Send + same-thread check).  
**Option verdict:** Option A — same STA-thread setup as 2.1.

---

### 2.3 `Dispatcher.InvokeImpl(DispatcherOperation, CancellationToken, TimeSpan)` (cpu: 2.11%)

**Surface:** `Dispatcher.InvokeImpl` is `private` (line 1312 of Dispatcher.cs). It calls `InvokeAsyncImpl` (queues the operation) then `operation.Wait()`, which blocks waiting for the pump to execute the operation. There is no path through this method that avoids waiting on the queue.  
**Dispatcher/STA requirement:** Option C — this method's only reachable code path from outside the fast-path requires the pump to drain the operation. The `private` access modifier also blocks reflection without `AccessTools` / `[assembly: InternalsVisibleTo]` changes.  
**Corpus shape (for future Option A attempt):** An STA thread + `Dispatcher.Run()` in a background thread; `Invoke` called from a worker thread at `DispatcherPriority.Normal`. 50-operation batch per BDN iteration. High CV risk from OS scheduling.  
**BDN filter:** `*DispatcherInvokeImplBenchmark*` (if ever attempted).  
**Negative control:** N/A (skipped).  
**Feasibility:** `needs-pump`  
**Option verdict:** Option C — `private` access + mandatory pump. The fast-path that bypasses this method is already covered by entries 2.1 and 2.2.

---

### 2.4 `DispatcherOperation.Invoke()` (cpu: 2.09%)

**Surface:** `op.Invoke()` where `op` is an `internal DispatcherOperation`. The method is `internal` (line 394 of DispatcherOperation.cs), so accessible from the microbench project if `InternalsVisibleTo("Microbenchmarks")` is added to WindowsBase. It calls `CulturePreservingExecutionContext.Run` then `_invokeInSecurityContext`, which calls `InvokeImpl()`. No pump needed — it executes inline.  
**Dispatcher/STA requirement:** Option B — add `[assembly: InternalsVisibleTo("Microbenchmarks")]` to WindowsBase and call `op.Invoke()` directly on an STA thread that owns the `Dispatcher`. Construct the operation via the `internal` constructor: `new DispatcherOperation(dispatcher, DispatcherPriority.Normal, action)`.  
**Corpus shape:** 50 distinct `Action` delegates (RNG seed 42). Pre-allocate 50 `DispatcherOperation` objects per iteration; reset `_status = Pending` via reflection between invocations to avoid the "already completed" guard. Alternatively create a fresh `DispatcherOperation` per iteration if status reset proves fragile.  
**BDN filter:** `*DispatcherOperationInvokeBenchmark*`  
**Negative control:** Call `action()` directly, bypassing `CulturePreservingExecutionContext.Run` overhead.  
**Feasibility:** `proxy-only` (requires WPF source change for `InternalsVisibleTo`).  
**Option verdict:** Option B — the execution path is pure managed dispatch, no pump dependency. Needs one-line source patch. If source change is out of scope, downgrade to `skipped`.

---

### 2.5 `DispatcherOperation.InvokeImpl()` (cpu: 2.09%)

**Surface:** `InvokeImpl()` is `private` on `DispatcherOperation` (line 486). It sets up `DispatcherSynchronizationContext`, calls `PromoteTimers`, then calls `InvokeDelegateCore()` (for async semantics) or `dispatcher.WrappedInvoke()` (for legacy). Directly unreachable without reflection or subclassing.  
**Dispatcher/STA requirement:** Option B via subclassing — `DispatcherOperation` is a non-sealed `public` class. A test subclass can override `InvokeDelegateCore()` and call the base through the public `Invoke()` chain to exercise `InvokeImpl` indirectly. However, `InvokeImpl` itself cannot be called directly without reflection.  
**Corpus shape (proxy via subclass + Option B):** 50 subclassed operations with override that records call count. Call `op.Invoke()` (the internal method via InternalsVisibleTo) which triggers the full chain including `InvokeImpl`.  
**BDN filter:** `*DispatcherOperationInvokeImplBenchmark*` (shares class with 2.4 if implemented together).  
**Negative control:** Call `InvokeDelegateCore()` override directly (skip `InvokeImpl`'s SyncContext setup).  
**Feasibility:** `proxy-only` (covered transitively by entry 2.4 benchmark; not worth a separate class).  
**Option verdict:** Option B transitively via entry 2.4 — `InvokeImpl` is exercised whenever `op.Invoke()` runs. No standalone benchmark needed; mark as covered-by-2.4.

---

### 2.6 `DispatcherOperation.Wait()` (cpu: 2.11%)

**Surface:** `op.Wait()` — public method (line 165). Internally it branches: if called from the Dispatcher thread, it pushes a `DispatcherFrame` (needs pump); if called from another thread, it creates a `DispatcherOperationEvent` wrapping a `ManualResetEvent` and calls `WaitOne()`.  
**Dispatcher/STA requirement:** Option A for the cross-thread path. Spin a worker thread that posts `BeginInvoke(action)` and then calls `Wait()` on the returned `DispatcherOperation` from a second thread. This exercises `DispatcherOperationEvent.WaitOne()` which is a `ManualResetEvent.WaitOne`. The Dispatcher must be running on its STA thread to drain the operation and set the event.  
**Corpus shape:** 1 pre-created STA thread running `Dispatcher.Run()` in the background; benchmark body: `var op = dispatcher.BeginInvoke(action); op.Wait()`. 50 distinct no-op actions. High CV risk due to cross-thread coordination.  
**BDN filter:** `*DispatcherOperationWaitBenchmark*`  
**Negative control:** `manualResetEvent.WaitOne()` on a pre-signaled event — represents the OS kernel wait cost without the Dispatcher queue overhead.  
**Feasibility:** `needs-pump` (the `Wait()` cross-thread path requires the pump to execute the operation and signal the event).  
**Option verdict:** Option C — the method is structurally a cross-thread wait that cannot complete without a running pump. The interesting cost (kernel wait, event allocation) is below the noise floor relative to the pump overhead.

---

### 2.7 `DispatcherOperation+DispatcherOperationEvent.WaitOne()` (cpu: 2.11%)

**Surface:** This is a private nested class inside `DispatcherOperation` (line 609). `WaitOne()` calls `ManualResetEvent.WaitOne(_timeout, false)` and then cleans up event handlers under a lock. Accessible only via reflection or by triggering it through `DispatcherOperation.Wait()` from a non-Dispatcher thread.  
**Dispatcher/STA requirement:** Option C — same analysis as 2.6. The `WaitOne()` call itself costs almost nothing (it blocks on a pre-signaled MRE); what the profiler sees is wall-clock time waiting for the Dispatcher to service the operation. Not a CPU hotspot that can be isolated.  
**Corpus shape:** Same as 2.6 (covered transitively).  
**BDN filter:** Covered by `*DispatcherOperationWaitBenchmark*` if 2.6 is attempted.  
**Negative control:** N/A.  
**Feasibility:** `needs-pump`  
**Option verdict:** Option C — this is the same hotspot as 2.6 (the profiler splits `Wait` and `WaitOne` because they appear as two frames in the stack). A single benchmark for 2.6 covers both.

---

### 2.8 `DispatcherOperation.Wait(TimeSpan timeout)` (cpu: 2.11%)

**Surface:** `op.Wait(TimeSpan.FromMilliseconds(-1))` — the overload that does the real work (the no-arg `Wait()` delegates to this). Same analysis as 2.6.  
**Dispatcher/STA requirement:** Option C — identical to 2.6.  
**Corpus shape:** Same as 2.6.  
**BDN filter:** Covered by `*DispatcherOperationWaitBenchmark*` if 2.6 is attempted.  
**Negative control:** N/A.  
**Feasibility:** `needs-pump`  
**Option verdict:** Option C — deduplicated with entry 2.6.

---

### 2.9 `Dispatcher.ProcessQueue()` (cpu: 2.09%)

**Surface:** `ProcessQueue()` is `private` (line 1972). It dequeues one operation from `_queue`, checks `IsInputPending()` (a Win32 call), then calls `op.Invoke()`. Cannot be called without reflection, and `IsInputPending()` requires a real HWND message queue.  
**Dispatcher/STA requirement:** Option C — `IsInputPending()` calls `UnsafeNativeMethods.MsgWaitForMultipleObjectsEx` which requires an STA thread with a real Win32 message queue (HWND). Simulating this without a running pump risks incorrect results.  
**Corpus shape (hypothetical Option A):** Full STA pump + pre-filled queue; trigger via a `DispatcherTimer` callback. Very high setup complexity.  
**BDN filter:** `*DispatcherProcessQueueBenchmark*` (if ever attempted).  
**Negative control:** N/A (skipped).  
**Feasibility:** `needs-pump`  
**Option verdict:** Option C — `private` + Win32 dependency. The interesting sub-operation (dispatching an operation from queue) is covered by entries 2.4 and 2.5.

---

### 2.10 `Dispatcher.PushFrameImpl(DispatcherFrame)` (cpu: 2.2%)

**Surface:** `PushFrameImpl` is `private` (line 2046). Its body is the Win32 `GetMessage` + `TranslateMessage` + `DispatchMessage` loop — this IS the message pump. There is no way to benchmark it without a running HWND.  
**Dispatcher/STA requirement:** Option C — this method IS the pump. Calling it without a real Win32 message queue (or with `frame.Continue = false` immediately) would produce a degenerate constant-time measurement of zero iterations.  
**Corpus shape:** N/A.  
**BDN filter:** N/A.  
**Negative control:** N/A.  
**Feasibility:** `skipped` (the method is the message loop itself; there is no meaningful proxy).  
**Option verdict:** Option C / skip — benchmarking the message pump requires running an actual WPF application loop. This is outside BDN's scope entirely.

---

## Section 3 — Summary Table

| Entry | Method (short) | Feasibility | Bench class | Option |
|-------|---------------|-------------|-------------|--------|
| 2.1 | `Dispatcher.Invoke(Action)` | `runnable` | `DispatcherInvokeActionBenchmark` | A |
| 2.2 | `Dispatcher.Invoke(Action, Priority, CT, TimeSpan)` | `runnable` | `DispatcherInvokeActionBenchmark` | A |
| 2.3 | `Dispatcher.InvokeImpl` | `needs-pump` | — | C |
| 2.4 | `DispatcherOperation.Invoke()` | `proxy-only` | `DispatcherOperationInvokeBenchmark` | B |
| 2.5 | `DispatcherOperation.InvokeImpl()` | `proxy-only` | covered by 2.4 | B |
| 2.6 | `DispatcherOperation.Wait()` | `needs-pump` | — | C |
| 2.7 | `DispatcherOperation+DispatcherOperationEvent.WaitOne()` | `needs-pump` | covered by 2.6 | C |
| 2.8 | `DispatcherOperation.Wait(TimeSpan)` | `needs-pump` | covered by 2.6 | C |
| 2.9 | `Dispatcher.ProcessQueue()` | `needs-pump` | — | C |
| 2.10 | `Dispatcher.PushFrameImpl` | `skipped` | — | C |

**Runnable: 2 (entries 2.1, 2.2 — same benchmark class, fast path)**  
**proxy-only: 2 (entries 2.4, 2.5 — require `InternalsVisibleTo` patch)**  
**needs-pump: 5 (entries 2.3, 2.6, 2.7, 2.8, 2.9)**  
**skipped: 1 (entry 2.10 — PushFrameImpl IS the message loop)**

---

## Section 4 — Note on ExceptionWrapper

`ExceptionWrapper.InternalRealCall` appears in the `misc` cluster (not `dispatcher`), but is mentioned in the design doc as a stretch-goal. For completeness: **`InternalRealCall` is `private`** (line 37 of ExceptionWrapper.cs). However, `ExceptionWrapper.TryCatchWhen` is `public` and calls `InternalRealCall` directly. The `ExceptionWrapper` instance is reachable as the static `Dispatcher._exceptionWrapper` field. Via reflection: `_exceptionWrapper.TryCatchWhen(source, callback, args, 0, null)` exercises `InternalRealCall` with a no-exception path — this is `runnable` without a pump. The Implementer for the `misc` cluster should note that `TryCatchWhen` is the correct surface (not `InternalRealCall` directly), and the interesting fast paths are the `Action` (numArgs=0) and `DispatcherOperationCallback` (numArgs=1) branches — the `DynamicInvoke` fallback is the slow path worth measuring as a negative control.

---

## Section 5 — Surprising Source Finding

The most surprising finding: `Dispatcher.Invoke(Action)` at `Send` priority on the Dispatcher's own thread takes a **completely different code path** that bypasses `InvokeImpl`, the operation queue, and all of `DispatcherOperation.Invoke`/`Wait`. It just swaps `SynchronizationContext` and calls the callback directly (lines 577-608). The profiler attributes ~2.09% CPU to this entry across all scenarios — that cost is entirely SyncContext swap overhead, not queue/wait overhead. This means entries 2.1+2.2 are benchmarkable in isolation and will produce **low CV** results because there is no Win32 or cross-thread synchronization involved.
