# Post-T4 Baseline and Stack Attribution

Generated: 2026-05-11  
Trace source: `run-profile-2026-05-11.py` run at 19:13–19:41 (previous agent session)  
T4 commit: `e56671084` — pool `AdornerLayer._zOrderMap` value snapshot  
Candidate DLLs: `wpf-perf` HEAD build (PresentationCore + PresentationFramework + WindowsBase)  
Stock DLL restoration: **CLEAN** (confirmed in `/tmp/run-profile.log`)

---

## 1. Post-T4 Baseline Numbers

### take-open
| Metric | Value |
|--------|-------|
| totalAllocBytes | 284.8 MB |
| renderPassCount | 33,516 |
| renderFrameP95Ms | 9.926 ms |
| renderFrameP99Ms | 17.029 ms |
| GC count | 18 |
| GC total pause | 8,080 ms |
| GC max pause | 604 ms |

**Top-10 allocators (stack rollup, take-open.alloc.json):**

| MB | Stack frame |
|----|-------------|
| 151.8 | `DispatcherOperation.InvokeImpl()` |
| 151.5 | `CulturePreservingExecutionContext.CallbackWrapper()` |
| 151.4 | `DispatcherOperation.Invoke()` |
| 151.3 | `ExceptionWrapper.InternalRealCall()` |
| 151.2 | `Dispatcher.ProcessQueue()` |
| 151.2 | `ExceptionWrapper.TryCatchWhenWithHandlers()` |
| 150.9 | `Dispatcher.PushFrameImpl()` |
| 150.9 | `Dispatcher.PushFrame()` |
| 150.9 | `Window.ShowHelper()` |
| 150.8 | `Window.Show()` |

**Top-10 allocated types (from TypeStackProbe):**

| MB | Type |
|----|------|
| 27.86 | `System.String` |
| 10.47 | `FourElementAsyncLocalValueMap` |
| 9.45 | `ForceSample` |
| 9.10 | `System.Double[]` |
| 8.11 | `ForceSampleWithLayout[]` |
| 7.22 | `System.Windows.Threading.DispatcherOperation` |
| 6.61 | `System.String[]` |
| 6.30 | `System.Threading.ExecutionContext` |
| 6.00 | `ActivityInfo` |
| 5.90 | `System.Boolean[]` |

### startup
| Metric | Value |
|--------|-------|
| totalAllocBytes | 293.4 MB |
| renderPassCount | 758 |
| renderFrameP95Ms | 74.699 ms |
| renderFrameP99Ms | 113.354 ms |

### playback (INVALID — idle capture)
| Metric | Value |
|--------|-------|
| totalAllocBytes | 508.3 MB *(inflated by idle polling)* |
| renderPassCount | **876** ← idle window, NOT real playback |
| renderFrameP95Ms | 31.63 ms |
| renderFrameP99Ms | 32.7 ms |

> **Playback capture is invalid.** 876 render passes vs 18,169 in baseline — same idle-capture bug as the previous agent reported. Playback numbers cannot be used for comparison this round.

---

## 2. Apples-to-Apples vs Pre-T4

### Pre-T4 baselines

| Scenario | Source | totalAllocBytes | renderPassCount |
|----------|--------|-----------------|-----------------|
| take-open | `take-open-pre-t4/analysis.json` | 587.1 MB | 32,484 |
| playback | `playback-pre-t4/analysis.json` | 234.4 MB | 18,169 |
| startup | `startup-pre-t4/analysis.json` | 293.7 MB | 707 |

*Note: `take-open/analysis.json` and `take-open-pre-t4/analysis.json` reference the same nettrace path and contain identical pre-T4 values (587.1 MB / 32,484 renders). The pre-T4 subfolders are snapshot copies.*

### Post-T4 (valid scenarios only)

| Scenario | totalAllocBytes | renderPassCount | P95 | P99 |
|----------|-----------------|-----------------|-----|-----|
| take-open | 284.8 MB | 33,516 | 9.926 ms | 17.029 ms |
| startup | 293.4 MB | 758 | 74.699 ms | 113.354 ms |
| playback | INVALID | 876 | — | — |

### Delta

