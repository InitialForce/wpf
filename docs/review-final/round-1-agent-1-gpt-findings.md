# Round-1 Agent-1: GPT-5.5-Pro Review — Fix Verification Audit

**Agent:** Review agent 1/6 — Lens: GPT-5.5-Pro findings verification  
**Date:** 2026-04-28  
**Branch audited:** if/main at `/c/work/wpf-fork-real/`  
**Original review:** `/c/work/wpf-fork-impl/docs/REVIEW_GPT_PRO.md`

---

## Executive Summary

Of the 22 original findings (4 CRIT + 8 HIGH + 6 MED + 4 LOW), **10 are FIXED, 7 are PARTIAL, 3 are STILL-BROKEN, and 2 are NEW-ISSUE** (introduced by the fixup).

The most dangerous unresolved issues are:
1. **CRIT-1 (PARTIAL)**: Gate output not transitively propagated — `sha-smuggling-check`, `cherry-pick`, `denylist-check`, `build-and-test`, and `perf-gate` jobs in `pr-ingestion.yml` all reference `needs.gate.outputs.proceed` but do not list `gate` in their direct `needs:`. In GitHub Actions, outputs from non-direct dependencies resolve to an empty string, so all these jobs evaluate their `if:` condition as `'' == 'true'` → `false` → **permanently skipped**. The entire ingestion pipeline after `pre-flight` is a dead end.
2. **CRIT-2 (PARTIAL)**: Three secondary workflows (`nightly-rebase.yml`, `pr-discovery.yml`, `claude-on-failure.yml`) still use the old broken CLI arguments (`--details`, `--actor-run-url`) and are missing required arguments (`--pr-number`, `--head-sha`, `--details-json`). All ledger writes in those workflows will crash at runtime.
3. **HIGH-6 (STILL-BROKEN/NEW-ISSUE)**: The `build-and-test` job attempts `gh workflow run build.yml --json | python3 -c "... ['id']"`. The `gh workflow run` subcommand does not output JSON containing a run ID (it dispatches asynchronously). The `if [ -z "$RUN_ID" ]` guard will always trigger → the job always fails with exit 1, making the entire pipeline stall at build-and-test.

---

## Findings Detail

### CRIT-1: Autonomy kill-switch non-enforcing
**Verdict: PARTIAL**

**What was fixed:**
- `autonomy-check.yml` now has Gate 0 (default-deny for unknown `requested_action`).
- `pr-review.yml` calls gate with `requested_action: claude-invoke` and `bypass_for_human_dispatch`.
- `release.yml` calls gate with `requested_action: release-publish`.
- `pr-ingestion.yml` calls gate with `requested_action: cherry-pick`.
- `pre-flight` job (the direct child of `gate`) correctly checks `needs.gate.outputs.proceed == 'true'`.

**What remains broken:**

`pr-ingestion.yml` — every job downstream of `pre-flight` references `needs.gate.outputs.proceed` but does NOT list `gate` in its direct `needs:` list:

| Job | `needs:` list | `gate` present? |
|-----|--------------|-----------------|
| `sha-smuggling-check` | `pre-flight` | NO |
| `cherry-pick` | `sha-smuggling-check` | NO |
| `denylist-check` | `cherry-pick` | NO |
| `build-and-test` | `denylist-check` | NO |
| `perf-gate` | `[build-and-test, cherry-pick]` | NO |
| `open-pr` | `[perf-gate, cherry-pick]` | NO |

Per GitHub Actions documentation: outputs are only available from jobs in the *direct* `needs:` list. Transitive dependencies do not propagate outputs. Therefore `needs.gate.outputs.proceed` evaluates to `''` for all these jobs → all conditions evaluate to `'' == 'true'` → `false` → all jobs are **permanently skipped**.

**Evidence:** `.github/workflows/pr-ingestion.yml:126-127`, `:229-230`, `:311-312`, `:358-359`, `:392-393`, `:453-454`

