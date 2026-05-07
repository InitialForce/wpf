# Design Notes — Cluster: misc

**Entries:** 5 (ExceptionWrapper.InternalRealCall, ExceptionWrapper.TryCatchWhen,
CulturePreservingExecutionContext.CallbackWrapper, HwndSubclass.SubclassWndProc,
HwndWrapper.WndProc)

**ExceptionWrapper placement:** ExceptionWrapper stays in **misc**. It is a pure managed
dispatch wrapper (no Dispatcher pump required). The dispatcher Designer was told to defer
it here.

---

## Section 1 — Cluster Summary

The misc cluster groups two purely-managed dispatch wrappers (ExceptionWrapper,
CulturePreservingExecutionContext) and two Win32 WndProc wrappers (HwndSubclass,
HwndWrapper). The managed wrappers require no Dispatcher or STA thread; they are the
easiest benchmarkable targets in the entire swarm. The Win32 wrappers require a live
HWND created in GlobalSetup on an STA thread; they are runnable but need care around
message-loop interaction (a `HwndWrapper` with a no-op hook suffices; no
`Dispatcher.Run` frame loop is required because `HwndSubclass.SubclassWndProc` only
calls `Dispatcher.FromThread` — which returns null if no Dispatcher exists — and then
calls the hook directly). Overall feasibility: **4 runnable, 1 proxy-only**.

---

## Section 2 — Per-Entry Analysis

### 2.1 ExceptionWrapper.InternalRealCall / TryCatchWhen

**Profile entries:**
- `WindowsBase!System.Windows.Threading.ExceptionWrapper.InternalRealCall(class System.Delegate,class System.Object,int32)` (2.09% CPU)
- `WindowsBase!System.Windows.Threading.ExceptionWrapper.TryCatchWhen(class System.Object,class System.Delegate,class System.Object,int32,class System.Delegate)` (2.09% CPU)

**Source:** `WindowsBase/MS/Internal/Threading/ExceptionWrapper.cs`

**Surface:**
- `InternalRealCall` is **private**; it is only reachable via the public `TryCatchWhen` method or via reflection.
- Preferred benchmark surface: call `exceptionWrapper.TryCatchWhen(source, delegate, args, numArgs, catchHandler)` directly (class is `internal`, access via `[assembly: InternalsVisibleTo]` or reflection; use reflection to avoid source modification).
- Both entries are addressed by a single benchmark class exercising `TryCatchWhen`, which calls `InternalRealCall` on every iteration.

**Dispatcher/STA requirement:** None. `ExceptionWrapper` has no thread affinity.
**Option A** is not needed. Call directly on a background thread.

**Corpus shape:**
- N=64 pre-allocated `Action` delegates (seeded via array index, varying lambda captures).
- numArgs=0 fast path (exercises `Action` branch of `InternalRealCall`).
- Second `[Benchmark]` method: numArgs=1 with `DispatcherOperationCallback` (exercises the 1-arg fast path).
- Negative control: call `delegate.DynamicInvoke()` directly (bypasses `InternalRealCall` type-check fast path).

**BDN filter:** `*ExceptionWrapperBenchmark*`

**Feasibility:** `runnable`

**Note on access:** `ExceptionWrapper` is `internal`. Options: (a) add
`[assembly: InternalsVisibleTo("Microbenchmarks")]` to WindowsBase (source change), or
(b) instantiate via `Activator.CreateInstance(typeof(...).Assembly.GetType(...))` and
invoke via cached `MethodInfo`/`Func<>` delegate in `[GlobalSetup]`. Option (b) adds
~3 ns overhead per call but avoids touching the WPF source. Recommend option (b) unless
the Implementer determines reflection overhead swamps the signal.

---

### 2.2 CulturePreservingExecutionContext.CallbackWrapper

**Profile entry:**
- `WindowsBase!MS.Internal.CulturePreservingExecutionContext.CallbackWrapper(class System.Object)` (2.09% CPU)

**Source:** `Shared/MS/Internal/CulturePreservingExecutionContext.cs`

**Hot path:**
```
CallbackWrapper(object obj):
  1. Cast obj to CultureAndContextManager
  2. WriteCultureInfosToCurrentThread()   — sets Thread.CurrentCulture + CurrentUICulture
  3. callback.Invoke(state)               — the user callback
  4. ReadCultureInfosFromCurrentThread()  — reads back modified culture
```

