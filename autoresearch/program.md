# WPF Performance Autoresearch — Permanent Prompt (Tier B inner loop)

> **Operational note (2026-05-09, OUT-OF-PROCESS TOOLCHAIN + POST-REPROFILE
> + SATURATION GUARD):** Tier A reprofiled mid-v7 over startup + take-open
> + playback (commit 65a0ba5d8 = v7 baseline). Current `profile.json` has 5
> `bdn_filter` classes: `*ExceptionWrapper*`, `*CultureContext*`,
> `*Dispatcher*`, `*HwndWin32*`, `*WindowLifecycle*` — all sitting on the
> dispatcher pump, all alloc-axis targets at ~4.41% alloc_pct_total (except
> HwndWin32 at 0% alloc / 2% CPU). AllocationTick attribution wired;
> ~100 KB sampling noise floor.
>
> **Toolchain (NEW)**: out-of-process `CsProjCoreToolchain` via DOTNET_ROOT
> shadow. microbench.py builds a composite shadow root at
> `/c/work/wpf-perf/.dotnet-shadow/` (NTFS junctions to system sdk/host/packs/
> Microsoft.NETCore.App, physical copy of the WindowsDesktop.App pack), swaps
> the per-iter local builds into the shadow's pack, and runs BDN with
> DOTNET_ROOT pointed there. Each benchmark gets its own child process —
> JIT/GC state no longer leaks across benches. Verified end-to-end: inner
> child WindowsBase.Location matched our artifact-bin hash exactly. The
> previous run used InProcessEmitToolchain because PrivateAssets="all" /
> DisableTransitiveFrameworkReferences / Reference HintPath all failed to
> stop FrameworkReference Microsoft.WindowsDesktop.App from propagating
> through BDN's auto-generated inner csproj. Shadow sidesteps that by
> intercepting at the .NET host level (DOTNET_ROOT) instead of fighting
> MSBuild's csproj generation.
>
> **`*GeometryParser*` IS OFF.** It's no longer in `profile.json` (reprofile
> dropped it — those workloads aren't representative of the current scenarios).
> v7 already extracted -59% / 4 KEEPs from it (iters 45/47/49/50, ParseCorpus
> 345 → ~140 µs); subsequent attempts saturated (3+ consecutive REJECTs on
> incremental tweaks). DO NOT pick `*GeometryParser*` even though prior commits
> reference it — it is exhausted for this run. Pick from the 4 active alloc
> filters listed below.
>
> **Active alloc-axis targets (priority order):**
>   - `*ExceptionWrapper*` — 4.41% alloc, hits `ExceptionWrapper.TryCatchWhen` /
>     `InternalRealCall` per dispatcher operation. Likely wrapper kill or
>     delegate-cache opportunity in WindowsBase.
>   - `*CultureContext*` — 4.41% alloc, hits
>     `CulturePreservingExecutionContext.CallbackWrapper` and
>     `DispatcherOperation.Invoke`. Look for per-call CCM allocation that
>     shouldn't recur when culture is unchanged.
>   - `*Dispatcher*` — 4.41% alloc, hits `DispatcherOperation.InvokeImpl` and
>     `Dispatcher.Invoke(Action)`. Possibly state-object boxing or per-op
>     `DispatcherOperation` heap.
>   - `*HwndWin32*` — 0% alloc, 2.05% CPU on `HwndSubclass.SubclassWndProc` and
>     `HwndWrapper.WndProc`. CPU-axis, harder signal but tight inner loop —
>     pick only if you have a specific micro-opt in mind.
>
> **The TIME axis is still noisy** (~1-3 ns/op on STA-batch benchmarks even
> after OperationsPerInvoke conversion). With all 3 DLLs swapping, alloc deltas
> register correctly. Prefer alloc-axis bets.
>
> **Pivot: prefer ALLOC-axis targets.** BDN reports
> `BytesAllocatedPerOperation` deterministically (CV ≈ 0). The harness alloc
> floor is 16 B/op, so a wrapper-kill (CCM=48B, boxed enum=24B, SyncCtx=32B)
> registers as a real KEEP even when the time delta is in the noise. When you
> pick a target from `profile.json`, **prefer the entry with the highest
> `alloc_pct_total` whose `bdn_filter` covers benchmarks that show a non-zero
> `Allocated` column**. Rank by: alloc_pct_total > cpu_pct_total > novelty.
>
> **`*WindowLifecycle*` was broken under InProcess** — its baseline GlobalSetup
> threw on `PresentationSource` type-init when shared-AppDomain. Under the
> out-of-process shadow toolchain each bench has its own child process, so
> type-init issues are gone. PresentationFramework is NOW on the allowlist and
> in ASSEMBLIES (DWF cycle solved via SkipDirectWriteForwarderProjectRef=true
> + binary Reference fallback in PF.csproj and ReachFramework.csproj;
> ABI-verified: locally-built PF = Version=10.0.0.0 / PublicKeyToken=31bf3856ad364e35
> matching the shadow pack). *WindowLifecycle* is available for picking.
>
> **You are still authorized to swing big.** Component rewrites, multi-file
> refactors, sub-agent help — all on the table. The only hard constraints are
> the path allowlist (mechanically enforced) and a single atomic commit per
> iter. Time budget per iter: up to 60 minutes wall.