**Recommendation:** Add `gate` to the `needs:` list of every downstream job that checks `needs.gate.outputs.proceed`, or cascade the output via an intermediate output (e.g., `pre-flight` outputs `gate_proceed` and downstream jobs check `needs.pre-flight.outputs.gate_proceed`).

---

### CRIT-2: Ledger CLI contract broken throughout
**Verdict: PARTIAL**

**What was fixed:**
- `pr-review.yml`: all 4 `ledger-event.py` invocations (review_1, review_2, review_single_path_warning, merged_verdict) now use correct args: `--event`, `--pr-number`, `--head-sha`, `--actor`, `--details-json`, `--push`.
- `pr-ingestion.yml`: all 4 invocations (pre_flight_failed, escalated×2, cherry_picked) use correct args.
- `release.yml`: the `published` event invocation uses correct args.
- `ledger_schema.py` created as shared module with `merged_verdict` added to `VALID_EVENTS`.
- Both `ledger-event.py` and `ledger-validate.py` import from `ledger_schema.py`.

**What remains broken:**

Three secondary workflows still use the original broken CLI:

**`.github/workflows/nightly-rebase.yml:206`** — `rebase_failed` event:
```
python tools/ledger-event.py \
  --event rebase_failed \
  --details "{...}" \                  # INVALID (should be --details-json)
  --actor-run-url "..."                # INVALID (not an argparse arg)
                                       # MISSING: --pr-number, --head-sha, --actor
```
Additional issue: `rebase_failed` is NOT in `VALID_EVENTS` → double failure.

**`.github/workflows/nightly-rebase.yml:303`** — `autonomy_resumed` event:
```
python tools/ledger-event.py \
  --event autonomy_resumed \           # IS in VALID_EVENTS
  --details "{...}" \                  # INVALID
  --actor-run-url "..."                # INVALID
                                       # MISSING: --pr-number, --head-sha, --actor
```

**`.github/workflows/pr-discovery.yml:116`** — `discovered` event:
```
python tools/ledger-event.py \
  --event discovered \                 # IS in VALID_EVENTS
  --batch-file /tmp/discovered-batch.json \  # NOT IN ARGPARSE → crash
  --actor-run-url "..."                # INVALID
```
`--batch-file` is not implemented in `ledger-event.py`. Additionally `--pr-number`, `--head-sha`, `--actor`, `--details-json` are all missing.

**`.github/workflows/claude-on-failure.yml:236`** — `failure_analyzed` event:
```
python tools/ledger-event.py \
  --event failure_analyzed \           # NOT in VALID_EVENTS → crash
  --details "{...}" \                  # INVALID
  --actor-run-url "..."                # INVALID
```

Also: `pre_flight_failed` (used in pr-ingestion.yml:104) and `review_single_path_warning` (used in pr-review.yml:245) are NOT in `VALID_EVENTS` in `ledger_schema.py`. Every invocation of these event types will fail at the `if args.event not in VALID_EVENTS` guard in `ledger-event.py`.

**Evidence:**
- `.github/workflows/nightly-rebase.yml:206,303`
- `.github/workflows/pr-discovery.yml:116`
- `.github/workflows/claude-on-failure.yml:236`
- `tools/ledger_schema.py` (missing: `rebase_failed`, `failure_analyzed`, `pre_flight_failed`, `review_single_path_warning`)

**Recommendation:** Fix all 4 secondary workflows with correct CLI. Add the 4 missing event types to `ledger_schema.py`. Implement `--batch-file` in `ledger-event.py` or split discovery into per-PR individual calls.

---

### CRIT-3: Ledger race-prone and non-durable
**Verdict: PARTIAL**

**What was fixed:**
- `ledger-event.py` now pushes after each commit (`_push_with_retry`).
- Retry logic handles non-fast-forward: pull/rebase → strip last ledger line → recompute `prev_hash` → re-append → recommit → retry push. `MAX_PUSH_RETRIES = 3`.
- `--push` flag defaults to `True` in CI (`CI=true`), `False` outside.
- All workflow invocations pass `--push` explicitly or rely on CI default.
- `Ledger-Line-Hash:` trailer added to git commits (supports MED-5 fix).