`CallbackWrapper` is a **private static** method; it is invoked only as a
`ContextCallback` passed to `ExecutionContext.Run`. The public entry point is
`CulturePreservingExecutionContext.Run(cpec, callback, state)`.

**Surface:**
- Call `CulturePreservingExecutionContext.Run(ctx, callback, state)` on each iteration.
- `ctx` captured once in `[GlobalSetup]` via `CulturePreservingExecutionContext.Capture()`.
- However `ctx` is disposed after `DispatcherOperation.Invoke()` returns in production;
  for benchmarking, recapture a fresh context per iteration using `Capture()` to avoid
  calling `Run` on a disposed context. This means the benchmark measures
  `Capture() + Run()` together — acceptable because both are on the hot path.
- `callback` is a no-op `ContextCallback` (avoids confounding work inside the callback).
- Negative control: call `ExecutionContext.Run(ec, callback, state)` directly (bypasses
  culture-preservation overhead; reveals how much overhead CPEC adds over raw EC.Run).

**Dispatcher/STA requirement:** None. `ExecutionContext.Run` works on any thread.

**Corpus shape:**
- Single callback per iteration (no meaningful variation in callback identity).
- 64 distinct pre-captured culture objects are not needed — the culture round-trip cost
  is constant. Use a single context, single callback. Comment in code explaining why
  corpus size = 1 is correct here.

**BDN filter:** `*CulturePreservingBenchmark*`

**Feasibility:** `runnable`

**Note on access:** `CulturePreservingExecutionContext` is `internal` in the
`MS.Internal` namespace (Shared project, compiled into WindowsBase). Same
reflection-or-InternalsVisibleTo choice as ExceptionWrapper. Because `Capture()` and
`Run()` are both `public static` methods on an `internal` class, they are accessible
once you obtain the `Type` via reflection. Cache the `MethodInfo` objects in
`[GlobalSetup]`.

---

### 2.3 HwndSubclass.SubclassWndProc

**Profile entry:**
- `WindowsBase!MS.Win32.HwndSubclass.SubclassWndProc(int,int32,int,int)` (2.09% CPU)

**Source:** `Shared/MS/Win32/HwndSubclass.cs`

**Hot path:**
```
SubclassWndProc(hwnd, msg, wParam, lParam):
  1. Check _bond state
  2. Dispatcher.FromThread(Thread.CurrentThread) — returns null if no dispatcher
  3. If dispatcher != null: dispatcher.Invoke(Send priority, DispatcherOperationCallback, param)
  4. Else: falls through to calling the hook via DispatcherCallbackOperation directly
     (this path is NOT taken in production — dispatcher is always present on WPF UI thread)
  5. CallOldWindowProc → UnsafeNativeMethods.CallWindowProc
```

**Critical observation:** In production, `Dispatcher.FromThread` returns a live
dispatcher (the STA UI thread dispatcher), so `dispatcher.Invoke(...)` is called on
every WndProc message. The benchmark must replicate this. This means either:
- (A) Run the benchmark on a real STA thread with a live Dispatcher (no pump needed
  for `Dispatcher.FromThread` to return non-null — the Dispatcher object is created on
  first access for any STA thread, but `Dispatcher.Invoke` with Send priority blocks
  until the operation completes, which requires a pump). This is the `needs-pump` path.
- (B) Benchmark the `SubclassWndProc` code path when `Dispatcher.FromThread` returns
  null (i.e., no dispatcher on the benchmarking thread). This misses the real hot path
  but is still runnable and exercises the HWND-subclassing mechanics.

**Recommendation:** Use option (B) with a clear comment that the dispatcher-invoke
branch is not exercised. Create a `HwndWrapper`-hosted HWND in `[GlobalSetup]`, attach
a subclass to it, then drive it via `UnsafeNativeMethods.SendMessage` from a non-STA
thread. The `SubclassWndProc` will be invoked (via the Win32 WNDPROC chain) on the
message-only window created in GlobalSetup.

**Dispatcher/STA requirement:** Option B — real HWND required; STA thread in
GlobalSetup to create the window. BDN runs the benchmark body on its own thread which
need not be STA (message delivery to a HWND from a non-STA thread is fine for
`SendMessage`; the WndProc runs on the thread that created the window).

**Corpus shape:**
- `[GlobalSetup]` creates a `HwndWrapper` on a dedicated STA thread (spin via
  `Thread(ApartmentState.STA)`, call `HwndWrapper(...)`, signal a `ManualResetEvent`).
