# WPF App Known Performance Issues

Structural perf pathologies observed in Motion Catalyst (MC), correlated with their
WPF-framework root causes.  Each entry is research-driven; the WPF source tree at
`/c/work/wpf-perf` is the canonical reference for structural causes and fix commits.

---

## ISS-01: Forever-animation drives 17× render-pass amplification through default AdornerLayer

**Status**: `MITIGATED-WPF`
WPF-side closed via commits `5e7df8833` (dirty-bit guard) and `9ddfa26bc`
(empty-map fast-path).  App-side `IsBusy` lifetime issue is OPEN per user direction
— recorded as application-layer follow-up.

**Discovered**: 2026-05-09, take-open scenario profile
(`autoresearch/profile-output/take-open/take-open.nettrace`).  19-second capture
showed 10 817 render passes (~570/sec) vs animation rate of 32 Hz → 17.7× amplification
ratio.

**App-side observation (MC)**: `SpinBusyUserControl` storyboard with
`RepeatBehavior="Forever"` runs perpetually while `IsBusy=true`.  `IsBusy` is bound to
long-running analysis state (waveform/skeleton/...) that stays `true` after the video
preview is rendered.  Result: spinner runs forever, propagating `Freezable.FireChanged`
up the visual tree at 32 Hz.

Relevant files:
- `src/motioncatalyst/Present/MotionCatalyst.Present/UserControls/SpinBusyUserControl.xaml` — line 10: `<Storyboard x:Key="StartSpin" RepeatBehavior="Forever">`
- `src/motioncatalyst/Present/MotionCatalyst.Present/UserControls/ViewPortWindowView.xaml` — line 75–76: `<UserControls:SpinBusyUserControl ... IsBusy="{Binding IsBusy}" />`

**WPF-side root cause**: `AdornerLayer.OnLayoutUpdated`
(`src/Microsoft.DotNet.Wpf/src/PresentationFramework/System/Windows/Documents/AdornerLayer.cs`,
line 608) ran a per-pass walk → `UpdateAdorner` → `TransformToAncestor` →
`InvalidateMeasure` even when no user adorners were attached.  Each `InvalidateMeasure`
synchronously scheduled `BeginInvokeOnRender → PostRender → next render pass`, creating
a self-sustaining loop.  The default `AdornerLayer` subscribes to `LayoutUpdated`
unconditionally at construction time (line 184:
`LayoutUpdated += new EventHandler(OnLayoutUpdated)`) — every WPF window has one.

The combination of the unconditional subscription and the absence of an early-exit
guard meant that every animation tick that dirtied layout (even by a single
`Freezable` property change deep in the visual tree) caused the full adorner walk to
run, which re-dirtied layout, triggering the next render pass indefinitely.

**Mitigations**:

- **WPF-side** (landed):
  - `5e7df8833` — dirty-bit guard: `OnLayoutUpdated` returns early if `_layoutDirty`
    is `false` (AdornerLayer.cs, line 625).  Closes the re-entry loop when no adorner
    geometry has actually changed.
  - `9ddfa26bc` — empty-map fast-path: skips the full walk and clears the stale dirty
    flag when `ElementMap.Count == 0` (AdornerLayer.cs, lines 619–622).  Eliminates
    any overhead on windows that have no user-added adorners, which is the common case
    for MC.

- **App-side** (NOT YET FIXED — recorded as follow-up):
  Split `IsBusy` into `IsVideoLoading` + `IsAnalysisRunning`, drive spinner only from
  `IsVideoLoading`.  Recommended approach (per oracle panel consultation):
  - Use an enum state machine: `Idle → LoadingVideo → Analyzing → Idle`.
  - Per-load operation IDs to avoid stale completion races.
  - `try/finally` guard for zombie-spinner protection.

  **Known-bug history**: prior MC attempts to split `IsBusy` reportedly introduced
  regressions.  Failure modes to guard against: stale completion, zombie spinner,
  premature clear (decode != render), flapping, composite regression on other UI
  consumers of `IsBusy`.

**Detection signal**: `renderPassCount / (animationRenderRate × captureSpanSeconds) > ~3`
indicates amplification.  Healthy ratio ≈ 1 (each animation tick produces ~1 render
pass).  This trace yielded 17.7.  See `autoresearch/profile-amplification-check.py`
for the automated check.

---

## ISS-02: Unfrozen `StreamGeometry` instances rebuild byte stream every frame

**Status**: `MITIGATED-WPF` (partial); `OPEN` app-side.
WPF-side `[ThreadStatic]` pool extended in commit `f3c309145` (T2-A big-win)
caught ~half of the wedge; the rest needs app-side `Freezable.Freeze()` on
geometries that don't change between frames.

**Discovered**: 2026-05-11, deep-dive
(`autoresearch/deep-dive-2026-05-11/T2-dp-storage-churn.md`).

**Symptom**: `MS.Utility.SingleItemList<System.Byte[]>` allocations dominated the
WPF-allocator profile in take-open + playback scenarios — ~70 MB combined across
both scenarios.