**What remains incomplete:**
- No concurrency group serializing ledger writes. When `review-1` and `review-2` run in parallel (the designed behavior), both jobs read the same `prev_hash`, both write an entry, and both try to push simultaneously. The retry-on-non-FF handles one of them backing off and rebasing, but under high parallel load this creates a storm of retries with a 1-second back-off. With only 3 retries, a burst of 3+ simultaneous writers would exhaust retries and fail.
- If push is permanently blocked (e.g., the ledger branch has branch protection requiring PR review), `is_non_ff` is `False`, so `die()` is called immediately after the first attempt. The error surface is correct, but the root cause — ledger branch potentially having the same protection as `if/main` — is unresolved.

**Evidence:** `tools/ledger-event.py:244-327` (retry logic), `tools/ledger-event.py:260-278` (retry cap)

**Recommendation:** Add a `concurrency: group: ledger-write cancel-in-progress: false` group to each ledger-writing step. Even better: introduce a serialized `ledger-write` job that all parallel jobs dispatch to via `repository_dispatch`.

---

### CRIT-4: merge-verdict job references non-existent needs.gate
**Verdict: FIXED**

`merge-verdict` in `pr-review.yml` now declares `needs: [gate, review-1, review-2, single-review-approval]`. The `gate` job is a direct dependency. The `if:` condition uses `always() && needs.gate.outputs.proceed == 'true'` with fallthrough for skipped `review-2`/`single-review-approval`.

**Evidence:** `.github/workflows/pr-review.yml:263`

---

### HIGH-1: Two-review guarantee silently disabled when IF_REVIEW_DOUBLE_REQUIRED is unset
**Verdict: FIXED**

`review-2` condition changed from `== 'true'` to `!= 'false'`. When `IF_REVIEW_DOUBLE_REQUIRED` is unset (`''`), `'' != 'false'` is `true` → `review-2` runs. When explicitly set to `'false'`, `review-2` is skipped.

Single-review fast path now requires `environment: branch-promotion` (a manual human approver) and emits a `review_single_path_warning` ledger event before `merge-verdict` runs.

**Note:** `review_single_path_warning` is NOT in `ledger_schema.py:VALID_EVENTS`. The ledger emit in `single-review-approval` (`.github/workflows/pr-review.yml:244`) will crash at runtime. (Cross-reference CRIT-2.)

**Evidence:** `.github/workflows/pr-review.yml:150`, `:217-254`

---

### HIGH-2: Unsigned ledger commits silently allowed in production
**Verdict: FIXED**

`_git_commit()` in `ledger-event.py` now calls `_is_ci()` and, when in CI, calls `die(5, ...)` if GPG signing fails instead of falling back to unsigned commit. Outside CI, the warning+fallback is preserved.

**Evidence:** `tools/ledger-event.py:136-205`

---

### HIGH-3: SHA smuggling check is not ledger-backed and has unsafe fallback
**Verdict: FIXED**

The `verify-sha` step now reads the ledger JSONL file and extracts `head_sha` from the last `review_1` or `review_2` entry for the PR. The Python snippet iterates all lines and overwrites `review_sha` on each matching entry — so the last (most recent) review event wins, which is correct for re-reviews. If no review entry is found, the step exits nonzero and calls `ledger-event.py --event escalated`. There is no unsafe branch-HEAD fallback.

**Subtle concern (acceptable):** If `review_1` and `review_2` emit events with the same `pr_number` in the same run (parallel reviewers), both should have identical `head_sha` values (same PR revision), so last-wins produces the correct result. If the PR was pushed between reviews (different SHAs), the re-review would produce a newer entry that supersedes the old one — correct behavior.

**Evidence:** `.github/workflows/pr-ingestion.yml:154-222`

---

