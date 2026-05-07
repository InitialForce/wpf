# DESIGN-NOTES: window-app cluster

## Section 1 — Cluster Summary

Five entries from `PresentationFramework` all share the same fundamental problem: they
either *are* the Win32 message loop (`Application.Run`, `RunInternal`, `RunDispatcher`)
or they block inside one (`Window.ShowDialog`). None can be called in isolation inside
BDN's default thread. The only benchmarkable proxy is a warm-start STA host that already
has a running `Application` + `Dispatcher`, measures per-iteration `Window.Show` →
`Window.Hide` round-trips, and calls `Application.Shutdown` only at teardown. CV for
Win32 show/hide is expected to be 5–10% due to message-pump variance and first-frame
rendering jitter. `Application.RunDispatcher` is also the hottest entry in the
`dispatcher` cluster (`Dispatcher.PushFrameImpl`); the two clusters share the same
hot frame — document the overlap and do NOT duplicate benchmark class logic.

---

## Section 2 — Per-Entry Analysis

### Entry 1: `Application.RunDispatcher(object ignore)`
- **Profile:** 2.20% CPU (hottest in cluster), 2.34% alloc. Appears in both
  `window-app` cluster AND as the call-site parent of `dispatcher`'s
  `Dispatcher.PushFrameImpl`. The body is a 3-line wrapper:
  `_ownDispatcherStarted = true; Dispatcher.Run(); return null;`
- **Surface:** Cannot be called directly — sets `_ownDispatcherStarted` (private bool)
  and then blocks on `Dispatcher.Run()`, which pumps until `Application.Shutdown`.
- **Dispatcher/STA requirement:** Option B — throughput proxy only. Any Show→Hide
  measurement on a warm host already captures this call stack depth. Calling
  `RunDispatcher` independently is impossible without forking `Application` and
  resetting `_ownDispatcherStarted` via reflection.
- **Corpus shape:** N/A for standalone call; shared with Show→Hide proxy (see §Window.Show).
- **BDN filter string:** `*WindowAppBenchmark*`
- **Negative-control method:** Time `Dispatcher.BeginInvoke` enqueue + drain without
  a `Window`, measuring Dispatcher queue cost without window overhead.
- **Feasibility tag:** `proxy-only`
- **Overlap with dispatcher cluster:** `RunDispatcher` calls `Dispatcher.Run()` →
  `Dispatcher.PushFrame()` → `PushFrameImpl()`. The `dispatcher` cluster benchmarks
  `PushFrameImpl` directly via its own STA pump. These are the same hot frame viewed
  from different call-graph depths. The `window-app` implementer must not re-implement
  a PushFrame benchmark — use the Show→Hide proxy only and note the overlap.

---

### Entry 2: `Application.Run(Window)`
- **Profile:** 2.19% CPU. Source: one-liner `VerifyAccess(); return RunInternal(window);`
  — no independent hot path; sample cost is entirely from RunInternal downstream.
- **Surface:** `new Application().Run(window)` — blocks until `Application.Shutdown()`.
  Cannot return mid-session; cannot be iterated.
- **Dispatcher/STA requirement:** Option B (proxy). The "surface" in the benchmark is
  the Show→Hide proxy loop running inside a pre-started Application, not `Run` itself.
- **Corpus shape:** shared warm-host proxy (see §Window.Show entry).
- **BDN filter string:** `*WindowAppBenchmark*`
- **Negative-control method:** `window.UpdateLayout()` call inside the same warm host
  (measures layout-only cost without show/hide Win32 round-trip).
- **Feasibility tag:** `proxy-only`

---

### Entry 3: `Application.RunInternal(Window)`
- **Profile:** 2.19% CPU (tied with Run(Window)). Source: validates arguments, optionally
  enqueues `window.Show()` via `Dispatcher.BeginInvoke(DispatcherPriority.Send)`, calls
  `EnsureHwndSource()`, then synchronously calls `RunDispatcher(null)`. The meaningful
  work pre-pump (window registration, HWND source creation) is startup-once; the 2.19%
  sample cost comes from the blocking pump call, not the preamble.
- **Surface:** internal method; not directly callable. Same proxy as Run(Window).
- **Dispatcher/STA requirement:** Option B (proxy). The measureable sub-cost is
  `EnsureHwndSource()`, which can be measured once per run via reflection if desired, but
  its share of the 2.19% is negligible (startup-once). Primary proxy: Show→Hide loop.
- **Corpus shape:** shared warm-host proxy.
- **BDN filter string:** `*WindowAppBenchmark*`
- **Negative-control method:** same as Application.Run(Window).
- **Feasibility tag:** `proxy-only`

---

### Entry 4: `Application.Run()`
- **Profile:** 2.18% CPU. Source: emits ETW trace event, then `return this.Run(null)`.
  Trivially wraps Run(Window) with null. No additional hot path.
- **Surface:** blocked by same message-loop constraint as Run(Window).
- **Dispatcher/STA requirement:** Option B (proxy).
- **Corpus shape:** shared warm-host proxy.
- **BDN filter string:** `*WindowAppBenchmark*`
- **Negative-control method:** same as Run(Window).
- **Feasibility tag:** `proxy-only`
- **Note:** Entries 2, 3, 4 are pure call-graph ancestors of RunDispatcher. They share
  100% of their sample cost with Entry 1. They do not need independent benchmark
  methods — one `WindowShowHideProxy` benchmark covers all three plus Entry 1.

