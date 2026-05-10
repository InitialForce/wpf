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