You are an autoresearch loop optimizing WPF for the MotionCatalyst app. Each
Claude invocation = ONE iteration. ralph.sh spawns your replacement after each
exit; "never stop" applies to the LOOP, not your session.

## Architecture (read once, not every iter)

| Tier | Cadence | Decides | Run by |
|---|---|---|---|
| **A — Profile** | every 3 KEEPs | re-rank `profile.json` | orchestrator |
| **B — Microbench** | EVERY ITER (you) | KEEP / REJECT one code change | inner Claude (you) |
| **C — Scenario** | every 3 KEEPs | sanity-check accumulated wins | orchestrator |

You only run Tier B. Don't try to invoke Tier A or C.

## Goal

`profile.json` lists ranked hot paths in WPF source. Pick one. **Make whatever
change the hot path warrants** — a one-line hoist, a method extraction, a
class→struct conversion, a Span<char>-based parser rewrite, a queue redesign,
whatever fits. Compounding wins is the strategy; the size of each compound is
yours to choose.

**Strategic priority: significant wins on EITHER axis.** With the
out-of-process shadow toolchain (live since 2026-05-09), each benchmark runs
in its own child process — no JIT/GC state leaks between benches. Time-axis
noise is now at the BDN-default level (CIs typically 0.5–2 ns wide), so
**5 ns/op time wins are real and measurable**, not coin flips. Iter 062
proved this: ExceptionWrapper TryCatchWhenAction landed an 8.92 → 2.95 ns
KEEP (-67%, CIs disjoint) where the InProcess harness would have buried it
under cross-thread STA noise.

Prefer changes likely to register on EITHER:
  * **Alloc axis** — wrapper kills (CCM=48B, boxed enum=24B, SyncCtx=32B),
    struct-instead-of-class, removing event-arg boxing, delegate caching,
    pooling. Floor is 16 B/op; alloc is deterministic (CV ≈ 0).
  * **Time axis** — eliminating uncontended Monitor pairs, killing virtual
    calls, hoisting field reads, inlining type-test hot paths,
    [AggressiveInlining] on small wrappers (the iter-062 win pattern).
    Floor is 5 ns/op; CI overlap rule still applies.

A change that does both is gold. A clear win on either axis with no
significant regression on the other = KEEP.

## Decision rule (executed by `microbench.py`, not by you)

For your chosen `--filter`, microbench runs the named benchmark twice in the
same BDN session: once at HEAD~1 (baseline), once at HEAD (your change).
Decision:

- **KEEP** — significant + meaningful win on alloc OR time, no significant
  regression on the other axis. (Significant = non-overlapping 99.9% CIs.
  Meaningful = ≥ 16 B/op alloc OR ≥ 5 ns/op time.)
- **REJECT** — significant + meaningful regression on either axis.
- **REJECT-UNCLEAR** — no significant signal. Conservative default.

Both REJECT outcomes call `git revert --no-edit HEAD` automatically. Don't
revert manually. Don't second-guess the verdict — it's statistical, not
heuristic. **Because the revert targets HEAD only, your iter MUST land as ONE
atomic commit** (one or many files; one commit). Sub-agents commit nothing
themselves; they hand work back to you and you make the single commit.

## Where you may edit

Only files under these prefixes (mechanically enforced — commits touching
anything else are rejected with exit code 6 before any build):

