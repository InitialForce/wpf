# Workflow Correctness Audit — Round 1, Agent 2

**Scope:** `.github/workflows/*.yml` (11 files)
**Lens:** Needs/outputs/conditions integrity, cross-workflow event matching, action pinning,
vars/secrets, permissions, concurrency groups
**Tool:** actionlint not available on this system — manual YAML inspection + Python parsing used.

---

## Executive Summary

The workflow set is architecturally sound but contains **one CRITICAL defect that completely
breaks the automated review pipeline**, six HIGH-severity issues that cause jobs to silently
skip or workflows to never trigger, and several MEDIUM/LOW issues around permission hygiene,
missing local actions, and shell-variable scoping. The most urgent fix is in `pr-discovery.yml`
where the dispatched event name (`review-batch`) does not match what `pr-review.yml` listens for
(`pr-discovered`), meaning the automated discovery → review → ingestion pipeline never fires.
Additionally, `pr-ingestion.yml` has a pervasive pattern where six out of eight jobs reference
`needs.gate.outputs.proceed` in their `if:` conditions but do not include `gate` in their `needs:`
array — in GitHub Actions, this silently evaluates to an empty string (`''`), so the condition
`'' == 'true'` is false and all six jobs are skipped. The `nightly-rebase.yml` verify job
references a local action (`.github/actions/build-smoke-harness`) that does not exist in the
repository. `weekly-differential.yml` calls the autonomy-check reusable workflow without
supplying the required `requested_action` input. Seven workflows carry `id-token: write` at the
workflow level with no apparent OIDC consumer.

---

## Issues by Workflow

### 1. `pr-discovery.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| D-1 | **CRIT** | 132 | `event_type=review-batch` dispatched, but `pr-review.yml` listens for `types: [pr-discovered]`. The event is silently discarded; the automated review pipeline never triggers. | Change to `event_type=pr-discovered` OR add `review-batch` to `pr-review.yml` types. Also note the payload only contains `batch_date`, not `pr_number`/`head_sha` — `pr-review.yml` expects those. The discovery → review integration must dispatch one event per discovered PR with its number and SHA. |
| D-2 | **HIGH** | 129 | `if: needs.discover.outputs.batch_count > 0` — `batch_count` is a string output in GH Actions expressions; numeric comparison against `0` relies on implicit coercion. If `batch_count` is empty or non-numeric, the condition evaluates unexpectedly. | Use `if: fromJSON(needs.discover.outputs.batch_count || '0') > 0` or compare as string: `if: needs.discover.outputs.batch_count != '' && needs.discover.outputs.batch_count != '0'`. |
| D-3 | **MED** | 82 | `record` job has no `if:` condition, so it runs even when `gate` blocked `discover` (which is then `skipped`). The job will attempt to run Python tooling / ledger write with no discovered data. | Add `if: needs.discover.result == 'success'` to `record`. |

---

### 2. `pr-ingestion.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| I-1 | **HIGH** | 127,230,312,359,393,454 | Six jobs (`sha-smuggling-check`, `cherry-pick`, `denylist-check`, `build-and-test`, `perf-gate`, `open-pr`) reference `needs.gate.outputs.proceed` in their `if:` expressions but do **not** include `gate` in their `needs:` array. GitHub Actions only makes `needs.X.outputs` available when `X` is in the job's own `needs:`. The expression evaluates to `''`, which is `!= 'true'`, so all six jobs are **silently skipped** whenever the gate is actually blocked. Conversely, when gate passes, the jobs are still skipped because the output is inaccessible. In practice the entire ingestion pipeline after `pre-flight` is dead unless `gate` is added to each job's `needs:`. | Add `gate` to the `needs:` array of every job in the chain that uses `needs.gate.outputs.proceed`: `sha-smuggling-check`, `cherry-pick`, `denylist-check`, `build-and-test`, `perf-gate`, `open-pr`. Pattern: `needs: [gate, <existing-need>]`. |
| I-2 | **HIGH** | 373-383 | `build-and-test` runs `gh workflow run build.yml --json` — the `--json` flag does not exist on `gh workflow run` (it is only available on `gh run list`, `gh run view`, etc.). The command will fail, making it impossible to capture the run ID and watch the build. | Remove `--json` and use `gh run list --workflow=build.yml --branch "$BRANCH" --limit 1 --json databaseId --jq '.[0].databaseId'` (with a short sleep to let the run register) to retrieve the run ID after triggering. |
| I-3 | **MED** | 127 | `sha-smuggling-check` uses `needs.gate.outputs.proceed == 'true' && needs.pre-flight.outputs.action == 'apply'` — valid once I-1 is fixed, but currently `pre-flight` IS in `needs`, while `gate` is not. Mixed state makes it harder to reason about. | After fixing I-1, verify both outputs are accessible. |