**WPF-side root cause**: `ByteStreamGeometryContext` is the write-side of
`StreamGeometry`.  Every `StreamGeometry.Open()` call triggers a chain ending in
`_chunkList.Add(chunk)` on a `FrugalStructList<byte[]>`.  When the underlying
`_listStore` is null (cold start, or after the multi-chunk reset path),
`FrugalStructList.Add` allocates `new SingleItemList<byte[]>()`.

The fork already had a `[ThreadStatic]` pool on the
`StreamGeometryCallbackContext` subclass (used by `Geometry.Parse` and
`StreamGeometry.Open`) that preserved the `SingleItemList` across pool cycles.
But four direct callers of `new ByteStreamGeometryContext()` bypassed the pool
entirely:
- `EllipseGeometry.GetPathGeometryData()` (line 310)
- `LineGeometry.GetPathGeometryData()` (line 244)
- `RectangleGeometry.GetPathGeometryData()` (line 409)
- `PathGeometry.GetPathGeometryData()` (line 957)

These fire on every `Geometry.Bounds`, `GetFlattenedPathGeometry`, `FillContains`,
and hit-test query — i.e. per-mouse-event for any UI with custom shape overlays.

**App-side observation (MC)**: `Shape.OnRender → EnsureRenderedGeometry →
DefiningGeometry` rebuilds a `StreamGeometry` whenever
`ResetRenderedGeometry()` is called by a layout pass.  MC shapes that resize
between frames (waveform overlays, video timeline ticks, force-plate curves)
end up calling `StreamGeometry.Open()` once per shape per render pass.

**Mitigations**:

- **WPF-side** (landed):
  - `f3c309145` — base-class `[ThreadStatic]` pool for
    `ByteStreamGeometryContext` with `AcquireFromPool()`/`ReleaseToPool()` API
    used by the four owner-less callers above.  Catches ~20-40 MB of the
    70 MB combined wedge — the residual ~30-50 MB comes from MC shapes whose
    `_renderedGeometry` is invalidated every frame by `ArrangeOverride` even
    though the underlying geometry data hasn't changed.

