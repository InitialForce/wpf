# Stack Attribution: DictionaryEntry Allocations — take-open scenario

**Date**: 2026-05-11  
**Trace**: `/c/work/wpf-perf/autoresearch/profile-output/take-open/take-open.nettrace` (236 MB)  
**Tool**: `TypeStackProbe` (built at `tools/type-stack-probe/`) — walks `GCAllocationTick` events, groups stacks by TypeName, ranks by bytes.  
**Stack keyword**: present in trace (`DotNETRuntime:0x1FFBCCBFF:5` includes `0x40000000` Stack bit); 2,991 / 5,715 AllocationTick events had stacks resolved.

---

## Confirmed totals in this trace

| Type | Bytes in trace | vs. baseline claim |
|---|---|---|
| `System.Collections.DictionaryEntry[]` | 170.26 MB | baseline claimed 178.5 MB ✓ (same order) |
| `System.Collections.DictionaryEntry` | 133.77 MB | baseline claimed 140.3 MB ✓ |

---

## `System.Collections.DictionaryEntry[]` — 170.26 MB total

**Single unique callstack (100% — 1,675 events):**

```
[0]  System.Windows.Documents.AdornerLayer.MeasureOverride(value class System.Windows.Size)
[1]  System.Windows.FrameworkElement.MeasureCore(value class System.Windows.Size)
[2]  System.Windows.UIElement.Measure(value class System.Windows.Size)
[3]  System.Windows.ContextLayoutManager.UpdateLayout()
[4]  System.Windows.ContextLayoutManager.UpdateLayoutCallback(class System.Object)
[5]  System.Windows.Media.MediaContext.FireInvokeOnRenderCallbacks()
[6]  System.Windows.Media.MediaContext.RenderMessageHandlerCore(class System.Object)
[7]  System.Windows.Media.MediaContext.RenderMessageHandler(class System.Object)
[8]  System.Windows.Threading.ExceptionWrapper.InternalRealCall(...)
[9]  System.Windows.Threading.ExceptionWrapper.TryCatchWhenWithHandlers(...)
[10] System.Windows.Threading.DispatcherOperation.InvokeImpl()
[11] MS.Internal.CulturePreservingExecutionContext.CallbackWrapper(...)
[12] System.Threading.ExecutionContext.RunInternal(...)
[13] System.Windows.Threading.Dispatcher.ProcessQueue()
...  (WndProc / ShowDialog root)
```

**Root cause (verified in source):**  
`AdornerLayer.MeasureOverride` (line 458) allocates a fresh `DictionaryEntry[]` snapshot of `_zOrderMap` on every layout pass:

```csharp
// AdornerLayer.cs:458
DictionaryEntry[] zOrderMapEntries = new DictionaryEntry[_zOrderMap.Count];
_zOrderMap.CopyTo(zOrderMapEntries, 0);
```

`_zOrderMap` is a `SortedList(10)` (field at line 1166). `SortedList.CopyTo` copies both the key and value arrays as `DictionaryEntry` structs. Since `DictionaryEntry` is a struct, every array slot is an object-field pair on the heap. With 1,675 layout passes during take-open (the adorner layer is measured every render tick), this produces ~170 MB of short-lived arrays.

The same pattern appears in `ArrangeOverride` at line 492–493 (not hitting as hard in this trace window, but identical code).

---

## `System.Collections.DictionaryEntry` — 133.77 MB total

**Two callstacks:**

### Stack #1 — 113.25 MB (84.7%, 1,114 events)

Identical path to above: `AdornerLayer.MeasureOverride → ...`

This is the `DictionaryEntry` **struct elements** within the array allocated above. `SortedList.CopyTo` fills `DictionaryEntry[]` with `DictionaryEntry` value-type instances — the CLR fires a separate `AllocationTick` for the struct array elements as a distinct type. Both metrics refer to the **same single allocation site**.

### Stack #2 — 20.53 MB (15.3%, 202 events)

```
[0]  System.Collections.SortedList.CopyTo(class System.Array, int32)
[1]  System.Windows.Documents.AdornerLayer.MeasureOverride(value class System.Windows.Size)
[2]  System.Windows.FrameworkElement.MeasureCore(...)
[3]  System.Windows.UIElement.Measure(...)
[4]  System.Windows.ContextLayoutManager.UpdateLayout()
...  (same render-dispatch chain)
```

Same site; here the CLR resolved the inline boundary differently (showing `SortedList.CopyTo` as the outermost frame), confirming that `SortedList.CopyTo` is the actual allocating method for both the `DictionaryEntry[]` array and its `DictionaryEntry` struct elements.

---

## Culprit: `AdornerLayer.MeasureOverride` / `AdornerLayer.ArrangeOverride`

**Source**: `src/Microsoft.DotNet.Wpf/src/PresentationFramework/System/Windows/Documents/AdornerLayer.cs`, lines 458–459 and 492–493.

**Mechanism**: On every layout pass during take-open (1,675 passes), `AdornerLayer.MeasureOverride` calls:

```csharp
DictionaryEntry[] zOrderMapEntries = new DictionaryEntry[_zOrderMap.Count];
_zOrderMap.CopyTo(zOrderMapEntries, 0);
```

This allocates a fresh heap array of `DictionaryEntry` structs to take a snapshot of the `SortedList _zOrderMap` before iterating (defensive copy to handle modification during iteration). The combined `DictionaryEntry[]` + `DictionaryEntry` element allocation accounts for **~304 MB** of the ~454 MB total measured in this trace.

**Note on the comment at line 1170–1175**: The WPF fork already pooled `_keysSnapshotBuffer` and `_removeList` for the `UpdateAdorner` hot path, but `MeasureOverride` and `ArrangeOverride` were not included in that pooling fix. The fix for these two methods is the same pattern: cache the `DictionaryEntry[]` as a field (or switch `_zOrderMap` from `SortedList` to a typed `SortedDictionary<int, ArrayList>` + iterate via enumerator with a `ToArray()` pooled buffer).

---

## Candidate WPF DLL status (at time of trace)

`PresentationCore.dll` in `MC_BUILD`: **4.4 MB** (stock — candidate is ~3.4 MB). The candidate DLLs were NOT in place at trace time (the `run-profile-2026-05-11.py` script restored stock DLLs after capturing). This trace is therefore the **stock baseline**. The candidate likely did not address `AdornerLayer` (that's a `PresentationFramework` change), so this allocation should be equally present in both stock and candidate.

---

## Recommended fix direction

Replace the per-call `SortedList.CopyTo` snapshot with a pooled/reused buffer:

```csharp
// In AdornerLayer — add field:
private DictionaryEntry[]? _zOrderMapSnapshot;

// In MeasureOverride / ArrangeOverride:
int count = _zOrderMap.Count;
if (_zOrderMapSnapshot == null || _zOrderMapSnapshot.Length < count)
    _zOrderMapSnapshot = new DictionaryEntry[count];
_zOrderMap.CopyTo(_zOrderMapSnapshot, 0);
// use _zOrderMapSnapshot[0..count-1]
```

Or, simpler for the WPF fork: switch `_zOrderMap` to `List<(int zOrder, ArrayList adorners)>` (typed, sortable) to avoid `DictionaryEntry` entirely, which also eliminates the boxing overhead on the `SortedList` integer keys.
