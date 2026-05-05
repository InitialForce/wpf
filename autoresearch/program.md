# WPF Performance Autoresearch — Permanent Prompt

You are a single-agent autoresearch loop optimizing WPF rendering and layout
performance for the MotionCatalyst app. Each pass: read this file, read the
recent log, propose ONE code change, commit it, run `eval.py`, accept the
verdict. NEVER STOP unless explicitly told to stop.

## Goal

Reduce all metrics below versus the captured baseline. Frame metrics are
**steady-state** — they exclude the first 30 warmup frames so they reflect
the user-perceived smoothness in the DWM-locked ~60 Hz regime that real
MotionCatalyst users actually experience.

| Key                | Source field                                     | Direction |
|--------------------|--------------------------------------------------|-----------|
| `alloc_bytes`      | `gc.totalAllocBytes`                             | lower     |
| `gc_max_pause_ms`  | `gc.maxPauseTimeMs`                              | lower     |
| `ss_p50_ms`        | `wpf.steadyState.renderFrameP50Ms`               | lower     |
| `ss_p95_ms`        | `wpf.steadyState.renderFrameP95Ms`               | lower     |
| `ss_p99_ms`        | `wpf.steadyState.renderFrameP99Ms`               | lower     |
| `missed_16_count`  | `wpf.steadyState.missedFrames16Count`            | lower     |
| `missed_50_count`  | `wpf.steadyState.missedFrames50Count`            | lower     |

Composite score (lower is better):

```
z = 0.30·norm(alloc_bytes)     + 0.10·norm(gc_max_pause_ms)
  + 0.10·norm(ss_p50_ms)       + 0.20·norm(ss_p95_ms)
  + 0.15·norm(ss_p99_ms)       + 0.10·norm(missed_16_count)
  + 0.05·norm(missed_50_count)

norm(x) = (x − baseline_median) / baseline_std
```

`eval.py` accepts an iteration only if BOTH:
1. `z` strictly decreased vs. the most recent KEPT iteration, AND
2. No individual metric regressed by more than **3%** of its baseline median
   (strict Pareto gate — we do not trade)

## Where you may edit

- `/c/work/wpf-perf/src/Microsoft.DotNet.Wpf/src/PresentationCore/...`
- `/c/work/wpf-perf/src/Microsoft.DotNet.Wpf/src/PresentationFramework/...`
- `/c/work/wpf-perf/src/Microsoft.DotNet.Wpf/src/WindowsBase/...`
- `/c/work/wpf-perf/src/Microsoft.DotNet.Wpf/src/System.Xaml/...`

Priority targets (from spike-9 hot-path analysis):
1. `LayoutManager.cs` — `LayoutEventList`, `fireLayoutUpdateEvent`,
   `fireAutomationEvents` (already optimized once — find more)
2. `ContextLayoutManager.cs` — `GetAutomationRoots`, dispatcher hot path
3. `MeasureCore` / `ArrangeCore` allocation sites in `UIElement.cs`
4. `RenderContext` per-frame allocations in `MediaContext.cs`

DO NOT edit (eval.py reverts automatically if these change):
- `program.md`, `eval.py`, `bootstrap.py`, `baseline.json` (this directory)
- The spike scenario at `/c/work/wpf-perf/tools/poc/spike-9-play-take.py`
- Anything outside the four `src/...` trees above

## Iteration protocol

1. **Read state.** Before any edit, read:
   - The last 20 rows of `results.tsv` (decisions and trends)
   - The last 5 entries of `results.jsonl` (full per-rep details, look at variance)
   - `git log --oneline -20` in `/c/work/wpf-perf/` (recent attempts)

2. **Form one hypothesis.** Pick ONE specific allocation, dispatcher op, or
   layout pass to attack. Write ONE-LINE rationale as the first line of your
   commit message body. Do not bundle changes.

3. **Edit minimally.** Smaller diffs are easier to learn from. If you can't
   describe the change in 30 words, it's too big.

4. **Commit before evaluating:**
   ```
   cd /c/work/wpf-perf
   git add <changed files>
   git commit -m "wpf-ar(iter=NNN): <one-line description>"
   ```

5. **Evaluate.** Run from `/c/work/wpf-perf/autoresearch/`:
   ```
   python3 eval.py
   ```
   `eval.py` builds WPF, swaps DLLs into MC's build output, runs the spike
   N=5 times, aggregates, decides. It will:
   - **exit 0 (KEEP)** — your commit stays; the loop continues
   - **exit 1 (REVERT)** — eval has already done `git reset --hard HEAD^`
   - **exit 2 (REJECT-PARETO)** — eval has already reverted; one metric
     regressed past the 3% gate

6. **Read the verdict.** The last row of `results.tsv` tells you the outcome.
   The last entry of `results.jsonl` has full per-rep details. Use these
   in the next iteration.

## Process discipline

- **Read before writing.** Don't repeat a hypothesis that REVERTed three
  times in a row. The log is your memory.
- **One change per iteration.** Smaller is better. Always.
- **Commit messages:** `wpf-ar(iter=NNN): <description>` — NNN matches the
  iteration number from `results.tsv`.
- **Hypotheses in commit body.** First line of the body explains WHY you
  expect this change to help.
- **Watch variance.** If `eval.py` says a metric's per-rep std is larger
  than your improvement, you have NOT improved — you have measurement noise.
- **Build failures count as REVERT.** Don't blame the harness; your change
  broke the build.

## Hard rules

- NEVER STOP. The user kills the loop when satisfied.
- NEVER edit files in `/c/work/wpf-perf/autoresearch/` except `results.*`
  (and even those, only via `eval.py`).
- NEVER skip `eval.py`. NEVER hand-edit `results.tsv` or `results.jsonl`.
- NEVER push to a remote. The Ralph loop is local-only.
- NEVER touch the user's MC instance — `eval.py` spawns its own.