- 64 distinct `WM_USER + i` messages posted in `[IterationSetup]` and consumed via
  `SendMessage` in the benchmark loop.
- Negative control: call `UnsafeNativeMethods.CallWindowProc(DefWndProc, hwnd, msg, 0, 0)`
  directly (bypasses managed WndProc chain entirely).

**BDN filter:** `*HwndSubclassBenchmark*`

**Feasibility:** `runnable` — caveat: message delivery to a HWND created on a
different thread requires that thread to be running a message pump or be responsive
to `SendMessage` (which blocks until the WndProc returns on the owning thread).
In GlobalSetup the STA thread must run a minimal message loop (`Application.DoEvents`
equivalent or a custom `PeekMessage`/`DispatchMessage` loop) to process incoming
messages. This is doable but adds setup complexity; mark as HIGH-CARE.

---

### 2.4 HwndWrapper.WndProc

**Profile entry:**
- `WindowsBase!MS.Win32.HwndWrapper.WndProc(int,int32,int,int,bool&)` (2.09% CPU)

**Source:** `Shared/MS/Win32/HwndWrapper.cs`

**Hot path:**
```
WndProc(hwnd, msg, wParam, lParam, ref handled):
  1. Iterate _hooks (WeakReferenceList<HwndWrapperHook>)
  2. Call each hook: hook(hwnd, msg, wParam, lParam, ref handled)
  3. Check handled; break if true
  4. Special-case WM_NCDESTROY (calls Dispose)
  5. CheckForCreateWindowFailure
```

`WndProc` is **private**; it is registered as the WndProc of the HwndWrapper via
`HwndSubclass`. It is invoked whenever the HWND receives a message.

**Surface:** Same setup as 2.3. Create a `HwndWrapper` in `[GlobalSetup]` with one
registered `HwndWrapperHook` (no-op hook). Drive via `SendMessage` from the benchmark
loop.

**Dispatcher/STA requirement:** Same as HwndSubclass (STA thread + minimal pump in
GlobalSetup thread). Option B applies here too: no Dispatcher.Run needed, just a
thread running a `PeekMessage`/`DispatchMessage` loop.

**Corpus shape:** Same HWND as HwndSubclass benchmark (share GlobalSetup if both
benchmarks live in the same class). 64 WM_USER+i messages. Hook count variation:
benchmark with 1 hook (fast path) and 4 hooks (covers the loop body).

**Negative control:** Same window but zero hooks registered — tests just the
`WM_NCDESTROY` and empty-loop overhead.

**BDN filter:** `*HwndWrapperBenchmark*` (or same class as HwndSubclass:
`*HwndWin32Benchmark*` to group them).

**Feasibility:** `runnable` — same HIGH-CARE caveat as HwndSubclass re: owning-thread
message pump.

**Implementer note:** HwndWrapper and HwndSubclass share the same GlobalSetup HWND
and STA host thread. Recommend implementing them in one class `HwndWin32Benchmark` to
avoid spinning two separate STA host threads.

---

## Section 3 — Summary Table

| Entry | Method (short) | BDN filter | Feasibility | STA/HWND | Notes |
|-------|---------------|------------|-------------|----------|-------|
| ExceptionWrapper.InternalRealCall | `TryCatchWhen` (calls InternalRealCall) | `*ExceptionWrapperBenchmark*` | runnable | None | Reflection access; hot path is numArgs=0 Action branch |
| ExceptionWrapper.TryCatchWhen | same class as above | `*ExceptionWrapperBenchmark*` | runnable | None | Same benchmark, second [Benchmark] for numArgs=1 path |
| CulturePreservingExecutionContext.CallbackWrapper | `CPEC.Run(Capture(), noop, null)` | `*CulturePreservingBenchmark*` | runnable | None | Reflection access; neg-control = raw ExecutionContext.Run |
| HwndSubclass.SubclassWndProc | SendMessage to HwndWrapper-hosted HWND | `*HwndWin32Benchmark*` | runnable | STA + pump | Dispatcher=null path; HIGH-CARE GlobalSetup |
| HwndWrapper.WndProc | SendMessage to same HWND | `*HwndWin32Benchmark*` | runnable | STA + pump | Share GlobalSetup with HwndSubclass; test 1-hook and 4-hook |

**Most easily runnable benchmark target:** `ExceptionWrapper.TryCatchWhen` — pure
managed, no STA, no reflection if `InternalsVisibleTo` is added, maps directly to a
profile hot-path that appears in all three scenarios (startup, take-open, playback).
