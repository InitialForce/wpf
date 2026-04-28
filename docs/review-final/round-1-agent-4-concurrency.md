# Review Agent 4/6 — Race Conditions, Concurrency, Durability
**Lens:** Parallel writes, concurrency groups, build-run dispatch, filesystem isolation, TOCTOU, state derivation  
**Date:** 2026-04-28  
**Status:** Read-only audit — no fixes applied

---

## 1. Parallel Ledger Writes — `_push_with_retry` (CRIT-3 fix)

### What the code does
`ledger-event.py::_push_with_retry` (lines 244–326) implements retry-with-rebase:

1. Push HEAD to origin. If rejected as non-fast-forward, proceed to retry.
2. `git fetch origin <branch>` + `git rebase origin/<branch>`.
3. `git reset --soft HEAD~1` to pop the bad commit without losing file changes.
4. Strip the last line of the ledger file (the entry with the wrong `prev_hash`).
5. Recompute `prev_hash` from the updated ledger tip, rebuild the line, re-append, re-commit.
6. `time.sleep(1)` back-off, then try pushing again.
7. Cap at `MAX_PUSH_RETRIES = 3`.

### Finding 1.1 — Conflict on rebase: silent abort, then hard fatal

When `_git_pull_rebase` (lines 217–241) runs `git rebase origin/<branch>` and that rebase encounters a conflict (e.g. two runners appended to the same ledger file with the same prev_hash and identical line content producing a merge conflict), the function calls `git rebase --abort` and returns `False`. The caller immediately calls `die(6, "Failed to rebase after non-fast-forward push rejection")`, exiting non-zero with no retry. This is acceptable crash semantics (the event is lost; the workflow step fails; GitHub Actions will mark the job red), but it is **not recovered** — no further retry is attempted and no ledger event is written for the failure. The concurrency group (one run per PR) means a second trigger would be required.

**More subtle edge case:** The rebase conflict scenario in question (same `prev_hash` + same line content) can occur when the two parallel runners (`review-1` and `review-2`) both read the same ledger tip at the same moment before either has pushed. Runner A pushes successfully. Runner B's rebase picks up Runner A's commit as the new base, then attempts to replay B's local commit (which appended a line using the OLD prev_hash). Since the ledger file was modified by both runners, git sees a content conflict in `.if-fork/patch-ledger.jsonl`. This is the exact scenario `_push_with_retry` tries to handle, but the rebase conflict path does **not** strip-and-rebuild — it just aborts and dies.

### Finding 1.2 — Three retries all fail: failure with no ledger tombstone

After `MAX_PUSH_RETRIES` (3) exhausted, `die(6, ...)` is called on the `attempt == MAX_PUSH_RETRIES` branch (line 270–277). The script exits with code 6 and a JSON error on stderr. The ledger does **not** receive a failure/tombstone event — the append that was attempted simply doesn't appear on the remote. The CI job fails, but the ledger remains consistent (no partial entry). However, the audit trail has a gap: the `review_1` or `review_2` event was never recorded, which means downstream steps (`sha-smuggling-check`, `merge-verdict`) would either not find a review entry or work from stale ledger state. This is a durability gap but not a corruption risk.

### Finding 1.3 — prev_hash recomputation: not atomic with file write

Between `_prev_hash_of_ledger(ledger_path)` on line 305 and the `ledger_path.open("a", ...)` on line 315, there is a TOCTOU window. Another process on the **same runner** could in theory write to the file. In the GitHub Actions model each job runs on an isolated runner, so cross-runner filesystem races are impossible. However, the script itself does not hold any file lock between the read and the write. If the script were ever invoked twice on the same runner (e.g., a retry step on the same machine), this would be a real race. In current usage this is benign.

More critically: the `git reset --soft HEAD~1` on line 285 operates on the local git working tree. Between that reset and the `_prev_hash_of_ledger` call, nothing prevents the rebased remote state from advancing again (another runner pushes). The script reads the locally-rebased ledger (which reflects origin after the rebase), so the `prev_hash` it computes IS correct at that moment. The race window is only the network latency from the rebase to the next push attempt; the 1-second sleep partially, but not fully, closes this.

### Finding 1.4 — Ping-pong scenario with two simultaneous retries

Both Runner A and Runner B enter their retry loops simultaneously. After the first push failure:
- Both rebase.
- Both pop their commits, strip the last ledger line, recompute prev_hash.
- Both now have the same prev_hash (the pre-existing ledger tip).
- Both commit new lines with that prev_hash.
- Both try to push.

