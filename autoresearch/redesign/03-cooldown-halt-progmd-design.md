# Design: Path Cooldown, Diagnostic Halt, and program.md Updates

## Context

As of 2026-05-07, results.jsonl shows all Tier B entries (lines 17 onward) use
`filter: "*GeometryParser*"` — the only entry in profile.json with a `bdn_filter`
set. The benchmark-author swarm will add more filters, but even afterward, inner
Claude needs runtime guardrails so it doesn't fixate on whichever entry it prefers
when a path is temporarily exhausted.

---

## Element 1 — Path Cooldown

### Decision: Option (a) — compute from results.jsonl, no state file

Inner Claude reads results.jsonl at iter start (it already does `tail -20`). The
cooldown rule is expressed entirely in program.md; no new files are needed. This
keeps results.jsonl as the single source of truth, is simpler to implement (no
file write in microbench.py), and avoids a new file inner Claude might
misinterpret.

The orchestrator may still write a `cooldown.json` snapshot for **diagnostic
display** (see Element 2 / diagnostic halt section), but inner Claude does not
read it.

### Which verdicts count?

**Only REJECT-UNCLEAR counts toward cooldown, not REJECT.**

Rationale:
- REJECT means the change caused a meaningful regression — the path is interesting,
  just hard. Inner Claude should be allowed to try a different angle on the same
  path (e.g., "try allocation reduction instead of time").
- REJECT-UNCLEAR means the benchmark produced no signal — the change was too small
  or the benchmark noise floor is too high. Two consecutive REJECT-UNCLEARs on the
  same filter mean the current angle on this path is exhausted, not just hard.
  Continuing burns iterations with no information gain.
- BENCH-FAIL and BUILD-FAIL are harness failures, not path failures — they do not
  count toward cooldown.

### Cooldown parameters

- Trigger: **2 consecutive REJECT-UNCLEAR** on the same `filter` value.
  "Consecutive" means the 2 most recent Tier B rows for that filter are both
  REJECT-UNCLEAR (intervening non-UNCLEAR rows reset the count).
- Duration: **5 iterations** (counted as 5 subsequent rows in results.jsonl,
  regardless of path). After 5 rows have been appended since the second UNCLEAR,
  the path is eligible again.
- Matching key: the `filter` field (e.g., `"*GeometryParser*"`). This is more
  reliable than `bench_name` because bench_name is a free-form tag inner Claude
  chooses per run; filter is the BDN glob that uniquely identifies the benchmark
  group.

### Query algorithm (inner Claude, in Step 1)

```
1. Read ALL lines from results.jsonl (not just tail-20) filtered to tier=="B".
2. For each unique filter value, find the two most recent rows.
3. If both are REJECT-UNCLEAR:
   a. Find the row index (0-based line number in the full file) of the second-most-recent REJECT-UNCLEAR for that filter.
   b. Count how many total tier-B rows have been written AFTER that row index.
   c. If that count < 5, the filter is ON COOLDOWN — do not pick it.
4. Build the "cool list": all filters currently on cooldown.
5. From profile.json, restrict picks to entries whose bdn_filter is NOT on cooldown
   AND is not null.
6. If ALL non-null bdn_filter entries are on cooldown, pick the one with the oldest
   cooldown (least recently rejected) — i.e., "least-recently-rejected" fallback.
   Do NOT refuse to pick — that stalls the loop.
```

The 5-iter window uses row count rather than a timestamp so the cooldown degrades
naturally as the loop produces new rows, without inner Claude needing to do
date arithmetic.

### Starve risk and mitigation