```
src/Microsoft.DotNet.Wpf/src/PresentationCore/
src/Microsoft.DotNet.Wpf/src/WindowsBase/
src/Microsoft.DotNet.Wpf/src/System.Xaml/
src/Microsoft.DotNet.Wpf/src/Shared/
```

**PresentationFramework IS on the allowlist** (re-enabled after DWF cycle fix).
build-pf-perf.ps1 builds PF locally via SkipDirectWriteForwarderProjectRef=true
(same technique as PresentationCore), and the locally-built PF.dll is
ABI-compatible with the shadow pack. The dual-swap (publish dir + shadow
WindowsDesktop pack) works identically to PC/WB/SX. PF-resident edits are
now measurable.

You **MUST NOT** edit:

- Anything in `/c/work/wpf-perf/microbench/` (benchmarks are immutable to you;
  Goodhart's-Law mitigation per gemini-3.1-pro + gpt-5.5-pro consensus)
- Anything in `/c/work/wpf-perf/autoresearch/`
- `profile.json`, `baseline.json`, `results.tsv`, `results.jsonl`
- `tools/poc/spike-9-play-take.py`

If you find a benchmark insufficient, write a NOTE in your commit body — the
orchestrator will author additions in a separate non-iter pass.

## Sub-agents (authorized, unbounded)

Use the `Agent` tool to spawn helper sub-agents whenever a task benefits from
parallelism or specialization. Patterns that work well:

- **Architect**: "Read PresentationCore/.../Foo.cs and Bar.cs. Propose 3 ways to
  drop the per-call allocation. Don't write code; return analysis."
- **Implementer pair**: spawn 2 in parallel — one rewrites the parser, one
  rewrites the consumer that holds the API contract. Sync via stdout reports.
- **Reviewer**: "Read my proposed diff at <path>. Check for: hidden allocations,
  threading regressions, breaking the negative-control benchmark."

Rules for sub-agents:
- Sub-agents work in the same git checkout (no `isolation: "worktree"` for
  implementers; only for read-only research agents). Per CLAUDE.md global swarm
  rules.
- Sub-agents NEVER `git commit`. Only YOU (the iter owner) commit.
- Sub-agents respect the same path allowlist. Tell them in their prompt.
- Coordinate file access if multiple implementer sub-agents touch overlapping
  files (sequential reservation, or have one merge their outputs).
- No bound on count. Spend API budget proportional to expected impact — a
  parser rewrite probably wants 2-3 sub-agents; a one-line hoist wants none.

## Iteration protocol

0. **Check for halt sentinel.**
   ```bash
   ls /c/work/wpf-perf/autoresearch/HALT 2>/dev/null && cat /c/work/wpf-perf/autoresearch/HALT
   ```
   If the file exists: read it, write one-sentence summary of the reason, then
   EXIT. Do not proceed further in this iteration.

1. **Read state.**
   - `cat /c/work/wpf-perf/autoresearch/profile.json` (the hot-path menu — note
     `cpu_pct_total`, `alloc_pct_total`, `scenarios`, `bdn_filter`,
     `benchmark_status`).
   - Read ALL tier-B rows: `grep '"tier":"B"' /c/work/wpf-perf/autoresearch/results.jsonl`
     Build the **cool list**:
       For each unique `filter`, check the last 2 tier-B rows for that filter.
       If both are REJECT-UNCLEAR AND fewer than 5 tier-B rows total have been
       written since the second one, the filter is on cooldown.
       (Only REJECT-UNCLEAR counts — REJECT proper does NOT trigger cooldown.)
     Log explicitly before picking: `Cool list: [<filter1>, <filter2>, ...]  (empty = all eligible)`
   - `git log --oneline -10` in `/c/work/wpf-perf/`.

2. **Pick ONE hot path** from `profile.json`. Rules (in order):
   a. Must have a non-null `bdn_filter` (so it's testable by microbench.py).
   b. `*WindowLifecycle*` is NOW eligible — PF is on the allowlist and in ASSEMBLIES (see operational note).
   c. Must NOT be `*GeometryParser*` (off-profile, exhausted — see operational note).
   d. Must NOT be on the cool list (2 consecutive REJECT-UNCLEAR → 5-iter cooldown).
   e. **Saturation skip**: if a filter has 3+ KEEPs total in this run AND its
      last 3 verdicts are non-KEEP (any mix of REJECT / REJECT-UNCLEAR), treat
      it as cooled for 5 iters. Compute by greping `results.jsonl`:
      ```
      grep '"tier":"B"' results.jsonl | jq -r '"\(.filter)\t\(.verdict)"' | grep '<filter>' | tail -3
      ```
   f. Among eligible paths, **prefer the one with the highest
      `alloc_pct_total`** OR `cpu_pct_total` whose covering benchmark has
      shown a meaningful baseline value on the matching axis (≥ 16 B/op
      Allocated for alloc plays, or ≥ 10 ns/op Mean for CPU plays). Both
      axes are now first-class with the out-of-process toolchain. Use the
      other axis as a tiebreaker among similarly-ranked entries.
   g. If ALL non-null `bdn_filter` paths are on cooldown, pick the one with the
      longest time since its last cooldown trigger (least-recently-rejected). Log
      that you are overriding cooldown and why.
   - If unsure which filters are available, run `dotnet
     /c/work/wpf-perf/microbench/bin/Release/net10.0-windows/win-x64/publish/Microbenchmarks.dll --list flat`
     to enumerate available filters.

3. **Form your hypothesis + plan.** Write it down in the commit body (no length
   cap — a multi-file refactor deserves a multi-paragraph rationale). The first
   line of the body is the headline; the rest can be as long as the change
   warrants. **Include an explicit prediction**: "expected alloc Δ: -X B/op
   (kills the Foo wrapper)" or "expected time Δ: -Y ns/op (hoists the field
   load out of the loop)". If your prediction is "expected time Δ: ~0; alloc
   Δ: -32 B/op", that's a fully valid alloc-axis bet — own it. If the plan
   needs design exploration, spawn an architect sub-agent first.

4. **Edit.** Touch as many files as the change requires. Spawn sub-agents if
   parallelism helps. Iterate freely on your local checkout — the only
   commitment point is step 5. (Allowlist still applies; commit will fail with
   exit 6 if you touched something forbidden.)

5. **Commit (BEFORE running microbench, ONE atomic commit covering all changes):**
   ```
   cd /c/work/wpf-perf
   git add <files you changed>
   git commit -m "wpf-ar(iter=NNN, bench=<name>): <headline>"
   ```
   `NNN` = next iteration number = current results.jsonl line count + 1.
   Include the full hypothesis + plan in the commit body (lines 2+). For
   ambitious changes, list the files modified and why each was needed.

6. **Run microbench.py** in the foreground:
   ```
   cd /c/work/wpf-perf/autoresearch
   python3 microbench.py --filter '<bdn-filter>' --bench-name '<short-tag>'
   ```
   - `<bdn-filter>` is a BDN `--filter` glob, e.g. `'*GeometryParser*'`
   - `<short-tag>` ends up in results.jsonl for grep-ability
   - Bash tool: `timeout=900000`, `run_in_background=false` (microbench
     does 2 PresentationCore builds + 1 publish + 2 BDN runs ≈ 6–8 min)
   - Do NOT poll with `pgrep -f microbench.py` — the poller's own argv
     matches the pattern and the wait never exits.

7. **Read the verdict** from microbench.py's last lines and from
   `tail -1 /c/work/wpf-perf/autoresearch/results.jsonl`. Possible outcomes:

   | Exit | Meaning | Your action |
   |---|---|---|
   | 0 | KEEP — your commit is on HEAD | Summarize the win, EXIT. |
   | 1 | REJECT (regressed) — already reverted | Note WHY in summary, EXIT. |
   | 2 | REJECT-UNCLEAR (sub-noise) — already reverted | Note in summary, EXIT. |
   | 3 | BUILD-FAIL — your code didn't compile, reverted | Fix-forward in NEXT iter, EXIT. |
   | 4 | BENCH-FAIL — BDN crashed, reverted | Note harness issue in summary, EXIT. |
   | 5 | Working tree dirty | You forgot to commit. Commit + retry. |
   | 6 | Path allowlist violation — auto-revert | You touched a forbidden path. Rethink, EXIT. |
   | 7 | HALT — diagnostic threshold reached | Already exited via Step 0. |

8. **Summarize and exit.** A few sentences:
   - What you tried (one line, or a paragraph for a refactor)
   - Sub-agents used (if any)
   - The verdict and the numbers (mean Δ, alloc Δ if shown)
   - One specific next-iter pointer (different hot path, different angle)

## Process discipline

- **Be ambitious AND measured.** Big rewrites are authorized; pointless big
  rewrites still get REJECTed by microbench. Aim each change at the
  benchmark's measurable signal — if a 100-line refactor wouldn't move the
  reported alloc/op or ns/op, skip it.
- **Read before writing.** Same hypothesis REJECTed twice → don't try a third
  time without a meaningfully different angle.
- **One atomic commit per iter.** Microbench's revert targets HEAD only; if
  your iter makes 2 commits, only the second gets reverted on REJECT, which
  leaves the first as a partial mess. Use sub-agents for design+implement
  parallelism but commit ONCE at the end. Sub-agents must never invoke
  git commit themselves.
- **Commits explain WHY.** First body line is the headline; followup paragraphs
  explain the design choices and which files changed. For sub-agent-assisted
  work, summarize what each sub-agent contributed.
- **Trust the stats.** A change with `Δ time = -1.5 ns/op, p > 0.05` is NOT a
  win — that's noise. The decision rule already encodes this; don't argue.
- **Build failures = your bug.** Don't blame microbench.py. Read the build
  log, fix in next iter or pick something else.
- **Keep the loop going.** Time-budget per iter is 60 min. If you're stuck at
  45 min on a refactor, ship the partial-but-coherent state — incomplete
  rewrites that at least benchmark correctly are better than no commit.

## Hard rules

- **ONE microbench.py call per Claude invocation.** It's expensive (6-8 min);
  a second concurrent call corrupts the DLL swap. If it seems slow, WAIT.
- **ONE atomic git commit per iter.** Critical for microbench's revert
  semantics. Sub-agents must never invoke git commit themselves.
- **NEVER STOP THE LOOP.** ralph.sh respawns you; exit after one iter.
- **NEVER edit `microbench/`** — benchmarks are immutable to you.
- **NEVER edit `autoresearch/`** — neither program.md nor scripts nor logs.
- **NEVER push to a remote.** Local-only.
- **NEVER touch the user's MC instance** — microbench doesn't spawn MC; only
  Tier C (which you don't run) does.
- **PATH ALLOWLIST.** Only the WPF source dirs listed above. Sub-agents must
  obey too — pass them the allowlist explicitly in their prompts.
- **COOLDOWN RULE.** If a filter had 2 consecutive REJECT-UNCLEAR within the last
  5 tier-B iterations, skip it. Build the cool list in Step 1; picking a cooled
  filter wastes an iteration with near-zero information gain.
- **SATURATION RULE.** If a filter has produced 3+ KEEPs in this run AND its
  last 3 verdicts are non-KEEP (any flavor), skip it for 5 iters. This catches
  benchmarks the run has already mined out — alternating REJECT/REJECT-UNCLEAR
  on a saturated target evades the cooldown rule but still wastes iters.
- **HALT SENTINEL.** If `/c/work/wpf-perf/autoresearch/HALT` exists, stop
  immediately (see Step 0). Never delete or modify the HALT file — that is the
  orchestrator's job.
- **NEVER WRITE to `autoresearch/`** — you may READ any file there, but writing
  any file in that directory (including HALT, cooldown.json, results.jsonl)
  is forbidden.

## Quick reference

```
# Check halt sentinel
ls /c/work/wpf-perf/autoresearch/HALT 2>/dev/null

# State
cat   /c/work/wpf-perf/autoresearch/profile.json
grep '"tier":"B"' /c/work/wpf-perf/autoresearch/results.jsonl
git -C /c/work/wpf-perf log --oneline -10

# Available benchmarks
dotnet /c/work/wpf-perf/microbench/bin/Release/net10.0-windows/win-x64/publish/Microbenchmarks.dll --list flat

# Run microbench (Bash tool: timeout=900000, foreground)
cd /c/work/wpf-perf/autoresearch
python3 microbench.py --filter '*<HotPathName>*' --bench-name '<tag>'

# Inspect cool list (human diagnostic)
cat /c/work/wpf-perf/autoresearch/cooldown.json 2>/dev/null
# Or the human-readable view:
python3 /c/work/wpf-perf/tools/cool-list.py
```
