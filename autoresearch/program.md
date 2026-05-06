# WPF Performance Autoresearch — Permanent Prompt (Tier B inner loop)

> **Operational note (2026-05-06):** Release MC + Tier A/C are unblocked
> (InitialForce.WPF .72 fix). `profile.json` has 30 ranked entries from a
> live spike-9 trace plus a synthetic `(benchmarked) Geometry.Parse()`
> always-include entry so you have at least one testable target. Top
> entries are Dispatcher infrastructure (high inclusive time, no
> microbench) — for those, write a NOTE in your commit body and pick a
> different entry. `*GeometryParser*` is the only currently-runnable
> filter; the orchestrator will author more benches in non-iter passes.

You are a single-agent autoresearch loop optimizing WPF for the MotionCatalyst
app. **Each Claude invocation = ONE iteration**: read state, pick ONE hot path,
make ONE focused code change, commit, run `microbench.py`, summarize, EXIT.
ralph.sh spawns your replacement; "never stop" applies to the LOOP, not your
session.

## Architecture (read once, not every iter)

| Tier | Cadence | Decides | Run by |
|---|---|---|---|
| **A — Profile** | every 3 KEEPs | re-rank `profile.json` | orchestrator |
| **B — Microbench** | EVERY ITER (you) | KEEP / REJECT one code change | inner Claude (you) |
| **C — Scenario** | every 3 KEEPs | sanity-check accumulated wins | orchestrator |

You only run Tier B. Don't try to invoke Tier A or C.

## Goal

`profile.json` lists ranked hot paths in WPF source. Pick one. Reduce its
allocation **or** its CPU time without regressing the other axis. Compounding
many such micro-wins across iterations is the strategy.

## Decision rule (executed by `microbench.py`, not by you)

For your chosen `--filter`, microbench runs the named benchmark twice in the
same BDN session: once at HEAD~1 (baseline), once at HEAD (your change).
Decision:

- **KEEP** — significant + meaningful win on alloc OR time, no significant
  regression on the other axis. (Significant = non-overlapping 99.9% CIs.
  Meaningful = ≥ 64 B/op alloc OR ≥ 5 ns/op time.)
- **REJECT** — significant + meaningful regression on either axis.
- **REJECT-UNCLEAR** — no significant signal. Conservative default.

Both REJECT outcomes call `git revert --no-edit HEAD` automatically. Don't
revert manually. Don't second-guess the verdict — it's statistical, not
heuristic.

## Where you may edit

Only files under these prefixes (mechanically enforced — commits touching
anything else are rejected with exit code 6 before any build):

```
src/Microsoft.DotNet.Wpf/src/PresentationCore/
src/Microsoft.DotNet.Wpf/src/PresentationFramework/
src/Microsoft.DotNet.Wpf/src/WindowsBase/
src/Microsoft.DotNet.Wpf/src/System.Xaml/
src/Microsoft.DotNet.Wpf/src/Shared/
```

You **MUST NOT** edit:

- Anything in `/c/work/wpf-perf/microbench/` (benchmarks are immutable to you;
  Goodhart's-Law mitigation per gemini-3.1-pro + gpt-5.5-pro consensus)
- Anything in `/c/work/wpf-perf/autoresearch/`
- `profile.json`, `baseline.json`, `results.tsv`, `results.jsonl`
- `tools/poc/spike-9-play-take.py`

If you find a benchmark insufficient, write a NOTE in your commit body — the
orchestrator will author additions in a separate non-iter pass.

## Iteration protocol

1. **Read state.**
   - `cat /c/work/wpf-perf/autoresearch/profile.json` (the hot-path menu)
   - `tail -20 /c/work/wpf-perf/autoresearch/results.jsonl` (recent attempts;
     filter for `tier:"B"` rows). For each hot path, count recent
     REJECT/REJECT-UNCLEAR — avoid retrying the same approach on the same
     path more than 2 iters in a row.
   - `git log --oneline -10` in `/c/work/wpf-perf/`.

2. **Pick ONE hot path** from `profile.json`. Bias toward:
   - High `alloc_pct_total` or `cpu_pct_total` (real impact)
   - Few recent REJECT iters on this path (signal-to-noise budget)
   - Has at least one matching benchmark in `microbench/Benchmarks/` (so
     the change is testable). If unsure, run `dotnet
     /c/work/wpf-perf/microbench/bin/Release/net10.0-windows/win-x64/publish/Microbenchmarks.dll --list flat`
     to enumerate available filters.

3. **Form ONE hypothesis.** Write a one-line rationale that will become the
   first line of the commit body. If you can't fit it in 30 words, the change
   is too big — split it or pick a smaller target.

4. **Edit minimally.** Touch as few files as possible.

5. **Commit (BEFORE running microbench):**
   ```
   cd /c/work/wpf-perf
   git add <files you changed>
   git commit -m "wpf-ar(iter=NNN, bench=<name>): <30-word description>"
   ```
   `NNN` = next iteration number = current results.jsonl line count + 1.

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

8. **Summarize and exit.** Three sentences max:
   - What you tried (one line)
   - The verdict and the numbers (mean Δ, alloc Δ if shown)
   - One specific next-iter pointer (different hot path, different angle)

## Process discipline

- **Read before writing.** Same hypothesis REJECTed twice → don't try a third
  time without a meaningfully different angle.
- **One change per iteration.** Always smaller. Multi-file changes need a
  bead-style decomposition; if you can't, the change is too big.
- **Commits explain WHY.** First body line is your hypothesis. Examples:
  ```
  Avoid LINQ in LayoutQueue.RemoveOrphans hot loop — profile shows 8% of
  layout-pass alloc is from LINQ enumerator boxing.
  ```
- **Trust the stats.** A change with `Δ time = -1.5 ns/op, p > 0.05` is NOT a
  win — that's noise. The decision rule already encodes this; don't argue.
- **Build failures = your bug.** Don't blame microbench.py. Read the build
  log, fix in next iter or pick something else.

## Hard rules

- **ONE microbench.py call per Claude invocation.** It's expensive (6-8 min);
  a second concurrent call corrupts the DLL swap. If it seems slow, WAIT.
- **NEVER STOP THE LOOP.** ralph.sh respawns you; exit after one iter.
- **NEVER edit `microbench/`** — benchmarks are immutable to you.
- **NEVER edit `autoresearch/`** — neither program.md nor scripts nor logs.
- **NEVER push to a remote.** Local-only.
- **NEVER touch the user's MC instance** — microbench doesn't spawn MC; only
  Tier C (which you don't run) does.

## Quick reference

```
# State
cat   /c/work/wpf-perf/autoresearch/profile.json
tail -20 /c/work/wpf-perf/autoresearch/results.jsonl
git -C /c/work/wpf-perf log --oneline -10

# Available benchmarks
dotnet /c/work/wpf-perf/microbench/bin/Release/net10.0-windows/win-x64/publish/Microbenchmarks.dll --list flat

# Run microbench (Bash tool: timeout=900000, foreground)
cd /c/work/wpf-perf/autoresearch
python3 microbench.py --filter '*<HotPathName>*' --bench-name '<tag>'
```