### HIGH-4: Denylist check only covers the last commit
**Verdict: FIXED**

The `denylist-check` job now uses `fetch-depth: 0` and `git diff --name-only origin/if/staging...HEAD` (three-dot notation). This correctly computes the symmetric difference from the staging branch tip to HEAD, covering all commits in the cherry-pick branch.

**Evidence:** `.github/workflows/pr-ingestion.yml:328`, `:344`

---

### HIGH-5: Invalid workflow needs references cause silent skips or wrong-branch testing
**Verdict: PARTIAL**

**Fixed in `release.yml`:** All jobs that reference `needs.build.outputs.nuget_version` (`smoke-on-pack`, `perf-on-pack`, `release-notes`, `publish`, `record`) now have `build` in their direct `needs:` list.

**Still broken in `pr-ingestion.yml`:** `build-and-test` references `needs.cherry-pick.outputs.branch` (line 373) but only lists `denylist-check` in its `needs:`. Therefore `needs.cherry-pick.outputs.branch` evaluates to `''` → `gh workflow run build.yml --ref ""` runs the build on the default branch (not the cherry-pick branch).

**Evidence:** `.github/workflows/pr-ingestion.yml:358-384` (build-and-test), `:373` (empty branch ref)

---

### HIGH-6: Build run selection is a race condition
**Verdict: STILL-BROKEN / NEW-ISSUE**

The fix replaces the `sleep 10 + gh run list --limit 1` approach with:
```
RUN_ID=$(gh workflow run build.yml --ref "$BRANCH" --repo "${{ github.repository }}" \
  --json | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
```

**Problem:** `gh workflow run` dispatches the workflow asynchronously and does not return JSON containing a run ID. The `gh workflow run` subcommand's `--json` flag (if accepted at all) outputs the *request body*, not the run metadata. The response does not contain a field named `'id'`. As a result:
- `python3 -c "... print(json.load(sys.stdin)['id'])"` will raise `KeyError` or fail on non-JSON input → `RUN_ID` is empty.
- `if [ -z "$RUN_ID" ]; then echo "::error::..."; exit 1; fi` → the step **always exits 1**.
- `build-and-test` always fails, blocking `perf-gate` and `open-pr`.

This is a **new issue** introduced by the fix: the old approach (wrong run) was a race condition that sometimes passed. The new approach always fails.

**Evidence:** `.github/workflows/pr-ingestion.yml:371-384`

**Recommendation:** Use the GitHub REST API to get the run ID after dispatch, e.g.:
```bash
gh workflow run build.yml --ref "$BRANCH"
sleep 3  # allow GH to register the run
RUN_ID=$(gh run list --workflow=build.yml --branch="$BRANCH" \
  --event=workflow_dispatch --limit=1 --json databaseId \
  -q '.[0].databaseId')
```
Then `gh run watch "$RUN_ID" --exit-status`.

---

### HIGH-7: Release can publish from an unsigned or unreachable-from-release tag
**Verdict: FIXED**

- Tag signature check now uses `git tag -v "$TAG" || { echo "::error::..."; exit 1; }` — fatal on unsigned tags.
- Reachability check uses `git merge-base --is-ancestor "$TAG" origin/if/release/10.0` — correct branch.
- Tag format validation (`LOW-3`) added: `^if-10\.0\..+` rejects obvious non-conforming tags.

**Minor note on LOW-3 regex** (see LOW-3 finding): the current regex is weaker than the recommended `^if-10\.0\.[0-9]+.*$` but sufficient for security purposes because `$TAG` is used only in quoted git commands.

**Evidence:** `.github/workflows/release.yml:64-89`

---

### HIGH-8: MSBuild targets unconditionally remove WPF DLLs on all platforms
**Verdict: FIXED**