- **App-side** (NOT YET FIXED — recorded as follow-up):
  For any `Shape` whose `DefiningGeometry` is stable between frames (most
  overlays, ticks, ruler marks — anything that doesn't move every frame),
  build the `StreamGeometry` once, call `.Freeze()`, cache the frozen
  geometry on the view-model or as a field, and return that cached instance
  from `DefiningGeometry`.

  Frozen `StreamGeometry` instances are serialized to MIL exactly once and
  never trigger `Open()` again.  For shapes that legitimately animate every
  frame (e.g. data-driven waveforms during live playback), the current code
  is fine — the WPF pool now amortizes the per-frame cost.

  **Suspected MC files** (search starting points; not exhaustive):
  - `src/motioncatalyst/Present/MotionCatalyst.Present/Analysis/...` — waveform
    and force-plate overlays.
  - `src/motioncatalyst/Present/MotionCatalyst.Present/Video/...` — timeline
    ticks, video scrubber chrome.

**Detection signal**: in any per-scenario `analysis.json`, look for
`MS.Utility.SingleItemList`1[System.Byte[]]` or
`MS.Utility.SixItemList`1[System.Byte[]]` in the top-30 allocators.  A healthy
ratio is < 1 MB per scenario; > 10 MB signals an unfrozen-geometry pattern.

---

## ISS-03: Per-mouse-event hit-test object churn (now mitigated)

**Status**: `MITIGATED-WPF`.  App side does not need to change anything.

**Discovered**: 2026-05-11, deep-dive
(`autoresearch/deep-dive-2026-05-11/T1-point-allocations.md`).

**Symptom**: `System.Windows.Point` was the #3 raw allocator in take-open +
playback (~71 MB combined), but `Point` is a struct — the heap traffic came
from class wrappers and delegates allocated on every mouse-event hit-test.

**WPF-side root cause**: `UIElement.InputHitTest` allocated four heap objects
per call: `new PointHitTestParameters(pt)`, `new InputHitTestResult()`, and
two `HitTestCallback` delegates.  At 60 Hz cursor movement, this fires
thousands of times per second.  Each `PointHitTestParameters`/`PointHitTestResult`
is itself a `DependencyObject`, so each allocation triggers an
`EffectiveValueEntry[]` array allocation as a side-effect.

**Mitigation** (landed): commit `1f50394c4` adds a `[ThreadStatic]` pool of
`PointHitTestParameters` (mutated via the pre-existing `SetHitPoint(Point)`
internal API), a static shared filter callback, and Acquire/Release pooling
on the `InputHitTestResult` nested class with its bound `HitTestResultCallback`.

**Why this matters for app-side reasoning**: the secondary effect — eliminating
`EffectiveValueEntry[]` regrowth from short-lived DOs — was the actual
dominant win on the take-open + playback scenarios.  Apples-to-apples
measurement (`compare-bigwins.py`, 2026-05-11) showed
`System.Windows.EffectiveValueEntry[]` dropping from 548 MB → <2 MB on
take-open and 308 MB → <0.2 MB on playback.

Going forward, **avoid creating short-lived `DependencyObject` subclasses
on the render path** (per-frame `Pen`, per-frame `Brush`, per-frame
`PointHitTestParameters`-like wrappers).  Each instance triggers a small
chain of `EffectiveValueEntry[]` regrowths.  If the same DP values would be
set every frame, construct once, `Freeze()`, reuse — same pattern as ISS-02.

---

## ISS-04: `AdornerLayer` allocated a fresh `DictionaryEntry[]` every layout pass

**Status**: `MITIGATED-WPF`.  No app-side action required.

**Discovered**: 2026-05-11, post-big-wins residual analysis
(`autoresearch/t4-stack-attribution.md`).  After ISS-01–03 landed,
`System.Collections.DictionaryEntry[]` (~178 MB) and
`System.Collections.DictionaryEntry` (~140 MB) jumped to the top of the
take-open allocator profile — 51% of the trace's `totalAllocBytes`.

**WPF-side root cause**: `AdornerLayer.MeasureOverride` and `ArrangeOverride`
both ran:

```csharp
DictionaryEntry[] zOrderMapEntries = new DictionaryEntry[_zOrderMap.Count];
_zOrderMap.CopyTo(zOrderMapEntries, 0);
```

on every layout pass to take a defensive snapshot of the `SortedList` keyed
z-order map before iterating (the iteration calls out to adorners that
could mutate the list).  In MotionCatalyst this fires ~1675 times during
take-open and allocates a fresh `DictionaryEntry[]` plus its boxed entry
structs each time — single call site, 100% of the wedge per
GC `AllocationTick` stack attribution.

**Mitigation** (landed): commit `e56671084` snapshots the value list
directly into a pooled `object[]` field, matching the existing
`_keysSnapshotBuffer` pool used by `UpdateAdorner`:

```csharp
IList valueList = _zOrderMap.GetValueList();
int count = valueList.Count;
if (_zOrderValuesSnapshotBuffer == null || _zOrderValuesSnapshotBuffer.Length < count)
    _zOrderValuesSnapshotBuffer = new object[Math.Max(count, 8)];
valueList.CopyTo(_zOrderValuesSnapshotBuffer, 0);
```

`SortedList.GetValueList()` returns a cached `IList` (one-time alloc) over
the internal values array and `CopyTo` does a direct `Array.Copy` — zero
`DictionaryEntry` boxing.  Measure and Arrange share the buffer because
they never overlap in a single layout pass.

**Result**: take-open `DictionaryEntry`+`DictionaryEntry[]` 318.8 MB → 0 MB,
`totalAllocBytes` 615.6 MB → 249.5 MB (-59%).  Playback collapsed 245.8 MB
→ 26.7 MB (-89%) as a side-effect of eliminating the per-pass churn that
had been dominating the AllocationTick sampling.

**Detection signal**: `DictionaryEntry[]` ≥ 50 MB in the per-scenario
`analysis.json` top-allocators indicates this pathology has regressed.
Healthy ratio is < 1 MB.

---

## ISS-05: Perf harness's own `EventSource` triggered ActivityTracker churn

**Status**: `MITIGATED-MC` (perf-harness change in InitialForce/ScDesktop,
branch `wpf-perf-harness`, commit `c9662b2bbf3`).  Not a WPF runtime issue.

**Discovered**: 2026-05-11, post-T4 stack attribution
(`autoresearch/post-t4-baseline-and-attribution.md`).  After T4 collapsed
the `DictionaryEntry` wedge, the next ~23 MB residual sat on:
- `FourElementAsyncLocalValueMap` (10.5 MB take-open)
- `ActivityInfo` (6.0 MB)
- `System.Threading.ExecutionContext` (6.3 MB)

100% of these stacks rooted in
`WpfPerfHarness.OnDispatcherOperationStarted → EventSource.WriteEvent →
WriteEventWithRelatedActivityIdCore → ActivityTracker.OnStart/OnStop`.

**Root cause**: the harness's `PerfHarnessEventSource` decorated its
dispatch-op `Start`/`Stop` events with `EventOpcode.Start`/`EventOpcode.Stop`.
That triggers the implicit `ActivityTracker` chain on every event, which
in turn allocates the `AsyncLocalValueMap` + `ActivityInfo` +
`ExecutionContext` trio through AsyncLocal propagation — pure measurement
overhead.

**Mitigation** (landed in MC): add
`ActivityOptions = EventActivityOptions.Disable` to all six
`Start`/`Stop` events.  The events themselves are still emitted with the
opcode semantics intact for PerfView / dotnet-trace consumers; we don't
rely on AsyncLocal-propagated activity IDs because every event carries
its own explicit elapsed-time payload.

**Result**: all three artifact types absent from the top-30 of every
scenario in the post-harness baseline (`autoresearch/post-harness-baseline.md`).

**Lesson for future WPF perf instrumentation**: when adding `EventSource`
events to a hot-path callback, default to `EventActivityOptions.Disable`
unless you specifically want the AsyncLocal-propagated activity ID for
correlation.  The default behavior allocates per-event.

---
