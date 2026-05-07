# Benchmark Author Swarm — Design Document

**Status:** Design only — no code yet  
**Author:** Planning agent (Sonnet 4.6), 2026-05-07  
**Branch:** wpf-perf-harness (wpf-perf repo)

---

## 1. Current State

### What exists
- `microbench/Benchmarks/GeometryParserBenchmark.cs` — real benchmark, corpus-based,
  exercises `Geometry.Parse`, covers the `*GeometryParser*` BDN filter
- `microbench/Benchmarks/SmokeBenchmark.cs` — no-op smoke check (`Size.Equals`)
- Both are `[Config(typeof(AutoresearchConfig))]`, self-contained (`win-x64`, `net10.0-windows`),
  no Dispatcher dependency

### What's missing
`profile.json` has **30 entries** with `needs_benchmark: true`, zero BDN filters assigned.
These cluster into **five logical groups** by benchmarkability:

| Cluster | Count | Benchmarkability |
|---------|-------|-----------------|
| Dispatcher infrastructure (Invoke/Wait/PushFrame loop) | 12 | Hard — Dispatcher requires a real Win32 message pump; pure BDN cannot host it without a dedicated STA thread and hand-rolled frame loop. Authors must wrap the method call in a helper that spins a minimal Dispatcher. |
| Window / Application lifecycle (Show, ShowDialog, Run) | 7 | Very hard — these block until a message loop exits. Only benchmarkable via a warm-start host that shows/hides a minimal window in a timed loop. High CV risk. |
| Layout (ContextLayoutManager, LayoutCallback) | 4 | Medium — UpdateLayout is callable in isolation on a UIElement tree without a real window, provided a PresentationSource is attached. CV typically < 3% in practice. |
| MediaContext / Render pipeline | 3 | Hard — RenderMessageHandler is called from inside the compositor thread; cannot be invoked directly. A throughput proxy (e.g. timed render-tick count on a headless DrawingContext) is the best available proxy. |
| Win32 wrappers (HwndSubclass, HwndWrapper, ExceptionWrapper, CulturePreservingExecutionContext) | 5 | Medium — ExceptionWrapper and CulturePreservingExecutionContext are purely managed dispatch wrappers with no UI state. HwndSubclass/HwndWrapper require a live HWND; benchmarkable via a minimal Win32 window created in GlobalSetup. |

### Root cause of the REJECT-UNCLEAR loop
Tier B only has one testable filter (`*GeometryParser*`). Every hot path in profile.json
is a Dispatcher/layout/render call that *has* no benchmark yet, so inner Claude bounces
between them or retries GeometryParser with diminishing returns. The loop must be fed a
real menu of filters before it can make progress.

---

## 2. Swarm Shape

### Design+Implement pair model
Each logical cluster gets a **2-agent pair**:
- **Designer** (Sonnet): reads the WPF source for the cluster, identifies the minimal
  surface to call in isolation, designs the corpus and input distribution, drafts the
  benchmark class skeleton with detailed comments, writes a `DESIGN-NOTES.txt` in a
  temp location.
- **Implementer** (Sonnet): takes the design notes, writes the `.cs` file, does a
  smoke build (`dotnet publish`), runs one BDN warm-up invocation with
  `--warmupCount 1 --iterationCount 3`, checks CV on those 3 samples,
  updates `profile.json` `bdn_filter` if passing, commits.

Pairs are sequential within a cluster (Implementer waits for Designer). Pairs for
**different clusters** run in parallel — 5 clusters × 2 agents = up to 10 agents
concurrently, but practically we run **2 clusters in parallel per round** (see §2.3).

### Rationale for pair model vs. single agent
- A single agent writing benchmarks for Dispatcher infrastructure repeatedly hits the
  same wall (no Dispatcher = crash). The Designer role forces up-front feasibility
  analysis before code is written.
- Reviewers are cheaper when they only see the final `.cs`, not the full exploration
  context of the designer.

### Parallelism rules
1. **Authoring (Designer agents):** fully parallel across clusters — they only read
   source, no shared files.
2. **Implementing (build step):** must be **sequential** — `dotnet publish` writes to
   `microbench/bin/...` and `microbench-staging/`; concurrent publishes corrupt each
   other's DLL swap. One cluster's Implementer commits before the next starts.
3. **Review pass:** a single reviewer agent checks all authored benchmarks in one pass
   after all Implementers complete.
4. **Commit:** orchestrator commits the complete batch after review.

### Concurrency schedule (per batch)
```
Round 1 (parallel): Designers for clusters A, B, C, D, E write DESIGN-NOTES
Round 2 (sequential): Implementer-A → validate → commit → Implementer-B → ... → Implementer-E
Round 3 (parallel): Reviewer reads all new .cs files, raises issues
Round 4 (sequential): Orchestrator applies reviewer patches, commits, updates profile.json
```

---

## 3. Validation Contract

Every authored benchmark MUST pass all gates before the orchestrator commits its `.cs`
file or updates `profile.json`:

### Gate 1 — Compilation
```
cmd.exe /c dotnet publish microbench/Microbenchmarks.csproj -c Release -r win-x64 --self-contained
```
Exit 0 required.

### Gate 2 — BDN completion
```
Microbenchmarks.exe --filter '*<NewBenchmarkClass>*' --warmupCount 2 --iterationCount 5
```
Must produce a `*-report-full.json` with at least one benchmark entry (same check as
`parse_bdn_results()` in `microbench.py`).

### Gate 3 — CV < 5%
From the 5-iteration JSON: `StandardDeviation / Mean < 0.05`. If CV is 5-10% on first
pass, author iterates once (increase corpus size, reduce GC pressure, pin CPU affinity
via `[GlobalCleanup]` GC.Collect). Above 10% after one retry → entry is marked
`benchmark_status: "needs-orchestrator"` in profile.json and skipped by Tier B.

### Gate 4 — Surface coverage comment
Every benchmark class MUST include a comment of the form:
```csharp
// profile.json entry: "<method>" (samples=NNN, cpu_pct_total=N.NN%)
// Exercises: <concrete WPF API call(s) made in this benchmark>
```
This is a lightweight by-eye linkage. No automated check; the reviewer agent verifies.

### Gate 5 — Representative corpus (no trivial single-input)
The Implementer's prompt requires:
- Minimum **50 distinct inputs** per iteration (or equivalent loop body) unless the
  method's cost is entirely input-independent (in which case comment must say why).
- Corpus generated from a seeded RNG (same pattern as GeometryParserBenchmark).

---

## 4. Anti-Goodhart Guardrails

The core invariant is already enforced mechanically: `microbench.py` checks
`ALLOWED_PATH_PREFIXES` and **rejects any commit that touches `microbench/`**
(exit code 6). Inner Claude cannot edit benchmarks. This section reinforces the
authoring side.

### Guardrail A — Negative-control inputs
For benchmarks where it's feasible, include a second `[Benchmark]` method that
exercises a code path *adjacent but not identical* to the hot path:

```csharp
[Benchmark(Description = "negative-control: <what changes if the optimization is correct>")]
public int NegativeControl() { ... }
```

This allows the harness to detect over-fitting: if both the target and the control
show a measured speedup, the "win" is an artifact (e.g. JIT warming effect).

### Guardrail B — No hand-written expected values
Benchmarks MUST NOT contain `if (result != expectedConstant) throw`. These invite
the inner agent to match a hardcoded expected output. Return values or consume them
via a `[DoNotOptimize]`/`BenchmarkDotNet.Engines.Consumer` consume pattern only.

### Guardrail C — Reviewer agent checks for corpus triviality
The reviewer agent receives the prompt:
> "Check: does this benchmark's corpus represent a realistic distribution of inputs,
> or has it been narrowed to a single edge case that would be easy to special-case?
> If the corpus is trivially optimizable (e.g. all-zeros, single string), flag it."

### Guardrail D — Benchmark class marked `sealed`
`sealed` prevents inner-agent subclassing tricks (not a realistic attack vector but
cheap to require).

### Guardrail E — `[DoNotInline]` on any setup helper
If the benchmark calls a helper that isolates the hot path, mark it `[System.Runtime.CompilerServices.MethodImpl(MethodImplOptions.NoInlining)]` so JIT cannot hoist it out of the benchmark loop and produce a degenerate constant.

---

## 5. Workflow Walkthrough

### Step 0 — Orchestrator reads profile.json
Finds all entries with `needs_benchmark: true`. Groups into clusters (see §1).
Writes a `bench-queue.json` listing each cluster with its entries and a `status` field
(`pending` / `authored` / `review-passed` / `skipped`).

### Step 1 — Designer round (parallel, all clusters)
Spawn 5 Designer agents (one per cluster). Each receives:
- The cluster's profile.json entries (method signatures + samples)
- A pointer to the GeometryParserBenchmark as the model
- Instruction to read the WPF source for the identified methods
- Output: a `DESIGN-NOTES-<cluster>.md` in `autoresearch/redesign/bench-notes/`

No file reservations needed — each Designer writes to a unique file.

### Step 2 — Implementer pipeline (sequential)
Orchestrator sequences Implementers by cluster, easiest first (ExceptionWrapper cluster,
then Layout, then MediaContext proxy, then Dispatcher, then Window).

Each Implementer:
1. Reads the cluster's `DESIGN-NOTES-<cluster>.md`
2. Writes `microbench/Benchmarks/<ClusterName>Benchmark.cs`
3. Runs Gate 1 (compile) + Gate 2 (BDN warm run) + Gate 3 (CV check)
4. On pass: writes `bench-notes/gate-results-<cluster>.json` with CV, iteration count,
   BDN filter string, and sample mean
5. On fail (Dispatcher crash / CV > 10% after retry): marks `status: skipped` in
   `bench-queue.json` and writes a note in `bench-notes/skip-reason-<cluster>.txt`