---

### 3. `nightly-rebase.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| N-1 | **HIGH** | 243 | `verify` job uses `uses: ./.github/actions/build-smoke-harness` — that local composite action does not exist in the repository (`.github/actions/` directory is absent). The job will fail at startup before any step runs. | Either create the local composite action or replace with inline steps that perform the build and smoke check. |
| N-2 | **LOW** | 218 | The "Fail job when rebase failed" step references shell variable `$MAX_RETRIES` but this is a separate step from where the variable was defined. Each step runs in a fresh shell; `MAX_RETRIES` is not exported to `GITHUB_ENV`. The error message will display a blank value. | Either export via `echo "MAX_RETRIES=3" >> "$GITHUB_ENV"` in the rebase step, or hardcode the literal `3` in the error message. |

---

### 4. `weekly-differential.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| W-1 | **HIGH** | 26-27 | `gate` job calls `autonomy-check.yml` without any `with:` block. The `requested_action` input is **required** (`required: true`, no default). GitHub Actions will fail to start the reusable workflow, causing the entire weekly-differential run to fail immediately. | Add `with: requested_action: discovery-scan` (or a new action name if preferred; it must be in autonomy-check's known-actions list). |
| W-2 | **MED** | 10 | Concurrency group is `weekly-diff-${{ github.event.schedule }}`. On `workflow_dispatch`, `github.event.schedule` is empty, making the group `weekly-diff-` — a shared group for all manual runs, which could incorrectly cancel in-flight runs if `cancel-in-progress` were ever set to `true`. | Use `weekly-diff-${{ github.event.schedule || github.run_id }}` so manual runs each get a unique group. |
| W-3 | **LOW** | 36-37 | `build-upstream-clean` and `build-fork` use `if: needs.gate.result == 'success'` — this is correct (using `result` not `outputs.proceed`) and would work, but it is inconsistent with the rest of the codebase which checks `needs.gate.outputs.proceed == 'true'`. If gate is blocked but doesn't fail (it always exits 0, emitting `proceed=false`), `result == 'success'` would still be true and downstream jobs would run. | Change to `if: needs.gate.outputs.proceed == 'true'` consistent with all other callers. This requires adding `gate` to `needs:` of these jobs too (they already have it). |

---

### 5. `pr-review.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| R-1 | **MED** | 77,154 | `review-1` and `review-2` declare `outputs: verdict_file: /tmp/review-1.json` — these are **hardcoded literal string values**, not `${{ steps.X.outputs.Y }}` expressions. The output value is always the static string `/tmp/review-1.json` and is not derived from any step. If anything references `needs.review-1.outputs.verdict_file`, it gets only that literal. No downstream job currently uses these outputs (the artifact pattern is used instead), so this is misleading dead metadata. | Either remove the outputs declarations or wire them to an actual step output (e.g., `${{ steps.review.outputs.verdict_file }}`). |
| R-2 | **MED** | 63-64 | `bypass_for_human_dispatch` passes `${{ github.event_name == 'workflow_dispatch' }}` — this is an expression that evaluates to the string `"true"` or `"false"`. The reusable workflow input is typed `boolean`. GH Actions does support string-to-bool coercion here, but this is fragile and may cause unexpected behavior in edge cases. | Use explicit boolean: `bypass_for_human_dispatch: ${{ github.event_name == 'workflow_dispatch' }}` — this is actually fine in modern GH Actions. Mark as LOW risk. |