`RemoveRuntimeWpfAssets` target now has `Condition="'$(RuntimeIdentifier)' == 'win-x64'"`. A new `ErrorIfUnsupportedRid` target fires `BeforeTargets="RemoveRuntimeWpfAssets;InjectIfWpfAssemblies"` and emits a build `<Error>` for any non-empty non-`win-x64` RID. When `$(RuntimeIdentifier)` is empty (framework-dependent publish), the error also fires. `OverwriteReadOnlyFiles="true"` has been removed from all `<Copy>` tasks (LOW-2 fix).

**Evidence:** `packaging/InitialForce.WPF/buildTransitive/InitialForce.WPF.targets:37-41`, `:46-48`

---

### MED-1: Performance gate has three correctness holes
**Verdict: FIXED**

- `compute_delta_pct`: when `baseline == 0` and `current > 0`, now returns `float('inf')` → always fails threshold check.
- New scenarios (in current but absent from baseline): now produce a `warning` result with `reason="no_baseline"` (or `fail` with `--strict-new-scenarios`). No longer silently skipped.
- Empty results list: `aggregate_status([])` now returns `"fail"` with `reason="empty_comparison"`.

**Evidence:** `tools/check-regression.py:152-164`, `:196-230`, `:260-275`

---

### MED-2: Build workflow run selection race (duplicate of HIGH-6)
**Verdict: STILL-BROKEN** (same root cause as HIGH-6 above)

---

### MED-3: Release provenance: tag reachability wrong branch, no package checksum
**Verdict: PARTIAL**

- Reachability now checks `if/release/10.0` → FIXED.
- SHA256 cross-check step added (`"MED-3 SHA256 cross-check"`). However, the step only **records** hashes (`sha256sum ... | tee /tmp/publish-sha256.txt`) and verifies no zero-byte files. It does not cross-check downloaded hashes against build-time hashes from a separate artifact. The comment acknowledges this: "Phase-2: integrate SLSA provenance." No actual integrity verification is performed between build and publish artifacts.

**Evidence:** `.github/workflows/release.yml:361-377`

---

### MED-4: Prompt/procedure schema drift — cherry-pick.md shows stale ledger CLI
**Verdict: FIXED**

All `ledger-event.py` invocations in `.if-fork/prompts/cherry-pick.md` now use the correct CLI: `--event`, `--pr-number`, `--head-sha`, `--actor`, `--details-json`. No `--details` or `--actor-run-url` usage.

**Evidence:** `.if-fork/prompts/cherry-pick.md:71-150`

---

### MED-5: ledger-validate.py GPG correlation by index is fragile
**Verdict: FIXED**

`ledger-validate.py` now uses `_find_commit_for_line_hash(line_hash)` which runs `git log -S <line_hash>` (pickaxe search) to find the commit that introduced the specific line content. This is robust to squashes and rebases. `ledger-event.py` embeds a `Ledger-Line-Hash: <hash>` trailer in each commit message for forward-lookability.

**Evidence:** `tools/ledger-validate.py:93-119`, `tools/ledger-event.py:136-152`

---

### MED-6: Auto-merge condition bypasses autonomy-check
**Verdict: PARTIAL**

The auto-merge step now uses `if: vars.IF_AUTOMERGE_FROZEN != 'true'`. When unset, `'' != 'true'` is `true` → auto-merge enabled (correct default). The fix resolves the unset-variable bug.

The second half of the recommendation (route through autonomy-check output) was not implemented. The step still directly reads the repo variable rather than checking `needs.gate.outputs`. This is acceptable given that `open-pr` is already downstream of the gate chain, but it means the auto-merge step is not re-validated if the gate somehow changes state between job start and step execution.

**Evidence:** `.github/workflows/pr-ingestion.yml:509-510`

---

### LOW-1: VALID_EVENTS duplicated in ledger-validate.py
**Verdict: FIXED**

`tools/ledger_schema.py` created as the single source of truth. Both `ledger-event.py` and `ledger-validate.py` import `VALID_EVENTS` from it. No more duplication.

**Evidence:** `tools/ledger_schema.py`, `tools/ledger-event.py:32`, `tools/ledger-validate.py:40`