---

### Entry 5: `Window.ShowDialog()`
- **Profile:** 2.09% CPU. Source: extensive preamble (EnumThreadWindows, EnableThreadWindows,
  GetActiveWindow, owner handle walk) followed by `_showingAsDialog = true; Show()`, then
  blocks in the nested Dispatcher frame until the dialog closes via `DoDialogHide()` →
  `EnableThreadWindows(true)`.
- **Surface:** `window.ShowDialog()` — blocks on an inner dispatcher frame via
  `ComponentDispatcher.PushModal()`. Can complete only when the dialog calls `Close()` or
  `DialogResult` is set.
- **Dispatcher/STA requirement:** Option A (partial) — ShowDialog is callable on an STA
  thread with a running dispatcher *if* a second thread closes the dialog promptly. In a
  warm-host benchmark the pattern is:
  1. On the benchmark STA thread: `Dispatcher.BeginInvoke(() => window.Close())` enqueued
     *before* `ShowDialog()` returns, so the modal pump exits after one tick.
  2. Measure: `ShowDialog()` elapsed per iteration (one modal pump frame).
  Expected CV: 5–10% — Win32 `EnableWindow` / `DisableWindow` over all thread windows
  adds non-deterministic cost; the modal nested-pump itself is a `PushFrame` variant.
- **Corpus shape:** single minimal `Window` (no content); re-use across iterations
  (reset `_dialogResult = null` between iterations via `DialogResult` property or close +
  re-create). Minimum 50 iterations in the BDN loop. No seeded-RNG corpus needed — input
  is always the same minimal window.
- **BDN filter string:** `*WindowAppBenchmark*`
- **Negative-control method:** `window.Show(); window.Hide()` — exercises the same HWND
  show/hide path without the modal pump overhead (no EnumThreadWindows, no frame push).
- **Feasibility tag:** `high-cv` (runnable with warm-host, CV 5–10%)
- **Expected CV:** 5–10%. Mitigation: pin process affinity to 1 core; pre-warm with
  `--warmupCount 5`; close dialog within first dispatcher tick to minimise scheduling jitter.

---

### Shared Warm-Host Proxy — `Window.Show` + `Window.Hide`
(Covers Entries 1–4 as proxy and serves as negative control for Entry 5.)

- **Surface:** `window.Show()` → `window.Hide()` on a pre-constructed minimal `Window`
  (no XAML content, no data context) inside a `[GlobalSetup]`-initialised `Application`
  on a dedicated STA thread.
- **Hot path in Show:** `UpdateVisibilityProperty` → `ShowHelper` → `SafeCreateWindowDuringShow`
  → Win32 `ShowWindow(SW_SHOW)`. First call creates HWND; subsequent calls are show/hide
  of an existing HWND (cheaper). GlobalSetup must do one initial `Show`+`Hide` to warm
  the HWND before timed iterations begin.
- **Corpus shape:** single window instance, 50 iterations per BDN invocation. No RNG
  needed — cost is Win32-message-driven, not input-dependent.
- **BDN filter string:** `*WindowAppBenchmark*`
- **Feasibility tag:** `high-cv` (CV ~5–8% typical for Win32 show/hide)

---

## Section 3 — Final Summary Table

| Entry | Method (short) | Feasibility | BDN filter | Option | Expected CV |
|-------|---------------|-------------|-----------|--------|-------------|
| 1 | Application.RunDispatcher | proxy-only | `*WindowAppBenchmark*` | B | N/A (proxy) |
| 2 | Application.Run(Window) | proxy-only | `*WindowAppBenchmark*` | B | N/A (proxy) |
| 3 | Application.RunInternal | proxy-only | `*WindowAppBenchmark*` | B | N/A (proxy) |
| 4 | Application.Run() | proxy-only | `*WindowAppBenchmark*` | B | N/A (proxy) |
| 5 | Window.ShowDialog | high-cv | `*WindowAppBenchmark*` | A (warm host) | 5–10% |
| — | Window.Show→Hide (proxy) | high-cv | `*WindowAppBenchmark*` | A (warm host) | 5–8% |

**All five entries share one benchmark class: `WindowAppBenchmark`.**

Key design decisions:
1. Entries 1–4 are structural ancestors of `Dispatcher.Run` — their sample cost is 100%
   inherited from the message loop. The proxy measurement (Show→Hide throughput) captures
   real warm-steady-state cost without attempting to isolate what cannot be isolated.
2. `RunDispatcher` appears in both `window-app` and `dispatcher` clusters. The `dispatcher`
   cluster's implementer owns `PushFrameImpl` benchmarking; `window-app` must not duplicate
   it. `WindowAppBenchmark` must comment this explicitly.
3. `Window.ShowDialog` is the only entry that is independently benchmarkable (via a
   pre-enqueued close), but CV will be high due to `EnumThreadWindows` + `EnableWindow`
   cost. Mark with `benchmark_status: high-cv` in bench-queue.json if CV exceeds 5%
   on gate run.
4. The negative control for Show→Hide is `window.UpdateLayout()` (no Win32 message, no
   show/hide); the negative control for ShowDialog is `window.Show(); window.Hide()`.