---

### 6. `upstream-stable-adoption.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| U-1 | **MED** | 201-209 | `trigger-rebase` dispatches `event_type=nightly-rebase` but `nightly-rebase.yml` has no `repository_dispatch` trigger at all (only `schedule` and `workflow_dispatch`). The dispatch is silently dropped; the rebase is never triggered. | Add `repository_dispatch: types: [nightly-rebase]` to `nightly-rebase.yml`, OR change this step to `gh workflow run nightly-rebase.yml` (workflow_dispatch). |

---

### 7. `claude-on-failure.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| C-1 | **LOW** | 6-13 | `Weekly Differential` is not in the `workflow_run: workflows:` list. If the weekly differential fails, no failure-analysis issue is opened. | Add `- Weekly Differential` to the list. |
| C-2 | **LOW** | 22 | `id-token: write` at workflow level. No OIDC-consuming action is used; all auth is done via the GH App token. This excess permission violates least-privilege. | Remove `id-token: write` unless OIDC is intentionally planned. |

---

### 8. `build.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| B-1 | **LOW** | 26 | `id-token: write` at workflow level with no OIDC consumer. | Remove unless OIDC is planned. |
| B-2 | **LOW** | 95-102 | `actionlint` step has `continue-on-error: true` and only runs if actionlint is installed — it will silently pass in CI if actionlint is absent. | Either pin actionlint as a mandatory install step or remove `continue-on-error` so the job fails visibly if actionlint is missing. |

---

### 9. `release.yml`

| # | Severity | Line | Description | Recommended Fix |
|---|----------|------|-------------|-----------------|
| Re-1 | **MED** | 96-99 | `build` job uses `if: needs.verify-tag.result == 'success'` rather than `if: needs.gate.outputs.proceed == 'true'`. This is transitive-safe (verify-tag skips when gate blocks), but it silently ignores the gate's autonomy logic — if gate returned `proceed=false` but somehow verify-tag still ran (impossible in current design but a risk if the chain changes), the build would proceed. | Change to `if: needs.gate.outputs.proceed == 'true' && needs.verify-tag.result == 'success'` and add `gate` to `needs:`. |
| Re-2 | **LOW** | 22 | `id-token: write` at workflow level. No OIDC consumer visible. | Remove unless OIDC is planned. |

---

### 10. `pr-discovery.yml`, `pr-review.yml`, `pr-ingestion.yml`, `weekly-differential.yml`

| # | Severity | Lines | Description | Recommended Fix |
|---|----------|-------|-------------|-----------------|
| X-1 | **LOW** | all | `id-token: write` at workflow level across multiple workflows with no actual OIDC action. Excess token scope. | Audit and remove from all workflows that do not use OIDC. The anthropics/claude-code-action does not require id-token. |

---

### 11. `autonomy-check.yml`

No issues found. The reusable workflow is well-structured: inputs are correctly typed, outputs are
properly wired to step outputs, permissions are minimal (`contents: read`), and the gate logic is
clear and correct.

---

### 12. `test-autonomy-check.yml`

No issues found. Correctly calls the reusable workflow with all required inputs.

---

## Cross-Workflow Event Chain Verification

| From | Event Dispatched | To (Listener) | Verdict |
|------|-----------------|---------------|---------|
| `pr-discovery.yml` record job | `review-batch` | `pr-review.yml` listens for `pr-discovered` | **BROKEN** (CRIT D-1) |
| `pr-review.yml` merge-verdict (dispatch-approved.py) | `pr-ingestion-requested` | `pr-ingestion.yml` listens for `pr-ingestion-requested` | OK |
| `upstream-stable-adoption.yml` trigger-rebase | `nightly-rebase` | `nightly-rebase.yml` has no `repository_dispatch` trigger | **BROKEN** (MED U-1) |
| `pr-review.yml` → `pr-ingestion.yml` payload | `client_payload.pr_number`, `client_payload.head_sha` | `pr-ingestion.yml` reads `github.event.client_payload.pr_number` | OK (via dispatch-approved.py) |

---

## Action Pinning