One wins. The other fails again. That runner retries: rebase → strip → recompute → commit → sleep → push. Since retries are capped at 3 and each retry has a 1-second sleep, the worst case is 3 extra seconds per runner. The chain converges because the loser always rebases to incorporate the winner's entry. **This works correctly in the common case.**

Edge case: if the third retry for the "loser" collides with a third push from a third concurrent runner (e.g., `merge-verdict` also writes a ledger event on the same branch), the third runner's failure dies permanently. With `MAX_PUSH_RETRIES = 3` and `sleep(1)`, three truly concurrent writers on the same branch could exhaust the retry budget in a busy pipeline. In the current topology (2 parallel review jobs + 1 merge-verdict), this is a plausible scenario for a highly active pipeline.

### Finding 1.5 — `pr-ingestion` jobs also write ledger events on same branch

`pr-ingestion.yml` emits `pre_flight_failed`, `escalated`, `cherry_picked` events via `ledger-event.py --push`. These run sequentially within a single ingestion run (not in parallel with `pr-review`), but multiple concurrent ingestion runs for **different PRs** could push simultaneously. The concurrency group `ingestion-<pr_number>` (see below) prevents two ingestions for the same PR from colliding, but does NOT prevent two ingestions for PR #100 and PR #200 from writing the ledger concurrently. This is the expected retry target scenario and is handled by the retry logic — but means up to 4 concurrent writers (review-1, review-2, ingestion-PR100, ingestion-PR200) are possible.

---

## 2. Workflow Concurrency Groups

| Workflow | Group | Scope | Assessment |
|---|---|---|---|
| `pr-review.yml` | `pr-review-${{ ... pr_number }}` | Per-PR | Correct. Prevents re-triggering the same PR review. `cancel-in-progress: false` ensures in-flight reviews are not aborted. |
| `pr-ingestion.yml` | `ingestion-${{ inputs.pr_number \|\| client_payload.pr_number }}` | Per-PR | Correct. Prevents two cherry-picks of the same PR running simultaneously. |
| `release.yml` | `release-${{ github.ref }}` | Per-ref (tag) | Correct. Each tag triggers at most one release pipeline. |
| `nightly-rebase.yml` | `release-branch-write` | **Global singleton** | Correct — this is a global mutex for the staging branch rebase. Shared with `upstream-stable-adoption.yml` which also uses `release-branch-write`. This mutual exclusion is intentional. |
| `pr-discovery.yml` | **MISSING** | Uncontrolled | **Gap.** No `concurrency:` key. A manual `workflow_dispatch` overlapping with the scheduled daily run would start two discovery scans simultaneously. Both would scan upstream PRs and emit `discovered` ledger events, potentially duplicating entries in the ledger for the same PR. The ledger has no deduplication; `regenerate-state.py` would overwrite the PR's state with the latest event, but multiple `discovered` events would be emitted. |
| `build.yml` | `build-${{ github.ref }}` | Per-ref | Correct. `cancel-in-progress: true` means a superseded build is killed. |
| `claude-on-failure.yml` | `claude-on-failure-${{ workflow_run.id }}` | Per failed run | Correct. Each failure gets exactly one analysis run. |
| `upstream-stable-adoption.yml` | `release-branch-write` | Shared with nightly-rebase | Correct intentional sharing. |
| `weekly-differential.yml` | `weekly-diff-${{ github.event.schedule }}` | Per schedule | Adequate for a scheduled workflow. |

### Finding 2.1 — `pr-discovery.yml` has NO concurrency group

This is the only workflow without a `concurrency:` key. A workflow_dispatch trigger while the nightly schedule is running creates two concurrent discovery scans. Both query upstream dotnet/wpf PRs; both call `ledger-event.py` (via the `record` job) to emit `discovered` events; the ledger would contain duplicate `discovered` entries for the same PR number. The retry logic in `ledger-event.py` handles the push conflict, but the resulting ledger has redundant entries.

---

## 3. Build Run ID Capture (`gh workflow run --json`)

From `pr-ingestion.yml` lines 374–383:

```bash
RUN_ID=$(gh workflow run build.yml \
  --ref "$BRANCH" \
  --repo "${{ github.repository }}" \
  --json | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
```

### Finding 3.1 — `gh workflow run` does NOT return run ID in JSON

