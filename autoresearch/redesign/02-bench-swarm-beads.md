# Benchmark Author Swarm — Bead Specs

**Project:** wpf-perf (branch: wpf-perf-harness)  
**Prefix:** bd  
**Bead format:** br/beads_rust conventions (see CLAUDE.md)

---

## Bead: bench-swarm-infra

- Type: task
- Priority: P1
- BlockedBy: (none)
- Files touched:
  - `autoresearch/redesign/bench-notes/` (create dir + .gitkeep)
  - `autoresearch/bench-queue.json` (create from profile.json)
  - `autoresearch/bench-skipped.tsv` (create empty with header row)
  - `autoresearch/bench-author-prompt.md` (Implementer agent prompt template)
  - `autoresearch/bench-designer-prompt.md` (Designer agent prompt template)
- Acceptance criteria:
  - [ ] `bench-queue.json` exists, lists all 30 `needs_benchmark: true` entries grouped by cluster (dispatcher/window-app/layout/media/misc), each with `status: pending`
  - [ ] `bench-skipped.tsv` has header `method\treason\tskip_date\tretry_after`
  - [ ] `bench-author-prompt.md` covers: GuardRails A-E from design doc, Gate 1-5 validation steps, GeometryParserBenchmark as model, atomic commit instruction, file reservation requirement
  - [ ] `bench-designer-prompt.md` covers: cluster entries, WPF source paths to read, output format (DESIGN-NOTES-<cluster>.md), Dispatcher feasibility check
  - [ ] `bench-notes/` directory exists (with .gitkeep so git tracks it)
- Test: `python3 -c "import json; d=json.load(open('autoresearch/bench-queue.json')); assert sum(1 for e in d['clusters'] for _ in e['entries']) >= 30"`

---

## Bead: bench-designer-round

- Type: task
- Priority: P1
- BlockedBy: bench-swarm-infra
- Files touched:
  - `autoresearch/redesign/bench-notes/DESIGN-NOTES-dispatcher.md`
  - `autoresearch/redesign/bench-notes/DESIGN-NOTES-window-app.md`
  - `autoresearch/redesign/bench-notes/DESIGN-NOTES-layout.md`
  - `autoresearch/redesign/bench-notes/DESIGN-NOTES-media.md`
  - `autoresearch/redesign/bench-notes/DESIGN-NOTES-misc.md`
- Acceptance criteria:
  - [ ] All 5 DESIGN-NOTES files exist
  - [ ] Each file identifies: the concrete WPF API to call in the benchmark, whether Dispatcher/STA is required (and which Option A/B/C applies), proposed corpus description (input type + count + seed strategy), proposed BDN filter string (e.g. `*DispatcherBenchmark*`), proposed negative-control method if applicable
  - [ ] DESIGN-NOTES-dispatcher.md explicitly calls out which entries get Option C (skipped) vs. Option A (STA wrap attempt)
  - [ ] Each notes file includes a `feasibility: runnable|needs-pump|proxy-only|skipped` tag
- Test: all 5 files present and each contains a `feasibility:` line

---

## Bead: bench-implement-misc-layout

- Type: task
- Priority: P2
- BlockedBy: bench-designer-round
- Files touched:
  - `microbench/Benchmarks/ExceptionWrapperBenchmark.cs`
  - `microbench/Benchmarks/CultureContextBenchmark.cs`
  - `microbench/Benchmarks/LayoutManagerBenchmark.cs`
  - `autoresearch/redesign/bench-notes/gate-results-misc.json`
  - `autoresearch/redesign/bench-notes/gate-results-layout.json`