All external action references (`actions/checkout`, `actions/setup-python`, `actions/cache`,
`actions/upload-artifact`, `actions/download-artifact`, `actions/setup-dotnet`,
`tibdex/github-app-token`, `anthropics/claude-code-action`) use major-version tags (`@v4`, `@v5`,
`@v2`, `@v1`). No `@main` or `@sha` pinning issues found. For supply-chain hardening, SHA pinning
would be preferred over major-version tags, but this is a LOW/cosmetic concern not a functional
defect.

---

## Secrets and Vars

| Secret/Var | Used In | Status |
|------------|---------|--------|
| `secrets.GH_APP_ID` | All workflows except build, weekly-diff | Required — must be set in `bot-credentials` environment |
| `secrets.GH_APP_PRIVATE_KEY` | Same | Required |
| `secrets.ANTHROPIC_API_KEY` | claude-on-failure, pr-discovery, pr-review, pr-ingestion, release, nightly-rebase | Required |
| `secrets.NUGET_FEED_PAT` | release.yml publish step | Required |
| `vars.IF_AUTONOMY_ENABLED` | autonomy-check.yml | Required — gate always blocks if unset |
| `vars.IF_AUTOMERGE_FROZEN` | autonomy-check.yml, pr-ingestion.yml open-pr step | Required for auto-merge logic |
| `vars.IF_REVIEW_DOUBLE_REQUIRED` | pr-review.yml | Optional — default behavior is two-reviewer |

No obvious missing secrets. The design of using the `bot-credentials` environment as the gate for
secret access is appropriate.

---

## Permissions Audit

| Workflow | Declared | Excess | Missing |
|----------|----------|--------|---------|
| `autonomy-check.yml` | `contents: read` | None | None |
| `build.yml` | `contents: read, pull-requests: read, checks: write, id-token: write` | `id-token: write` (no OIDC) | None |
| `claude-on-failure.yml` | `actions: read, contents: read, issues: write, id-token: write` | `id-token: write` | None |
| `nightly-rebase.yml` | `contents: write, pull-requests: write, issues: write` | None | None |
| `pr-discovery.yml` | `contents: read, pull-requests: read, id-token: write` | `id-token: write` | None |
| `pr-ingestion.yml` | `contents: write, pull-requests: write, issues: write, id-token: write` | `id-token: write` | None |
| `pr-review.yml` | `contents: read, pull-requests: read, issues: write, id-token: write` | `id-token: write` | None |
| `release.yml` | `contents: write, packages: write, id-token: write` | `id-token: write` (possibly — depends on if OIDC push to packages is planned) | None |
| `upstream-stable-adoption.yml` | `contents: write, issues: write, pull-requests: write` | None | None |
| `weekly-differential.yml` | `contents: read, issues: write, id-token: write` | `id-token: write` | None |

---

## Priority Fix Order

1. **CRIT** — Fix `pr-discovery.yml` event_type (`review-batch` → `pr-discovered`) and add pr_number/head_sha to payload or rethink the dispatch architecture (one event per PR, not one batch event).
2. **HIGH** — Add `gate` to `needs:` of all six downstream jobs in `pr-ingestion.yml`.
3. **HIGH** — Fix `weekly-differential.yml`: add `with: requested_action: discovery-scan` to the autonomy-check call.
4. **HIGH** — Fix `nightly-rebase.yml` missing local action `.github/actions/build-smoke-harness`.
5. **HIGH** — Fix `pr-ingestion.yml` build-and-test: `gh workflow run --json` is not a valid flag; replace with correct run-ID capture pattern.
6. **MED** — Add `repository_dispatch: types: [nightly-rebase]` to `nightly-rebase.yml` so `upstream-stable-adoption.yml` can trigger it.
7. **MED** — Fix `weekly-differential.yml` concurrency group for `workflow_dispatch`.
8. **MED** — Add `Weekly Differential` to `claude-on-failure.yml` monitored workflow list.
9. **LOW** — Remove excess `id-token: write` from all workflows that do not use OIDC.
10. **LOW** — Fix `nightly-rebase.yml` `$MAX_RETRIES` scope in separate step.