If profile.json has only 2 non-null filters and both are on cooldown simultaneously,
the loop would stall under a strict rule. Mitigation: the fallback rule above (pick
the least-recently-cooldown'd filter) ensures the loop always makes a pick. The
cost is re-entering a barely-exhausted path earlier than desired, which is far
better than a stalled loop.

### Optional: cooldown.json snapshot

microbench.py (or a small helper) MAY write `autoresearch/cooldown.json` after
each run for human inspection:

```json
{
  "computed_at": "2026-05-07T10:00:00Z",
  "cool_filters": [
    { "filter": "*GeometryParser*", "cooled_at_row": 27, "rows_since": 2, "eligible_after_row": 32 }
  ]
}
```

Inner Claude does NOT read this file. It is for the orchestrator/human only.

---

## Element 2 — Diagnostic Halt

### Semantics

If the **last 10 consecutive Tier B iterations across all paths** are all
REJECT-UNCLEAR (i.e., the tail of the tier-B-filtered results.jsonl has no KEEP
and no REJECT and no BUILD-FAIL/BENCH-FAIL in the last 10 rows — only REJECT-UNCLEAR),
halt the loop.

The 10-row window deliberately excludes REJECT and BUILD/BENCH failures because
those are informative outcomes: a REJECT means a path is yielding signal (just
negative), and a BUILD-FAIL is a harness issue not a "no ideas" situation.
REJECT-UNCLEAR × 10 is the specific signature of "we've run out of actionable
micro-changes."

### Threshold tuning

The threshold is controlled by an environment variable: `WPF_AR_HALT_UNCLEAR_THRESHOLD`
(default 10). microbench.py reads this at exit time.

### Enforcement point

**microbench.py** is the right enforcement point. It has all the information needed
(results.jsonl path, the just-written row) and runs in the orchestrator's process
(ralph.sh waits on it). The flow:

```
main() → ...writes row to results.jsonl...
       → check_halt_threshold()
       → if halt: write HALT sentinel file, return special exit code 7
```

Exit code 7 is new: "HALT — diagnostic threshold reached."

ralph.sh already loops on exit codes; it needs one new check:

```bash
if [[ $rc -eq 7 ]]; then
    echo "[ralph] HALT sentinel written — stopping loop. See autoresearch/HALT."
    break
fi
```

### Sentinel file format

File: `/c/work/wpf-perf/autoresearch/HALT`

Written by microbench.py (the orchestrator-owned script). Inner Claude can READ
it (it's in the autoresearch dir which is readable) but CANNOT write it (write
restriction in program.md).

```
HALT: WPF autoresearch loop stopped — 10 consecutive REJECT-UNCLEAR across all paths.
Written: 2026-05-07T10:23:45Z
Last 10 tier-B rows:
  [2026-05-07T06:37:34Z] *GeometryParser*  REJECT-UNCLEAR  geom-parser-c-split-from-s
  [2026-05-07T06:43:18Z] *GeometryParser*  REJECT-UNCLEAR  geom-parser-struct-conversion
  ... (8 more)
Possible causes:
  1. All easy wins on covered paths are exhausted — the benchmark-author pass needs to
     cover new hot paths from profile.json.
  2. The benchmark noise floor is too high — BDN iteration count may need tuning.
  3. The profiler data in profile.json is stale — re-run Tier A.
Recovery:
  - Delete this file to allow the loop to resume.
  - Add a NOTE to program.md explaining what changed (new benchmarks, new profile, etc.).
  - Increase WPF_AR_HALT_UNCLEAR_THRESHOLD if you want a longer patience window.
```

Plain text is better than Markdown here because ralph.sh may `cat` it to terminal.

### Inner Claude's halt protocol

Inner Claude checks for the HALT file as the FIRST step of each iteration (before
reading profile.json). If present:

```
Read /c/work/wpf-perf/autoresearch/HALT.
Summarize its contents in one paragraph.
Write to stdout: "HALT sentinel present — loop was stopped by the orchestrator.
Reason: [first line of HALT]. Recovery: delete the HALT file and add a NOTE."
Exit immediately.
```

This prevents inner Claude from spending tokens on a full iter when the loop is
intentionally stopped.

### Recovery

Manual recovery by the orchestrator/human:
1. Delete `/c/work/wpf-perf/autoresearch/HALT`.
2. Optionally bump `WPF_AR_HALT_UNCLEAR_THRESHOLD` in the shell env.
3. Optionally run the benchmark-author swarm to cover new paths.
4. Re-launch `ralph.sh`.

No code change needed to resume — the sentinel's absence is the signal.

---

## Element 3 — program.md Updates

### 3a. Drop Tier C as per-iter gate

The current program.md architecture table already shows Tier C as "orchestrator"
cadence. The note at the top says "Release MC + Tier A/C are unblocked." The only
action needed is to confirm that inner Claude is not expected to invoke Tier C and
that the architecture table is clear. The current wording is correct; no change
needed for this element beyond confirming it.

### 3b. Add halt sentinel check (new Step 0 / top of iteration protocol)

Insert before Step 1:

```markdown
0. **Check for halt sentinel.** Before doing anything else:
   ```
   ls /c/work/wpf-perf/autoresearch/HALT 2>/dev/null
   ```
   If the file exists, read it, summarize its reason in one sentence, and EXIT.
   Do not proceed with the iteration. The orchestrator will handle recovery.
```

### 3c. Update Step 1 ("Read state") — enumerate cool list

Replace the current Step 1 bullet about results.jsonl with:

```markdown
1. **Read state.**
   - `cat /c/work/wpf-perf/autoresearch/profile.json` — the hot-path menu.
   - Read ALL tier-B lines from `/c/work/wpf-perf/autoresearch/results.jsonl`
     (use `grep '"tier":"B"' results.jsonl` or read the file in full).
     Build the **cool list**: for each unique `filter` value, if the two most
     recent tier-B rows for that filter are both `REJECT-UNCLEAR`, AND fewer
     than 5 tier-B rows have been written since the second REJECT-UNCLEAR, that
     filter is ON COOLDOWN — do not pick it.
   - Log the cool list explicitly in your reasoning before picking a path.
   - `git log --oneline -10` in `/c/work/wpf-perf/`.
```

### 3d. Update Step 2 ("Pick ONE hot path") — enforce cooldown

Replace the current Step 2 bullet about "Few recent REJECT iters" with:

```markdown
2. **Pick ONE hot path** from `profile.json`. Rules (in order):
   a. Must have a non-null `bdn_filter` (so it's testable by microbench.py).
   b. Must NOT be on the cool list (2 consecutive REJECT-UNCLEAR → 5-iter cooldown).
   c. Among eligible paths, prefer high `alloc_pct_total` or `cpu_pct_total`.
   d. If ALL non-null `bdn_filter` paths are on cooldown, pick the one with the
      longest time since its last cooldown trigger (least-recently-rejected). Log
      that you are overriding cooldown and why.
```

### 3e. Add cooldown and halt to "Hard rules"

Append to the Hard Rules section:

```markdown
- **COOLDOWN RULE.** If a filter had 2 consecutive REJECT-UNCLEAR within the last
  5 tier-B iterations, skip it. Build the cool list in Step 1; picking a cooled
  filter wastes an iteration with near-zero information gain.
- **HALT SENTINEL.** If `/c/work/wpf-perf/autoresearch/HALT` exists, stop
  immediately (see Step 0). Never delete or modify the HALT file — that is the
  orchestrator's job.
- **NEVER WRITE to `autoresearch/`** — you may READ any file there, but writing
  any file in that directory (including HALT, cooldown.json, results.jsonl, etc.)
  is forbidden.
```

### Full proposed program.md diff (orchestrator applies manually)

**Add Step 0** at the top of "Iteration protocol":

```markdown
0. **Check for halt sentinel.**
   ```bash
   ls /c/work/wpf-perf/autoresearch/HALT 2>/dev/null && cat /c/work/wpf-perf/autoresearch/HALT
   ```
   If the file exists: read it, write one-sentence summary of the reason, then
   EXIT. Do not proceed further in this iteration.
```

**Replace Step 1 tail-20 bullet** (keep the `git log` bullet, replace the results bullet):

```markdown
   - Read ALL tier-B rows: `grep '"tier":"B"' /c/work/wpf-perf/autoresearch/results.jsonl`
     Build the cool list:
       For each unique `filter`, check the last 2 tier-B rows for that filter.
       If both are REJECT-UNCLEAR AND fewer than 5 tier-B rows total have been
       written since the second one, the filter is on cooldown.
     Log: "Cool list: [<filter1>, <filter2>, ...]  (empty = all eligible)"
```

**Replace Step 2 bullet c** ("Few recent REJECT iters on this path"):

```markdown
   - Is NOT on the cool list. If all non-null bdn_filter entries are cooled,
     pick the least-recently-cooled one and log that you are overriding cooldown.
```

**Append to Hard Rules** (as above in 3e).

**Update "Quick reference"** to add:

```bash
# Check halt sentinel
ls /c/work/wpf-perf/autoresearch/HALT 2>/dev/null

# Inspect cool list (human diagnostic — inner Claude reads results.jsonl directly)
cat /c/work/wpf-perf/autoresearch/cooldown.json 2>/dev/null
```

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Cool list starves inner Claude when menu is sparse (1-2 non-null filters) | High until benchmark-author swarm runs | Least-recently-rejected fallback; never refuse to pick |
| Inner Claude miscounts "consecutive" (counts non-tier-B rows) | Medium | Explicit instruction to filter tier=="B" before counting |
| Inner Claude ignores the cooldown rule | Low-medium | Hard rule section + explicit log-the-cool-list instruction |
| Halt threshold too low (10) — halts on a brief noisy stretch | Low | WPF_AR_HALT_UNCLEAR_THRESHOLD env var; operator can raise |
| Halt threshold too high — loop burns API quota before halting | Low | 10 is ~1 hour of wall time; easy to Ctrl-C before that |
| Halt file written but ralph.sh doesn't check exit code 7 | Medium | ralph.sh needs a one-line addition (Element 2 enforcement) |
| microbench.py reads stale results.jsonl tail when computing halt | None | It just appended the new row, so the file is current |
| cooldown.json written by microbench.py conflicts with parallel runs | Very low (single-process loop) | N/A — ralph.sh is strictly serial |