The `gh workflow run` command dispatches a `workflow_dispatch` event and exits. The GitHub CLI **does not return the run ID** in its output, even with `--json`. The `--json` flag for `gh workflow run` outputs the dispatch response body, which at the time of writing does not include the run ID (the GitHub API `/actions/workflows/{id}/dispatches` endpoint returns HTTP 204 with no body). This means `json.load(sys.stdin)['id']` will fail with a JSON parse error or KeyError, causing `RUN_ID` to be empty. The guard on line 378 (`if [ -z "$RUN_ID" ]`) then triggers, and the step exits 1 with `"Failed to capture build workflow run ID"`.

**This means `gh run watch` is never reached.** The build is dispatched but the ingestion pipeline immediately fails rather than waiting for the build to complete. This is a **critical functional bug**: the HIGH-6 fix (replacing `sleep` + `gh run list` with `gh run watch $RUN_ID`) is broken because the run ID is never successfully captured.

The correct approach would be to use `gh run list --workflow build.yml --branch "$BRANCH" --limit 1 --json databaseId` after a brief polling delay, or to use `gh workflow run` and then poll `gh run list` with retry until the run appears.

### Finding 3.2 — Race between dispatch and run list

Even if the run ID were captured via polling, there is an inherent race: `gh workflow run` triggers the dispatch, but GitHub may take several seconds to create the workflow run object. Querying immediately after dispatch may return no results or the wrong (older) run for that branch. A polling approach with a timeout (e.g., 30 seconds) is needed. The current code has no such retry — it makes a single query and fails if empty.

---

## 4. Filesystem Races During Cherry-Pick

Each GitHub Actions job runs on a **fresh ephemeral runner** provisioned per-job. The cherry-pick job (`pr-ingestion.yml`, job 4) checks out the repository fresh via `actions/checkout@v4`. There is no shared filesystem between runners.

**Within** a single job, the Claude action (`anthropics/claude-code-action@v1`) and the subsequent `push cherry-pick branch` step share the same working directory. The `--disallowedTools mcp__github__push_files` flag prevents Claude from pushing via API, and the `git push origin ... --force-with-lease` is done in the shell step after Claude exits. No concurrency exists within a single job.

**Finding 4.1 — `--force-with-lease` on a branch Claude may have pushed:** If Claude's Bash tool ran `git push` during its turns (which Bash allows despite the disallowed MCP tool), the `--force-with-lease` in the shell step would succeed (updating the lease target), but a different PR's run that happened to push to the same branch name earlier would be overwritten. Branch names are `claude/cherry-pick-pr-<N>`, which are per-PR, so collision is only possible if the same PR is ingested twice (prevented by the concurrency group). This is safe in practice.

**Finding 4.2 — No filesystem isolation concern:** Each runner is fresh, so no cross-workflow filesystem races exist. This is a non-issue by design.

---

## 5. Ledger TOCTOU and Retry Convergence

The sequence of concern (from the prompt) is:

> Both runners read prev_hash X → both commit with prev_hash=X → push #1 succeeds, push #2 rejected → retry: rebase, read new prev_hash Y, commit with prev_hash=Y, push #2 succeeds.

This works correctly **in the two-runner case** and is confirmed by the code analysis in Finding 1.4 above.

### Finding 5.1 — Three-runner collision exhausts retry budget

If three runners (review-1, review-2, and merge-verdict or an ingestion event) are all writing simultaneously and unlucky timing means all three are always in their retry at the same time, one runner will exceed 3 retries and fail permanently. This is bounded, not an infinite loop, but it does mean a ledger event could be permanently lost under high concurrency. The probability is low with the current pipeline topology but nonzero.

### Finding 5.2 — Line stripping logic has an off-by-one on single-line ledger

In `_push_with_retry`, lines 297–303:

```python
if all_lines:
    ledger_path.write_text(
        "\n".join(all_lines[:-1]) + ("\n" if len(all_lines) > 1 else ""),
        encoding="utf-8",
    )
```