| Scenario | Pre-T4 alloc | Post-T4 alloc | Delta (MB) | Delta (%) |
|----------|--------------|---------------|------------|-----------|
| take-open | 587.1 MB | 284.8 MB | -302.3 MB | **-51.5%** |
| startup | 293.7 MB | 293.4 MB | -0.3 MB | -0.1% (flat) |
| playback | 234.4 MB | — | N/A (invalid) | — |

T4 hit is **entirely in take-open**: -302 MB (-51.5%). Startup is unaffected (expected — T4 targets AdornerLayer layout, not startup path). Playback needs a clean re-capture.

*Playback pre-bigwins reference (`playback-post-bigwins/analysis.json`): 234.4 MB / 18,169 renders / P95=1.723 ms / P99=1.955 ms — same as `playback-pre-t4/analysis.json`. Both reference the same nettrace.*

---

## 3. T5/T6/T7 Stack Attribution

Source: `TypeStackProbe.exe` on `take-open.nettrace` (post-T4 trace, 284.8 MB total).

### FourElementAsyncLocalValueMap — 10.47 MB total (4 unique stacks)

**Stack #1 — 5.59 MB (53%)**
```
AsyncLocalValueMap+FourElementAsyncLocalValueMap.Set(...)
ActivityTracker.OnStart(...)
EventSource.WriteEventWithRelatedActivityIdCore(...)
EventSource.WriteEvent(int32, string)
WpfPerfHarness.OnDispatcherOperationStarted(...)   ← harness instrumentation
Dispatcher.ProcessQueue()
HwndWrapper.WndProc(...)
ExceptionWrapper.InternalRealCall(...)
ExceptionWrapper.TryCatchWhenWithHandlers(...)
HwndSubclass.SubclassWndProc(...)
```

**Stack #2 — 4.57 MB (44%)**
```
AsyncLocalValueMap+FourElementAsyncLocalValueMap.Set(...)
ActivityTracker.OnStop(...)
ActivityTracker.OnStart(...)
EventSource.WriteEventWithRelatedActivityIdCore(...)
EventSource.WriteEvent(int32, string)
WpfPerfHarness.OnDispatcherOperationStarted(...)   ← harness instrumentation
Dispatcher.ProcessQueue()
HwndWrapper.WndProc(...)
ExceptionWrapper.InternalRealCall(...)
ExceptionWrapper.TryCatchWhenWithHandlers(...)
```

> **Finding:** Both stacks root in the WpfPerfHarness `OnDispatcherOperationStarted` event handler, which fires an ETW `WriteEvent`. This triggers `ActivityTracker.OnStart/OnStop`, which allocates `FourElementAsyncLocalValueMap` and `ActivityInfo` as part of ETW activity correlation. These are **harness-induced allocations** — they will not appear in production.

### ActivityInfo — 6.00 MB total (1 unique stack)

**Stack #1 — 6.00 MB (100%)**
```
ActivityTracker.OnStart(...)
EventSource.WriteEventWithRelatedActivityIdCore(...)
EventSource.WriteEvent(int32, string)
WpfPerfHarness.OnDispatcherOperationStarted(...)   ← harness instrumentation
Dispatcher.ProcessQueue()
HwndWrapper.WndProc(...)
ExceptionWrapper.InternalRealCall(...)
ExceptionWrapper.TryCatchWhenWithHandlers(...)
HwndSubclass.SubclassWndProc(...)
```

> **Finding:** Same root cause as `FourElementAsyncLocalValueMap` — harness-only ETW activity tracking overhead.

### System.Threading.ExecutionContext — 6.30 MB total (3 unique stacks)

**Stack #1 — 3.56 MB (56%)**
```
ActivityTracker.OnStop(...)
ActivityTracker.OnStart(...)
EventSource.WriteEventWithRelatedActivityIdCore(...)
EventSource.WriteEvent(int32, string)
WpfPerfHarness.OnDispatcherOperationStarted(...)   ← harness instrumentation
Dispatcher.ProcessQueue()
HwndWrapper.WndProc(...)
ExceptionWrapper.InternalRealCall(...)
ExceptionWrapper.TryCatchWhenWithHandlers(...)
HwndSubclass.SubclassWndProc(...)
```