- Acceptance criteria:
  - [ ] All `.cs` files compile (`dotnet publish` exit 0)
  - [ ] BDN `--warmupCount 2 --iterationCount 5` runs to completion for each new class
  - [ ] CV < 5% for each benchmark method (verified in gate-results JSON)
  - [ ] Each `.cs` file has the `// profile.json entry:` comment (Gate 4)
  - [ ] Corpus uses seeded RNG with minimum 50 iterations per BDN invocation (Gate 5)
  - [ ] Classes are `sealed`, helpers are `[MethodImpl(NoInlining)]`
  - [ ] Atomic commit per cluster: `bench(misc): author ExceptionWrapper + CultureContext benchmarks` then `bench(layout): author LayoutManager benchmark`
- Test: `Microbenchmarks.exe --filter '*ExceptionWrapper*' --list` and `*LayoutManager*` both return ≥ 1 benchmark

---

## Bead: bench-implement-media-dispatcher

- Type: task
- Priority: P2
- BlockedBy: bench-implement-misc-layout
- Files touched:
  - `microbench/Benchmarks/MediaContextBenchmark.cs`
  - `microbench/Benchmarks/DispatcherBenchmark.cs` (may contain only Option-A entries or be absent if all skipped)
  - `autoresearch/redesign/bench-notes/gate-results-media.json`
  - `autoresearch/redesign/bench-notes/gate-results-dispatcher.json`
  - `autoresearch/bench-skipped.tsv` (updated with any skipped dispatcher entries)
- Acceptance criteria:
  - [ ] MediaContextBenchmark.cs: DrawingContext throughput proxy compiles + CV < 5%
  - [ ] DispatcherBenchmark.cs: if authored, covers at least `OperationQueue` enqueue/dequeue path or a minimal STA-thread wrap of `Dispatcher.Invoke`; if skipped entirely, `bench-skipped.tsv` contains all 12 dispatcher entries with `reason: dispatcher-requires-pump`
  - [ ] Gate 1-3 pass for each authored benchmark
  - [ ] `bench-queue.json` updated: authored entries get `status: authored`, skipped entries get `status: skipped`
  - [ ] Atomic commit per authored cluster
- Test: `autoresearch/bench-skipped.tsv` row count equals number of entries with `status: skipped` in bench-queue.json

---

## Bead: bench-implement-window-app

- Type: task
- Priority: P3
- BlockedBy: bench-implement-media-dispatcher
- Files touched:
  - `microbench/Benchmarks/WindowLifecycleBenchmark.cs` (show/hide loop on minimal STA window)
  - `autoresearch/redesign/bench-notes/gate-results-window-app.json`
  - `autoresearch/bench-skipped.tsv` (if Window.Show/ShowDialog entries are unrunnable)
- Acceptance criteria:
  - [ ] If Window.Show is benchmarkable: STA thread + ApplicationDomain host spins a minimal WPF app, benchmark measures latency of one Show→Hide cycle; CV < 5%
  - [ ] If CV is 5-10% after corpus/GC tuning: entry marked `benchmark_status: high-cv` in bench-queue.json but .cs file kept (BDN filter usable, just noisier)
  - [ ] If consistently > 10% or crashes: entries added to bench-skipped.tsv with `reason: window-show-requires-full-app`
  - [ ] Gate 1 must pass even if Gate 3 fails (file compiles before being skipped)
  - [ ] Atomic commit: `bench(window-app): author WindowLifecycle benchmark`
- Test: gate-results-window-app.json exists and contains `cv_pct` field for each attempted benchmark

---

## Bead: bench-review-pass

- Type: task
- Priority: P2
- BlockedBy: bench-implement-misc-layout, bench-implement-media-dispatcher, bench-implement-window-app
- Files touched:
  - `autoresearch/redesign/bench-notes/review-report.md`
- Acceptance criteria:
  - [ ] review-report.md contains a `PASS` / `ISSUE` verdict per authored .cs file
  - [ ] Checklist for each file: Guardrail B (no hardcoded expected values), Guardrail C (corpus not trivially optimizable), Guardrail D (`sealed`), Guardrail E (`[NoInlining]` on helpers)
  - [ ] Issues are actionable: specific line + fix, not vague commentary
  - [ ] Reviewer marks any file that has a corpus of < 50 inputs as ISSUE-CORPUS
  - [ ] No code changes in this bead — output is the report only
