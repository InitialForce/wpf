# Operability Audit — Round 1 / Agent 5
**Lens:** Operator runbook executability, recovery paths, backup operator readiness
**Date:** 2026-04-28
**Reviewer:** Agent 5 (read-only)
**Files examined:** `docs/operator-runbook.md`, `docs/BOOTSTRAP_STATUS.md`, `docs/KNOWN_RISKS.md`, `docs/risk-register.md`, `docs/DECISION_LOG.md`, `docs/known-limitations.md`, `.if-fork/config.yaml`, `.github/workflows/autonomy-check.yml`, `.github/workflows/nightly-rebase.yml`, `.github/workflows/release.yml`, `.github/workflows/pr-discovery.yml`, `tools/ledger-validate.py`, `tools/ledger-event.py`

---

## Summary of Findings

| ID | Area | Severity | One-line description |
|----|------|----------|----------------------|
| OP-1 | I-1 NuGet unlist | CRIT | `gh api DELETE` for GitHub Packages requires `delete:packages` scope; `NUGET_FEED_PAT` likely lacks it and there is no documented PAT scope check |
| OP-2 | I-3 conflict artifacts | HIGH | `gh run download --name conflict-artifacts` references an artifact name (`conflict-artifacts`) that `nightly-rebase.yml` never uploads — operator will get a 404 |
| OP-3 | I-10 ledger tampering | HIGH | `git bisect run python tools/ledger-validate.py .if-fork/patch-ledger.jsonl` — the bisect invocation passes a positional path arg but `ledger-validate.py` only accepts `--ledger-path`; bisect will fail on every step |
| OP-4 | I-7 CVE cherry-pick | HIGH | `pr-review.yml` (autonomy gate) will block the cherry-pick branch because `IF_AUTOMERGE_FROZEN` will be `true` during a live CVE incident; runbook does not say to temporarily clear the freeze or bypass the gate |
| OP-5 | Broken-ledger recovery | HIGH | No documented recovery path for the case where all 3 `ledger-event.py` push retries fail simultaneously (CRIT-3 acknowledged in code); `patch-ledger.jsonl` is left in an inconsistent commit-vs-file state |
| OP-6 | Daily checklist label filter | MED | `gh issue list --label "security,review-disagreement,rebase-conflict"` — comma-separated multi-label in `--label` applies AND logic in `gh`; an issue with only `security` will not appear; operator must use separate calls or `--label security --label review-disagreement` etc. |
| OP-7 | Return-from-vacation label filter | MED | Same AND-label problem: `--label "security,review-disagreement,rebase-conflict"` on line 187 of runbook will silently miss single-label issues |
| OP-8 | I-2 force-push recovery | MED | Recovery command `git push if HEAD:refs/heads/if/release/10.0 --force` requires the operator to have force-push permission on the `if` remote; branch protection ("no force-push from non-bot accounts") documented in RISK-004 means this command will be rejected for a human operator |
| OP-9 | `--strict-signature` not in any CI workflow | MED | `ledger-validate.py --strict-signature` is documented in "Check patch-ledger integrity manually" but is never called from any workflow or cron; proactive detection of unsigned ledger commits does not occur automatically |
| OP-10 | Backup operator / credential vault | MED | Credentials are stored in GitHub Environments (secrets UI) with no documented vault or break-glass procedure; if Oystein loses GitHub org-owner access, there is no named second operator and no documented escalation path |
| OP-11 | `audit` branch in `git log audit` syntax | MED | `git log audit --oneline` relies on `audit` resolving as a branch ref; if the orphan branch is not locally fetched, the command silently fails or errors; runbook does not include a `git fetch` step before querying the audit branch |
| OP-12 | Monthly: `gh api /orgs/.../billing/actions` | MED | This endpoint requires `read:org` OAuth scope; GitHub App tokens do not have org billing scope by default; the command will return a 403 if run with the bot token or a narrow PAT |
| OP-13 | I-4 bisect test command | MED | `dotnet test test/InitialForce.WpfSmoke/ -c Release` in the I-4 bisect requires a Windows runner; running on Linux (which is the operator's dev environment per env hints) will fail because WPF tests are Windows-only |
| OP-14 | BOOTSTRAP_STATUS hand-off completeness | MED | Hand-off item `wpf-29a` ("Operator runbook validated by Oystein") has no documented acceptance criterion or checklist in the runbook; there is no sign-off field or completion test |
| OP-15 | I-1 consumer notification | LOW | Bad-NuGet incident remediation has no documented consumer notification step (email / Slack / GitHub Issue to SC team); remediation ends at "pin SC to known-good" with no notification SLA |
| OP-16 | `perf/series.jsonl` path mismatch | LOW | Dashboard reference says `perf/series.jsonl` but the workflow writes perf results to `/tmp/perf-result.json` and uploads as artifact; no step in any workflow appends to `perf/series.jsonl` on the branch |
| OP-17 | `git show audit:<run-id>.json` syntax | LOW | `git show audit:<path>` is correct git tree-object syntax only if `audit` is a valid branch that has been fetched; doc does not say to do `git fetch origin audit` first |
| OP-18 | Weekly: `--label "differential,weekly-report"` | LOW | Same AND-label problem; if the bot opens issues with only one of those labels the operator will see no results |
| OP-19 | Daily load estimate realism | LOW | Stated "6 predictable hours/month" assumes a mature steady state where autonomy is stable; the 223-PR bulk-review backlog (wpf-2hh) will consume significantly more on first run; no first-month estimate is given |
| OP-20 | Onboarding a backup operator | LOW | No onboarding checklist or estimated ramp time for a second engineer; KNOWN_RISKS RISK-012 acknowledges the gap but the runbook does not reference any onboarding procedure even in outline form |

---

## Detailed Findings

### OP-1 — CRIT: I-1 NuGet unlist missing PAT scope check

**Location:** `docs/operator-runbook.md` §I-1, step "Unlist from GitHub Packages"

**Issue:** The remediation step calls:
```bash
gh api -X DELETE /orgs/InitialForce/packages/nuget/InitialForce.WPF/versions/$VERSION_ID
```
Deleting a package version via the GitHub REST API requires a token with `delete:packages` scope. The `NUGET_FEED_PAT` stored in the `wpf-nuget-publish` environment is documented as having `write:packages` scope (needed for `dotnet nuget push`). These are different scopes. `write:packages` does not grant `delete:packages`.

**Impact:** During a live production incident, the unlist step will return HTTP 403, the bad package will remain published, and the MTTR target of 2 hours will be violated. The operator has no documented fallback (e.g., use the nuget.org web UI or contact GitHub support).

**Evidence:** `tools/ledger-event.py` comments say "HUMAN ONLY — requires NUGET_FEED_PAT" but do not specify which scopes are needed. The `docs/known-limitations.md` PAT rotation section only mentions `read:packages` for SC's restore PAT; the publish PAT scope is not documented.

**Required fix (do not implement — flag only):** Document the exact PAT scope required (`delete:packages`), verify the secret in the `wpf-nuget-publish` environment has it, or provide the nuget.org web-UI fallback URL.

---

### OP-2 — HIGH: I-3 references artifact name that is never uploaded

**Location:** `docs/operator-runbook.md` §I-3 Diagnosis

**Issue:** The runbook says:
```bash
gh run download <run-id> --name conflict-artifacts --dir /tmp/conflicts
```
Examining `nightly-rebase.yml`, the only artifact uploaded during a failed rebase is `discovered-batch.json` via the `pr-discovery` flow; `nightly-rebase.yml` itself never uploads an artifact named `conflict-artifacts`. The conflict file list is written to `/tmp/conflict-files.txt` inside the runner and is not persisted. The download command will produce:
```
no artifacts named "conflict-artifacts" found for run <run-id>
```

**Impact:** Operator cannot retrieve conflict artifacts via the documented procedure; diagnosis requires reading the raw log via `gh run view --log`.

---

### OP-3 — HIGH: I-10 `git bisect run` passes positional arg that ledger-validate.py does not accept

**Location:** `docs/operator-runbook.md` §I-10 Bisect

**Issue:** The runbook says:
```bash
git bisect run python tools/ledger-validate.py .if-fork/patch-ledger.jsonl
```
Reading `tools/ledger-validate.py`, the CLI uses `argparse` with `--ledger-path` as the named flag and `DEFAULT_LEDGER_PATH` as default. There is no positional argument. The invocation above passes `.if-fork/patch-ledger.jsonl` as an unrecognised positional argument; argparse will either ignore it (using the default) or error out with "unrecognised arguments".

The correct invocation is:
```bash
git bisect run python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl
```
Since the default is already `.if-fork/patch-ledger.jsonl`, this happens to work at the default path — but if the ledger is at a non-default path, every bisect step will silently validate the wrong file. The risk is that a tampering commit is not found because bisect always validates the empty/default file.

---

### OP-4 — HIGH: I-7 CVE response blocked by `IF_AUTOMERGE_FROZEN`

**Location:** `docs/operator-runbook.md` §I-7

**Issue:** I-7 instructs the operator to create a `claude/security-CVE-YYYY-NNNNN` branch and open a PR. The `pr-ingestion.yml` workflow (which would process this PR) calls `autonomy-check.yml` with `requested_action: auto-merge`. If the operator set `IF_AUTOMERGE_FROZEN=true` to investigate a concurrent regression (I-4, which runs before the CVE is confirmed), the autonomy gate will block the CVE PR from auto-merging.

The runbook does not tell the operator to:
1. Check the freeze state before starting I-7, or
2. Temporarily clear the freeze for the security PR using `bypass_for_human_dispatch: true`, or
3. Merge the security PR manually via `gh pr merge`.

MTTR for the CVE (24h) can be violated if the operator does not notice the frozen state.

---

### OP-5 — HIGH: No documented recovery for ledger in inconsistent state after all push retries exhausted

**Location:** `docs/operator-runbook.md` §Check patch-ledger integrity; `tools/ledger-event.py` §_push_with_retry

**Issue:** `ledger-event.py` implements 3 retries for non-fast-forward push failures (CRIT-3 fix). If all 3 fail (e.g., a sustained network partition or a force-push by another actor that keeps racing), the function calls `die(6, ...)` and exits. At this point the ledger file on disk has had the new line stripped and rewritten but the git commit was not pushed. The local working tree is in an inconsistent state: the last committed line in origin differs from what the local file shows.

The runbook under "Check patch-ledger integrity manually" gives:
```bash
python tools/ledger-validate.py .if-fork/patch-ledger.jsonl
```
But this runs against the local file, not origin. There is no documented procedure to:
- Detect that a push-retry exhaustion happened (the CI log shows it, but there is no issue opened)
- Recover: determine which line was dropped, re-append it correctly, and push manually
- Validate origin's ledger (must clone or `git fetch` then validate `.git/refs/remotes/origin/...`)

`tools/ledger-validate.py --strict-signature` (noted below in OP-9) is also not called in this scenario.

---

### OP-6 / OP-7 — MED: Multi-label `--label` filter applies AND logic

**Location:** `docs/operator-runbook.md` §Daily Checklist step 1, §Return-from-Vacation step 2

**Issue:** Both commands use:
```bash
gh issue list --label "security,review-disagreement,rebase-conflict" ...
```
The `gh issue list --label` flag, when given a comma-separated list, applies AND semantics: it returns only issues that have **all** of the specified labels simultaneously. An issue labelled only `security` (the highest-priority label) will not appear in results.

The correct form for OR semantics requires separate calls or `gh search issues` with `label:` filters.

**Impact:** An operator following the daily checklist may conclude "no flagged issues" when a `security`-only issue is actively open. The 48-hour email oncall trigger ("no human comment after 48 hours") would still fire, but the daily check would not catch it proactively.

---

### OP-8 — MED: I-2 recovery force-push will be rejected for human operators

**Location:** `docs/operator-runbook.md` §I-2

**Issue:** The last-resort recovery command is:
```bash
git push if $GOOD_TAG^{}:refs/heads/if/release/10.0 --force
```
KNOWN_RISKS RISK-004 documents branch protection with "no force-push from non-bot accounts" on `if/release/10.0`. A human operator (Oystein's personal GitHub account) cannot force-push to a protected branch. The bot (GitHub App) could, but the runbook does not describe how to invoke the bot token for this operation.

The `--force-with-lease` variant on the line above (which the runbook does use first) is also inappropriate when the branch tip is literally gone — lease will always fail on a missing ref.

**Correct path:** Temporarily remove branch protection via the GitHub UI before force-pushing, then re-enable it. The runbook mentions "Re-apply branch protection (re-run P0-5 from bootstrap runbook)" at the end but does not say to temporarily lift it first.

---

### OP-9 — MED: `--strict-signature` never called from any automated workflow

**Location:** `tools/ledger-validate.py` §CLI; `docs/operator-runbook.md` §Check patch-ledger integrity manually

**Issue:** `ledger-validate.py --strict-signature` verifies that every ledger line has a corresponding GPG-signed git commit. The manual ops section of the runbook lists this flag but no workflow calls it. The `pr-ingestion.yml`, `nightly-rebase.yml`, and all other workflows call `ledger-validate.py` without `--strict-signature`. Proactive detection of unsigned ledger commits (e.g., if a runner's GPG key silently expired) relies entirely on monthly spot-checks rather than CI.

---

### OP-10 — MED: No credential vault or break-glass procedure for backup operator

**Location:** `docs/operator-runbook.md` §Onboarding; `docs/KNOWN_RISKS.md` RISK-012; `docs/DECISION_LOG.md` DEC-012

**Issue:** The runbook has no "Backup Operator Onboarding" section. RISK-012 and DEC-012 acknowledge the single-key-holder risk and say "revisit when a second engineer joins." However:
- No named break-glass contact exists.
- The GitHub App private key is stored only in the `bot-credentials` GitHub Environment secret — accessible only to Oystein (org owner). There is no offline copy procedure or a secondary account with org-owner access.
- The `NUGET_FEED_PAT` and `ANTHROPIC_API_KEY` are similarly single-holder.
- The runbook return-from-vacation checklist instructs re-enabling autonomy but does not say who to call if Oystein is incapacitated (not just on vacation).

**Impact:** If Oystein is unavailable during a P0 incident (I-1 NuGet unlist, I-8 key compromise), there is no one who can act and no documented path to get access.

---

### OP-11 — MED: `git log audit --oneline` without prior fetch

**Location:** `docs/operator-runbook.md` §Dashboard References; §Monthly Checklist step 2

**Issue:** The `audit` branch is an orphan branch. On a freshly cloned or stale local checkout, `git log audit` will error with "unknown revision 'audit'" unless the branch has been explicitly fetched. The runbook never includes `git fetch origin audit` or `git fetch --all` as a prerequisite step before querying the audit branch. The monthly spot-check relies on this command.

---

### OP-12 — MED: Monthly billing API command likely returns 403

**Location:** `docs/operator-runbook.md` §Monthly Checklist step 1

**Issue:**
```bash
gh api /orgs/InitialForce/settings/billing/actions
```
This endpoint requires the `read:org` scope. The `gh` CLI uses the token from `GH_TOKEN` or the authenticated user. If the operator runs this with `gh auth login` using a fine-grained PAT that does not include `read:org`, the response is 403. The bot GitHub App token also does not have org billing scope by default (billing is not a standard GitHub App permission). This makes the monthly cost review unreliable.

---

### OP-13 — MED: I-4 bisect test requires Windows runner

**Location:** `docs/operator-runbook.md` §I-4

**Issue:** The bisect test command is:
```bash
dotnet test test/InitialForce.WpfSmoke/ -c Release
```
WPF is Windows-only. The `test/InitialForce.WpfSmoke/` harness uses WPF APIs and must run on `windows-latest`. The operator's dev environment (WSL1/Linux per session env) cannot execute this. The runbook does not say to either: (a) run bisect on a Windows machine, or (b) use `gh workflow run` to trigger CI at each bisect step.

---

### OP-14 — MED: Hand-off item `wpf-29a` has no acceptance criterion

**Location:** `docs/BOOTSTRAP_STATUS.md` §What still requires Oystein; `docs/operator-runbook.md` (general)

**Issue:** BOOTSTRAP_STATUS.md lists bead `wpf-29a` as "Operator runbook validated by Oystein" with type "Manual QA." There is no:
- Acceptance criterion specifying what "validated" means (e.g., complete daily + weekly checklist once with live data, verify each `gh` command returns output)
- Sign-off field in the runbook or BOOTSTRAP_STATUS.md
- Completion signal for the autonomous pipeline to detect that validation happened

Until `wpf-29a` is closed, the pipeline should not be considered production-ready, but there is no enforcement.

---

### OP-15 — LOW: I-1 has no consumer notification step

**Location:** `docs/operator-runbook.md` §I-1

**Issue:** The I-1 remediation sequence ends at "Pin SC to last known-good" and "Rebuild from previous good tag." There is no step to notify SC developers or other stakeholders that a bad package was shipped, what the rollback version is, or what the impact assessment is. The MTTR target mentions "SC hotfix" but not how SC learns a hotfix is available.

---

### OP-16 — LOW: `perf/series.jsonl` path referenced in dashboard but never written

**Location:** `docs/operator-runbook.md` §Dashboard References

**Issue:** The dashboard table lists `perf/series.jsonl (last entry = most recent benchmark)` as the perf signal. Examining `release.yml` and `nightly-rebase.yml`, perf results are written to `/tmp/perf-result.json` on the runner and uploaded as a GitHub Actions artifact (`perf-results-<version>`). No workflow appends to `perf/series.jsonl` in the repository. The file likely does not exist on `if/release/10.0`. An operator who runs `git show if/release/10.0:perf/series.jsonl` will get an error.

---

### OP-17 / OP-18 — LOW: Audit branch `git show` and weekly differential label filters

**Location:** `docs/operator-runbook.md` §Monthly Checklist step 2; §Weekly Checklist step 2

**OP-17:** `git show audit:<run-id>.json` requires `audit` to be a locally-known ref. Same issue as OP-11.

**OP-18:** `gh issue list --label "differential,weekly-report"` uses AND logic; will miss issues with only one of these labels.

---

### OP-19 — LOW: Time budget omits first-month bulk-review load

**Location:** `docs/operator-runbook.md` header

**Issue:** The honest load estimate is "6 predictable hours/month plus 2–14 hours interrupt." This is a steady-state estimate. The first operational month includes bulk-processing 223 candidates through `pr-review.yml` (bead `wpf-2hh`), triaging all `review-disagreement` issues to zero (bead `wpf-j79`), and running the first `release.yml` with manual approval (`wpf-2xo`). First-month load is plausibly 30–50 hours, not 8–20. The discrepancy could surprise a new operator.

---

### OP-20 — LOW: No backup operator onboarding checklist even in outline

**Location:** `docs/operator-runbook.md` (absent)

**Issue:** Beyond acknowledging the single-key-holder risk, neither the runbook nor any doc provides:
- An estimated ramp time for a second engineer
- A list of systems they would need access to (GitHub org owner, Anthropic API billing, nuget.org account)
- Whether there is a shared 1Password/Bitwarden vault or the credentials only exist in GitHub Secrets

RISK-012 / DEC-012 say "revisit when a second engineer joins" but the runbook should at minimum contain a placeholder section titled "Backup Operator Onboarding" with the access list, even if incomplete.

---

## Kill-Switch Variable Verification

The runbook references three variables. Cross-referencing against `autonomy-check.yml` and `config.yaml`:

| Variable | Runbook ref | `autonomy-check.yml` reads | `config.yaml` declares | Match? |
|---|---|---|---|---|
| `IF_AUTONOMY_ENABLED` | Yes — gate 1 | `vars.IF_AUTONOMY_ENABLED` | `autonomy_kill_switches.enabled_var` | Yes |
| `IF_AUTOMERGE_FROZEN` | Yes — gate 2 | `vars.IF_AUTOMERGE_FROZEN` | `autonomy_kill_switches.automerge_frozen_var` | Yes |
| `IF_REVIEW_DOUBLE_REQUIRED` | Yes — mentioned as "default true" | Consumed by `pr-review.yml` directly | Not in `config.yaml` `autonomy_kill_switches` | Partial — `pr-review.yml` reads it but it is not in the config schema; if someone looks for it in `config.yaml` they will not find it |

All three variable names are consistent between the runbook prose and the workflow YAML. No name mismatches. `IF_REVIEW_DOUBLE_REQUIRED` is correctly documented as not being enforced by `autonomy-check.yml` (it is a separate check in `pr-review.yml`).

---

## NuGet Rollback Runbook Executability Assessment

The I-1 runbook has three steps. Each is assessed below:

**Step 1 — Unlist via GitHub Packages API:**
- Command is syntactically correct.
- CRIT finding OP-1: `delete:packages` scope is almost certainly not on `NUGET_FEED_PAT`.
- No fallback to the GitHub web UI or support escalation documented.
- **Not reliably executable as written.**

**Step 2 — Pin SC to last known-good in `Directory.Packages.props`:**
- This is a manual file edit in the SC repo, not described in detail.
- Which file, which line, what the version string format is — not shown.
- Requires SC CI to pass after the pin — not mentioned.
- **Executable but underspecified (HIGH).**

**Step 3 — Rebuild from previous good tag:**
```bash
gh workflow run release.yml --repo InitialForce/wpf --ref $GOOD_TAG \
  -f version_override=$GOOD_VERSION
```
Examining `release.yml`: the `workflow_dispatch` inputs include `tag` (not `version_override`). The input name mismatch means this command will silently use the `tag` default (empty) or error. Correct form: `-f tag=$GOOD_TAG`. **Command is wrong (HIGH).**

---

## Broken Ledger Recovery Assessment

**Scenario:** `patch-ledger.jsonl` prev_hash chain is broken (CRIT-3: all 3 push retries fail simultaneously).

**Documented detection:** `tools/ledger-validate.py` CI failure — this runs on PRs. But if the ledger commit was not pushed, there is no PR to trigger CI. The operator must run the validation manually or discover it through a failed subsequent ledger append.

**Proactive CI detection:** `ledger-validate.py` is referenced in the `pr-ingestion.yml` and `nightly-rebase.yml` workflows as a pre-flight, but only on the files present in the checked-out branch. If the bad state is on a temporary branch that was abandoned, the next main-branch workflow will not see it.

**`--strict-signature` invocation:** Never called automatically. Not in any CI workflow. The runbook lists it as a manual operation only. **Proactive detection of unsigned ledger commits does not occur.** (OP-9 above.)

**Recovery instructions in runbook:** None. The "Check patch-ledger integrity manually" section shows how to detect a problem, not how to fix it. There is no documented procedure for re-threading the hash chain after a partial failure.

---

## Daily/Weekly Ops Budget Assessment

| Cadence | Documented estimate | Plausibility assessment |
|---|---|---|
| Daily | 5–15 min | Reasonable for steady state; assumes no flagged issues. |
| Weekly (rebase week) | 45–60 min | Plausible for a clean rebase; underestimates by 2–3x for a conflict-heavy week |
| Monthly | 45 min–2 h | Reasonable; does not include first-month bulk-review cost |
| Interrupt (incidents) | 2–14 h | Reasonable range; I-1 alone could consume 4 h if the unlist step fails (OP-1) |
| **First-month total** | Not documented | Estimated 30–50 h (223-PR bulk review, initial triage, first release) |

**Realism for a single operator:** Steady-state is achievable for Oystein. First-month is a material time commitment that should be budgeted explicitly.

---

## Phase-0 Bootstrap Checklist vs. Runbook Cross-Reference

17 hand-off items from `BOOTSTRAP_STATUS.md` checked against runbook coverage:

| Bead | Type | Covered in runbook? | Gap |
|---|---|---|---|
| wpf-3sm | GitHub admin (create repo) | No — bootstrap only | Acceptable: one-time setup |
| wpf-13l | GitHub admin (create App) | No — bootstrap only | Acceptable |
| wpf-238 | Branch protection | Partial — I-2 says "re-run P0-5" without specifying the exact commands | MED |
| wpf-2lc | Environments | No — not referenced in runbook | MED — if environments are deleted, runbook has no recovery |
| wpf-ts4 | Set repo variables | Yes — Kill-Switch Operations section | Covered |
| wpf-3ar | Create release branch | Partial — I-11 covers recovery | Covered |
| wpf-1gn | nuget.org namespace reservation | No — not mentioned post-bootstrap | LOW — one-time |
| wpf-1pt | First upstream build | No — bootstrap only | Acceptable |
| wpf-2hh | Bulk-process 223 candidates | No — not in runbook | MED — no operator procedure for this in runbook |
| wpf-j79 | Triage disagreements to zero | No — only "Escalate a review-disagreement issue" section exists | MED |
| wpf-3vk | Auto-apply approved patches | Implicit in weekly checklist | Covered |
| wpf-2xo | First release.yml run | No — runbook assumes release pipeline is already working | MED |
| wpf-23n | SC Directory.Build.props update | No — SC-side change not documented in this runbook | LOW — scope note |
| wpf-1o9 | Manual UI smoke | No — not referenced in runbook | LOW |
| wpf-29a | Runbook validated by Oystein | No acceptance criterion defined | MED (OP-14 above) |
| wpf-2wi | Enable cron triggers | No — not in runbook | LOW — one-time |
| wpf-1sr | 4 consecutive publishes | No — success metric, not a runbook step | Acceptable |

**5 medium gaps:** `wpf-2hh` (bulk-review procedure), `wpf-j79` (triage-to-zero procedure), `wpf-2xo` (first release procedure), `wpf-238` (branch protection recovery commands), `wpf-29a` (acceptance criterion). The runbook covers steady state well but has significant gaps for the Phase-0/Phase-1 transition activities.

---

*Audit complete. No files were modified.*