**Stack #2 — 2.64 MB (42%)**
```
ActivityTracker.OnStart(...)
EventSource.WriteEventWithRelatedActivityIdCore(...)
EventSource.WriteEvent(int32, string)
WpfPerfHarness.OnDispatcherOperationStarted(...)   ← harness instrumentation
Dispatcher.ProcessQueue()
HwndWrapper.WndProc(...)
ExceptionWrapper.InternalRealCall(...)
ExceptionWrapper.TryCatchWhenWithHandlers(...)
HwndSubclass.SubclassWndProc(...)
```

> **Finding:** Same harness ETW path. `ExecutionContext` is allocated as part of `AsyncLocal` propagation when the activity context is captured/restored per-dispatch-operation.

### System.Windows.Threading.DispatcherOperation — 7.22 MB total (8 unique stacks)

**Stack #1 — 3.25 MB (45%)**
```
Dispatcher.LegacyBeginInvokeImpl(...)
ContextLayoutManager.UpdateLayout()
ContextLayoutManager.UpdateLayoutCallback(...)
MediaContext.FireInvokeOnRenderCallbacks()
MediaContext.RenderMessageHandlerCore(...)
MediaContext.RenderMessageHandler(...)
ExceptionWrapper.InternalRealCall(...)
ExceptionWrapper.TryCatchWhenWithHandlers(...)
DispatcherOperation.InvokeImpl()
CulturePreservingExecutionContext.CallbackWrapper(...)
```

**Stack #2 — 2.75 MB (38%)**
```
Dispatcher.LegacyBeginInvokeImpl(...)
MediaContext.PostRender()
ContextLayoutManager.UpdateLayoutBackground(...)
ExceptionWrapper.InternalRealCall(...)
ExceptionWrapper.TryCatchWhenWithHandlers(...)
DispatcherOperation.InvokeImpl()
CulturePreservingExecutionContext.CallbackWrapper(...)
ExecutionContext.RunInternal(...)
DispatcherOperation.Invoke()
Dispatcher.ProcessQueue()
```

> **Finding:** `DispatcherOperation` allocations come from `Dispatcher.LegacyBeginInvokeImpl` called during layout (`UpdateLayout` / `UpdateLayoutBackground`) and render (`PostRender`). This is the WPF layout-driven re-queue pattern — every layout pass that schedules a background update allocates a new `DispatcherOperation`. This is a real production allocation, NOT harness overhead.

---

## 4. Suggested Next Culprits (T5/T6/T7)

**FourElementAsyncLocalValueMap + ActivityInfo + ExecutionContext** (combined ~22.8 MB in take-open):  
All three are driven entirely by `WpfPerfHarness.OnDispatcherOperationStarted` triggering ETW activity tracking — they are **measurement artifacts**. Suppressing or batching the harness ETW writes would eliminate them from the profile; they do not warrant a production optimization.

**System.Windows.Threading.DispatcherOperation** (7.22 MB in take-open):  
Allocated by `LegacyBeginInvokeImpl` during the layout/render cycle (`UpdateLayout → FireInvokeOnRenderCallbacks`, `PostRender → UpdateLayoutBackground`). The next optimization target should be **pooling or caching `DispatcherOperation` instances** for the high-frequency layout background scheduling path — or switching `LegacyBeginInvokeImpl` callers to a non-allocating `BeginInvoke` that reuses existing operations. This is the WPF render-loop's "schedule next layout pass" pattern and fires continuously during take-open's 33k render passes.

---

## File Index

| File | Description |
|------|-------------|
| `profile-output/take-open/analysis-post-t4.json` | Post-T4 take-open metrics (284.8 MB, 33516 renders) |
| `profile-output/take-open/analysis.json` | Pre-T4 take-open metrics (587.1 MB, 32484 renders) — same nettrace ref |
| `profile-output/take-open-pre-t4/analysis.json` | Pre-T4 snapshot copy (identical values) |
| `profile-output/playback/analysis-post-t4.json` | Post-T4 playback — INVALID (876 renders, idle capture) |
| `profile-output/playback-pre-t4/analysis.json` | Pre-T4 playback baseline (234.4 MB, 18169 renders) |
| `profile-output/startup/analysis-post-t4.json` | Post-T4 startup (293.4 MB, flat vs pre-T4) |
| `profile-output/take-open/type-stack-report.md` | TypeStackProbe output for all 4 target types |
| `/tmp/run-profile.log` | Full run log — confirms "Stock DLL restoration: CLEAN" |