- Test: `grep -c 'PASS\|ISSUE' autoresearch/redesign/bench-notes/review-report.md` equals number of authored .cs files

---

## Bead: bench-integrate

- Type: task
- Priority: P1
- BlockedBy: bench-review-pass
- Files touched:
  - `autoresearch/profile.json` (update `bdn_filter`, `needs_benchmark`, add `benchmark_class` field)
  - `autoresearch/bench-queue.json` (final status update)
  - `microbench/Benchmarks/*.cs` (reviewer-flagged patches, if any)
- Acceptance criteria:
  - [ ] Every entry in profile.json that passed Gate 1-4 has `bdn_filter` set and `needs_benchmark: false`
  - [ ] Every skipped entry retains `bdn_filter: null` and `needs_benchmark: true` but adds `benchmark_status` field
  - [ ] `bench-queue.json` has no entries with `status: pending`
  - [ ] Tier B `program.md` is NOT modified (immutable)
  - [ ] `dotnet publish` passes on final state of microbench/
  - [ ] Single atomic commit: `bench(profile): assign bdn_filter for N entries; skip M dispatcher entries`
  - [ ] `br sync --flush-only` run + beads directory committed
- Test: `python3 -c "import json; p=json.load(open('autoresearch/profile.json')); assert all(e['bdn_filter'] or e.get('benchmark_status') for e in p['hot_paths'] if not e['method'].startswith('(benchmarked)'))"` exits 0

---

## Bead: bench-smoke-verify

- Type: task
- Priority: P1
- BlockedBy: bench-integrate
- Files touched: (none — read-only verification)
- Acceptance criteria:
  - [ ] `Microbenchmarks.exe --list flat` shows ≥ 5 distinct benchmark methods (up from 2)
  - [ ] `microbench.py --filter '*LayoutManager*' --bench-name layout-smoke --no-revert` runs to completion (exit 0, 1, 2, or 4 are all acceptable — BENCH-FAIL exit 4 means benchmark ran but no signal, which proves the filter works; BUILD-FAIL exit 3 is a failure of this bead)
  - [ ] results.jsonl has a new row for the smoke run with `tier: "B"` and `bench_name: "layout-smoke"`
  - [ ] No benchmark runs produce exit code 4 (BENCH-FAIL) due to missing DLL or crash — only 0/1/2 are passing verdicts for this gate
- Test: `tail -1 autoresearch/results.jsonl | python3 -c "import json,sys; r=json.load(sys.stdin); assert r['bench_name']=='layout-smoke'"`

---

## Summary

| # | Bead | Priority | Role | Sequential? |
|---|------|----------|------|-------------|
| 1 | bench-swarm-infra | P1 | Orchestrator setup | Once |
| 2 | bench-designer-round | P1 | 5× parallel Designer agents | Parallel |
| 3 | bench-implement-misc-layout | P2 | 1 Implementer, 2 clusters | Sequential |
| 4 | bench-implement-media-dispatcher | P2 | 1 Implementer, 2 clusters | Sequential after #3 |
| 5 | bench-implement-window-app | P3 | 1 Implementer, 1 cluster | Sequential after #4 |
| 6 | bench-review-pass | P2 | 1 Reviewer | After all Implementers |
| 7 | bench-integrate | P1 | Orchestrator integration | After review |
| 8 | bench-smoke-verify | P1 | Read-only verification | After integrate |

**Total: 8 beads**  
Bootstrap beads: 1, 2 (run once to establish infra and designs)  
Per-batch authoring beads: 3, 4, 5 (re-run after each Tier A re-rank adds new entries)  
Review + integration: 6, 7 (re-run per batch)  
Verification: 8 (re-run per batch)