6. Commits the `.cs` file atomically: `bench(cluster): author <ClusterName>Benchmark`
7. Exits; orchestrator starts next Implementer

File reservations (via agent-mail) are taken on:
- `microbench/Benchmarks/<ClusterName>Benchmark.cs`
- `autoresearch/redesign/bench-queue.json`

### Step 3 — Reviewer pass (single agent)
One Reviewer agent reads all newly authored `.cs` files and the gate-results JSON.
Checks Guardrails B, C, D, E (§4). Produces a structured `review-report.md` with
`PASS` / `ISSUE` per file. Issues are actionable: either "accept as-is" or "specific
fix required".

### Step 4 — Patch + profile.json update (orchestrator)
Orchestrator applies any reviewer-flagged fixes (minor — should be < 5 line changes
per file). Then updates each passing entry in `profile.json`:
- Sets `bdn_filter` to the authored filter string (e.g. `"*DispatcherBenchmark*"`)
- Sets `needs_benchmark: false`
- Adds `benchmark_class` field for traceability

Commits: `bench(profile): assign bdn_filter for <N> entries after review`

### Step 5 — Skipped entries TSV
Writes `autoresearch/bench-skipped.tsv` with columns:
`method | reason | skip_date | retry_after`

Inner Claude's `program.md` instructs it to skip entries where `bdn_filter: null` (it
already does this via "pick one that has a matching benchmark"). The TSV is for
orchestrator audit — no code change needed.

---

## 6. Composition with Tier A Multi-Scenario

Tier A (being designed in parallel) will expand profile.json to ~15-20 entries with
both `cpu_pct_total` and `alloc_pct_total` columns.

Integration points:
- The benchmark-author swarm runs **after** each Tier A re-rank, treating new entries
  as a new batch. `bench-queue.json` is regenerated from profile.json delta.
- Existing benchmarks that match a re-ranked entry are *reused* (filter already set);
  only truly new entries enter the swarm.
- The `alloc_pct_total` column, once available, feeds Guardrail A: negative-control
  inputs can target allocation-neutral code paths (i.e., paths that will not change
  allocation even with correct optimization).
- A per-entry field `benchmark_last_authored` (ISO date) tracks staleness — if an
  entry was re-ranked with 2× higher `alloc_pct_total`, the orchestrator can trigger
  a benchmark refresh even if `needs_benchmark: false`.

---

## 7. Open Questions / Risks

### Risk 1 (BIGGEST): Dispatcher benchmarks may be permanently unrunnable
11 of 30 entries are Dispatcher call-chain members. Running `Dispatcher.Invoke` inside
BDN's default host crashes because there is no message pump and no STA thread. Solutions:

- Option A: A custom BDN `IConfig` that spins a `Thread(ApartmentState.STA)` + minimal
  `Dispatcher.Run()` and runs the benchmark body from inside it. Complex; fragile.
- Option B: Use a *throughput proxy* — measure the internal queuing logic (`OperationQueue.Enqueue`)
  directly via reflection or `internal` access (possible from the microbench project since
  it can reference internals via `InternalsVisibleTo`). More honest but requires source changes
  to WPF to add an `[assembly: InternalsVisibleTo]` attribute.
- Option C: Mark all Dispatcher entries as `benchmark_status: "dispatcher-requires-pump"`
  and focus the swarm's effort on the other 18 entries. Tier B's menu grows from 1 to
  ~8 actionable filters even without Dispatcher coverage. Accept the gap.

**Recommendation:** pursue Option C as default; attempt Option A for 2-3 key entries
(e.g. `DispatcherOperation.InvokeImpl`, `ExceptionWrapper.InternalRealCall`) as a
stretch goal with a defined retry limit.

### Risk 2: CV instability on Layout benchmarks
`ContextLayoutManager.UpdateLayout` traverses the full visual tree, so timing variance
depends on tree size. Mitigation: fix tree depth and element count in GlobalSetup; run
with `--server-gc` (already set in Microbenchmarks.csproj) and pin to one CPU core via
`Process.GetCurrentProcess().ProcessorAffinity = 1`.

### Risk 3: Benchmark staleness on profile re-rank
When Tier A produces a 4th-generation profile, old benchmarks may target methods that
dropped off the top-30. They remain harmless (BDN just runs extra filters) but consume
BDN time. Add a `benchmark_retired: true` field to profile.json entries that fall below
a `cpu_pct_total` threshold (< 0.5%).

### Risk 4: Inner Claude using benchmark existence to fake a win
If inner Claude notes that a new benchmark has high CV it might craft a change that
hits a consistently fast branch of the code. Guardrail A (negative controls) addresses
this; the reviewer agent explicitly checks for "corpus is all-easy inputs."

### Risk 5: Sequential Implementer pipeline bottleneck
5 sequential build-validate cycles × ~3 min each = ~15 min per batch. Acceptable for
a non-blocking orchestrator pass (it runs between Tier A re-ranks, not in the hot loop).
If profile.json grows to 30+ new entries, batch into groups of 5 and run groups in
sequence.