If the ledger contains exactly one line (genesis entry) and `all_lines[:-1]` produces an empty list, `"\n".join([])` is `""` and the conditional `("\n" if len(all_lines) > 1 else "")` adds nothing. So `write_text("")` is called — the ledger is emptied. Then `_prev_hash_of_ledger` returns `""` (genesis), and the retry builds a genesis-level entry as if the ledger were empty. **This erases any previously committed ledger content that was rebased in.** This is an edge case (only triggers when the runner's own append is the ONLY line in the entire ledger), but it is a correctness bug: after the rebase, the ledger file has content from origin, so `all_lines` should have more than one line in practice. Still, the logic is fragile.

---

## 6. State Derivation Race (`regenerate-state.py`)

### Finding 6.1 — `regenerate-state.py` is NOT invoked from any workflow

A thorough search of `.github/workflows/` and `tools/` confirms that `regenerate-state.py` is never called from any workflow file. It has no `concurrency:` coupling concerns because it runs only manually (CLI) or in local tooling. The `pr-discovery.yml` passes `STATE_PATH` as an env var to the Claude prompt, suggesting Claude may read `patch-state.json` during discovery, but `regenerate-state.py` is not called to generate or validate it within CI.

**This means:** The `patch-state.json` file committed to the repo may become stale relative to the `patch-ledger.jsonl` without any automated reconciliation. If `regenerate-state.py` were invoked concurrently with `ledger-event.py` during a CI run, the state file could reflect a partial ledger snapshot. But since it is never invoked from CI, this is a potential operational gap (stale state) rather than a live concurrency hazard.

### Finding 6.2 — No invocation from CI means no state freshness guarantee

Any tool or Claude prompt that reads `patch-state.json` from the checked-out repo reads a potentially stale snapshot. The `sha-smuggling-check` job in `pr-ingestion.yml` (lines 155–218) reads the ledger directly (`patch-ledger.jsonl`) — not the state file — which is the correct approach. However, the `pr-discovery.yml` prompt receives `STATE_PATH` pointing to the committed state file, which may lag behind the ledger if no one has manually run `regenerate-state.py` after recent ledger events.

---

## Summary Table

| # | Finding | Severity | File | Lines |
|---|---|---|---|---|
| 1.1 | Rebase conflict on ledger file kills retry without recovery | HIGH | `tools/ledger-event.py` | 280–281 |
| 1.2 | 3 retry exhaustion: no tombstone ledger event, audit gap | MED | `tools/ledger-event.py` | 270–277 |
| 1.3 | TOCTOU between prev_hash read and file append (benign per-runner) | LOW | `tools/ledger-event.py` | 305–315 |
| 1.4 | Ping-pong retry converges correctly for 2 runners | OK | `tools/ledger-event.py` | 260–326 |
| 1.5 | Multiple concurrent ingestion PRs = multiple concurrent ledger writers | INFO | `pr-ingestion.yml` | — |
| 2.1 | `pr-discovery.yml` missing `concurrency:` group — duplicate `discovered` events possible | MED | `.github/workflows/pr-discovery.yml` | — |
| 3.1 | `gh workflow run --json` does NOT return run ID; HIGH-6 fix is broken | CRIT | `pr-ingestion.yml` | 374–377 |
| 3.2 | Race: run not yet created when polled after dispatch | MED | `pr-ingestion.yml` | 374–383 |
| 4.1 | `--force-with-lease` safe; no cross-PR branch collision | OK | `pr-ingestion.yml` | 300–301 |
| 5.1 | 3 concurrent writers can exhaust MAX_PUSH_RETRIES | MED | `tools/ledger-event.py` | 260, 37 |
| 5.2 | Single-line ledger stripped to empty on retry (off-by-one) | MED | `tools/ledger-event.py` | 297–303 |
| 6.1 | `regenerate-state.py` never called from CI; no concurrency hazard | INFO | `tools/regenerate-state.py` | — |
| 6.2 | `patch-state.json` can be stale; discovery prompt reads stale state | MED | `pr-discovery.yml`, `tools/regenerate-state.py` | — |

---

## 5-Sentence Summary

The CRIT-3 retry logic in `_push_with_retry` correctly handles the common two-runner ping-pong (review-1 and review-2 colliding on the same ledger branch) but has two concrete failure modes: a rebase conflict on the ledger file itself causes an immediate unrecovered fatal exit rather than a further retry attempt, and the single-line ledger edge case in the line-stripping logic silently zeros the file before recomputing `prev_hash`. The `pr-discovery.yml` workflow is the only CI workflow without a `concurrency:` group, meaning a manual dispatch overlapping the nightly schedule produces duplicate `discovered` ledger events for the same PR. The HIGH-6 build-run-ID fix is broken: `gh workflow run --json` does not return a run ID in its output, so `RUN_ID` is always empty, the guard fires immediately, and `gh run watch` is never reached — the ingestion pipeline fails at the build-and-test step without actually waiting for the build. Three simultaneous ledger writers (review-1, review-2, plus a concurrent ingestion run) can exhaust the three-retry budget, permanently losing a ledger event with no tombstone written. The `regenerate-state.py` tool is never invoked from any CI workflow, so `patch-state.json` is a potentially stale snapshot that cannot be trusted as a consistent view of the ledger at pipeline runtime.