---

### LOW-2: OverwriteReadOnlyFiles=true silently overwrites protected assemblies
**Verdict: FIXED**

`OverwriteReadOnlyFiles="true"` removed from all `<Copy>` tasks in `InitialForce.WPF.targets`.

**Evidence:** `packaging/InitialForce.WPF/buildTransitive/InitialForce.WPF.targets` (no OverwriteReadOnlyFiles attribute found)

---

### LOW-3: Release tag format not validated
**Verdict: PARTIAL**

Regex added: `if [[ ! "$TAG" =~ ^if-10\.0\..+ ]]; then exit 1; fi`. This prevents obvious injection (`../evil`) and enforces the `if-10.0.` prefix.

The recommended pattern `^if-10\.0\.[0-9]+.*$` was not implemented. The current regex allows `if-10.0.` + any single non-numeric character (e.g., `if-10.0.x-release`). Given the tag is only used in quoted git commands, the security risk is low. However, a tighter regex would prevent accidental misuse during manual `workflow_dispatch`.

**Evidence:** `.github/workflows/release.yml:69-72`

---

### LOW-4: Token caps in config.yaml are not enforced
**Verdict: STILL-NOT-FIXED**

`claude_limits.daily_token_cap_usd` and `monthly_token_cap_usd` remain in `.if-fork/config.yaml` but no workflow reads or enforces them. No pre-flight check queries the Anthropic API for usage. No circuit breaker exists.

**Evidence:** `.if-fork/config.yaml:119-125`, (no enforcement found in any `.github/workflows/*.yml`)

---

## Top-3 Most Critical Remaining Issues

### 1. CRIT-1 (PARTIAL): Entire ingestion pipeline dead after pre-flight
**File:Line:** `.github/workflows/pr-ingestion.yml:126-454`

Every job after `pre-flight` references `needs.gate.outputs.proceed` but does not list `gate` in its direct `needs:`. GitHub Actions does not propagate outputs transitively. All conditions evaluate to `'' == 'true'` → `false` → **permanently skipped**. The ingestion pipeline effectively ends at pre-flight. No cherry-pick, no build, no PR, no auto-merge will ever run.

**Fix:** Add `gate` to the `needs:` list of `sha-smuggling-check` (and either propagate via output through each job or add `gate` to all downstream jobs). The simplest correct fix: each job that checks `needs.gate.outputs.proceed` must also have `gate` in its direct `needs:`.

### 2. CRIT-2 (PARTIAL): Secondary workflow ledger writes still crash
**File:Line:** `.github/workflows/nightly-rebase.yml:206,303`, `.github/workflows/pr-discovery.yml:116`, `.github/workflows/claude-on-failure.yml:236`

Three workflows still use `--details` (old arg), `--actor-run-url` (old arg), and `--batch-file` (not implemented), while missing `--pr-number`, `--head-sha`, `--actor`, `--details-json`. Additionally, `rebase_failed`, `failure_analyzed`, `pre_flight_failed`, and `review_single_path_warning` are absent from `VALID_EVENTS`. Every ledger write in these workflows will crash with argparse errors or event-validation errors.

**Fix:** Audit all `ledger-event.py` invocations in every workflow (not just the 3 main ones). Update with correct args. Add the 4 missing event types to `ledger_schema.py`.

### 3. HIGH-6 (STILL-BROKEN/NEW-ISSUE): `build-and-test` always fails due to `gh workflow run --json`
**File:Line:** `.github/workflows/pr-ingestion.yml:371-384`

`gh workflow run` does not output JSON containing a run ID. `RUN_ID` will always be empty → `exit 1` guard triggers → `build-and-test` always fails → `perf-gate` and `open-pr` never run. The fixup replaced a race condition with a deterministic failure. See fix recommendation in HIGH-6 section above.

---

*Audit conducted read-only. No files modified. Methodology: static code analysis of workflow YAML, Python tools, MSBuild targets, and prompt files.*
