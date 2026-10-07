# Operator Runbook — InitialForce WPF Fork

**Owner:** Øystein Krog (Initial Force)
**Date:** 2026-04-27, pipeline sections updated 2026-10-07
**Authoritative for:** Steady-state operations and incident response post-bootstrap.
**Not authoritative for:** Bootstrap steps (see `exec-docs/50-runbooks.md` §50.1).

Honest load estimate: 6 predictable hours/month (daily + weekly) plus 2–14 hours of interrupt-driven work (escalations, conflict resolution, regression investigations). Do not budget less than 10 hours/month in a mature steady state.

---

## Dashboard References

| Signal | Where |
|---|---|
| Workflow run history | `https://github.com/InitialForce/wpf/actions` |
| Open pipeline issues | `gh issue list --label automated --state open --repo InitialForce/wpf` |
| Open pipeline PRs | `gh pr list --label automated --repo InitialForce/wpf` |
| Kill-switch state | `gh variable get IF_AUTONOMY_ENABLED --repo InitialForce/wpf` |
| Freeze state | `gh variable get IF_AUTOMERGE_FROZEN --repo InitialForce/wpf` |
| Ledger | `.if-fork/patch-ledger.jsonl` on `if/main` |
| Audit branch | `git log audit --oneline` |
| Perf series | `perf/series.jsonl` (last entry = most recent benchmark) |

---

## How the pipeline runs

The pipeline runs without a person in the loop; [FORK.md](../FORK.md#pipeline) describes each stage. In short:

- Every change reaches `if/main` through a PR labelled `automated`: one sync PR `if/staging -> if/main` for upstream `release/10.0`, and one `claude/cherry-pick-pr-<N>` PR per approved upstream PR. `automerge.yml` merges each one when its `Build` run passes.
- `auto-release.yml` tags and publishes to nuget.org at most once a day, and only when `src/` changed since the last release.
- Every problem that needs a person is one open issue labelled `automated`. Repeats add a comment instead of a new issue, and the issue closes itself when the problem is gone:

| Issue title | Opened by | Closes when |
|---|---|---|
| `CI failure: <workflow>` | `claude-on-failure.yml`, with a Claude analysis | that workflow passes again |
| `Nightly upstream sync blocked` | `nightly-rebase.yml` | a later sync succeeds |
| `Weekly differential: fork smoke results differ from upstream` | `weekly-differential.yml` | a clean weekly run |
| `review-disagreement: PR #<N> ...` | `pr-review.yml` | a person closes it (see below) |

A failed build on a pipeline PR is reported as one comment on that PR, not as an issue.

---

## Daily Checklist (~5 min)

1. List open pipeline issues:
   ```bash
   gh issue list --repo InitialForce/wpf --label automated --state open
   ```
   Each one names what failed and links the run. Nothing else needs a daily look.

2. List pipeline PRs that are still open:
   ```bash
   gh pr list --repo InitialForce/wpf --label automated \
     --json number,title,headRefName,createdAt
   ```
   A PR open for more than a day either has a failed build (see its comment) or auto-merge is frozen.

---

## Weekly Checklist (~20 min)

1. Check that upstream is being merged. `if/main` should contain upstream `release/10.0` within a day of each upstream change.
   ```bash
   git fetch upstream release/10.0 && git fetch origin if/main
   git merge-base --is-ancestor upstream/release/10.0 origin/if/main && echo "up to date"
   ```

2. If `IF_AUTOMERGE_FROZEN=true` (set after a regression): investigate, fix, then clear:
   ```bash
   gh variable set IF_AUTOMERGE_FROZEN -b "false" --repo InitialForce/wpf
   ```

3. **Ledger signature integrity check** (no CI workflow runs it, so run it by hand):
   ```bash
   python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl --strict-signature
   ```
   Expected output: `Ledger OK: N entries, all hashes valid, all commits signed`
   Any error (unsigned commit, hash mismatch, unexpected entry) is an incident: pause autonomy and follow I-10.

---

## Monthly Checklist (~45 min–2 h)

1. **Cost review:**
   ```bash
   gh api /orgs/InitialForce/settings/billing/actions
   ```
   Compare against $52/month expected, $120 high-water. Over $60 — check which workflow is over-running.

2. **Audit branch spot-check** — pick 3 random decisions from the last 30 days:
   ```bash
   git log audit --since="30 days ago" --oneline | shuf | head -3
   git show audit:<run-id>.json | jq '.decision_summary'
   ```
   Verify: rationale is coherent, tool calls match the stated action, no unexpected file edits.

3. **Rotate GitHub App private key** — monthly:
   ```bash
   # Generate new key from GitHub App settings page → download new-private-key.pem
   gh secret set GH_APP_PRIVATE_KEY --env bot-credentials --repo InitialForce/wpf
   # Delete the old key fingerprint entry from the GitHub App settings UI.
   ```

4. **CVE scan:**
   ```bash
   gh api /repos/dotnet/wpf/security-advisories --jq '.[].summary' 2>/dev/null || \
     gh search issues --repo dotnet/wpf --label "area-Security" --state open
   ```
   Cross-reference with `.if-fork/patch-state.json`. Any upstream security fix not present as `cherry_picked` or `graduated_upstream` needs a new candidate issue.

5. **DECISION_LOG.md review:** Open `docs/DECISION_LOG.md`. Verify every `config.yaml` change in the last 30 days has a corresponding entry. If Claude changed a threshold autonomously without a log entry, add one manually and open an issue to improve the prompt.

6. **Issue debt check:**
   ```bash
   gh issue list --repo InitialForce/wpf --state open --label "automated" | wc -l
   ```
   If over 20, suspend new triage runs and clear the backlog first.

7. **Audit `audit/*` orphan branch** for orphan growth — should not accumulate unbounded blobs.

---

## Common Operations

### Manually trigger discovery
```bash
gh workflow run pr-discovery.yml --repo InitialForce/wpf
```

### Escalate a review-disagreement issue
1. Open the issue: `gh issue view <N> --repo InitialForce/wpf`
2. Read both reviewer rationales attached to the issue.
3. Make a judgment call: comment `human-resolution: safe` or `human-resolution: unsafe`.
4. If you decide the PR is safe, start ingestion by hand with the reviewed head SHA (the `review_1` and `review_2` ledger entries for the PR hold it):
   ```bash
   gh workflow run pr-ingestion.yml --repo InitialForce/wpf -f pr_number=<N> -f head_sha=<reviewed sha>
   ```
   If you decide it is unsafe, do nothing more. Close the issue in both cases.

### Run the upstream sync by hand
```bash
# Rebuild if/staging even if the open sync PR already has the current upstream SHA:
gh workflow run nightly-rebase.yml --repo InitialForce/wpf -f force_rebase=true
```
If the sync is blocked by conflicts, merge by hand. Never rebase `if/main`.
```bash
git fetch upstream release/10.0 && git fetch origin if/main
git checkout -B if/staging origin/if/main
git merge --no-ff upstream/release/10.0
# resolve, commit, then:
git push --force-with-lease origin if/staging
gh pr create --repo InitialForce/wpf --base if/main --head if/staging \
  --title "Merge upstream release/10.0 into if/main" --label automated
```
The `automated` label lets `automerge.yml` merge it when Build passes. The next nightly run closes the "Nightly upstream sync blocked" issue.

### Adopt an upstream stable tag by hand
```bash
gh workflow run upstream-stable-adoption.yml --repo InitialForce/wpf -f tag=v10.0.<N>
```
The daily poller does this on its own and stops retrying a tag after three failed runs.

### Merge a pipeline PR while auto-merge is frozen
Use a merge commit, never squash. Squashing drops the upstream history and the `(cherry picked from commit ...)` trailers.
```bash
gh pr merge <N> --repo InitialForce/wpf --merge
```

### Check patch-ledger integrity manually
```bash
python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl
python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl --strict-signature
# Expected output: "Ledger OK: N entries, all hashes valid, all commits signed"
# Any other output is an incident — pause autonomy and investigate.
python tools/regenerate-state.py .if-fork/patch-ledger.jsonl > /tmp/regenerated-state.json
diff /tmp/regenerated-state.json .if-fork/patch-state.json
```

Run `--strict-signature` at minimum once per week (add to weekly checklist). It is not called by any CI workflow; proactive detection of unsigned commits relies on this manual check.

### Recovery from exhausted ledger push retries

`tools/ledger-event.py` retries ledger pushes 3 times. If all 3 fail (network partition, racing force-push), it exits with code 6 and the ledger has a local uncommitted change that was never pushed. The ledger is in an inconsistent state.

**Detect:**
```bash
# Check whether the local ledger commit is ahead of origin
git log --oneline origin/if/release/10.0..HEAD -- .if-fork/patch-ledger.jsonl
# Non-empty output = local-only ledger commit that needs pushing
```

**Recover:**
```bash
# Pull down any remote changes since the failed push
git fetch origin if/release/10.0
git pull --rebase origin if/release/10.0
# If a conflict appears in patch-ledger.jsonl, the lines are append-only;
# keep BOTH sets of lines (local and remote) in chronological order by ts field.
# After resolving:
git add .if-fork/patch-ledger.jsonl
git rebase --continue
git push origin HEAD:refs/heads/if/release/10.0
# Validate after push:
python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl --strict-signature
```

---

## Kill-Switch Operations

### Pause all autonomous activity immediately
```bash
gh variable set IF_AUTONOMY_ENABLED -b "false" --repo InitialForce/wpf
```
Effect: every Claude-invoking workflow checks `IF_AUTONOMY_ENABLED` as step 1 and exits 0 cleanly. Cancel in-flight runs if immediate stop is needed:
```bash
gh run list --repo InitialForce/wpf --status in_progress \
  --json databaseId --jq '.[].databaseId' \
  | xargs -I{} gh run cancel {} --repo InitialForce/wpf
```

While paused: diagnose manually via `git` and `gh` commands. The ledger still works — append events manually via `tools/ledger-event.py` if needed. The `patch-ledger.jsonl` file is the source of truth.

### Resume
```bash
gh variable set IF_AUTONOMY_ENABLED -b "true" --repo InitialForce/wpf
```

### Freeze auto-merges only (keep discovery and review running)
```bash
gh variable set IF_AUTOMERGE_FROZEN -b "true" --repo InitialForce/wpf
# Clear when resolved:
gh variable set IF_AUTOMERGE_FROZEN -b "false" --repo InitialForce/wpf
```

### Force-double review on all patches
```bash
gh variable set IF_REVIEW_DOUBLE_REQUIRED -b "true" --repo InitialForce/wpf
```
Default is `true`. Only lower this after explicit operator decision.

---

## Return-from-Vacation Checklist

Run in order after any absence of 5+ days:
```bash
# 1. Kill-switch state
gh variable get IF_AUTONOMY_ENABLED --repo InitialForce/wpf
gh variable get IF_AUTOMERGE_FROZEN --repo InitialForce/wpf

# 2. Open pipeline issues (one per problem; repeats are comments)
gh issue list --repo InitialForce/wpf --label automated --state open

# 3. Ledger changes since last visit
LAST_VISIT=2026-05-01
git log origin/if/main --since=$LAST_VISIT --oneline -- .if-fork/patch-ledger.jsonl | head -30

# 4. Pipeline PRs still open
gh pr list --repo InitialForce/wpf --label automated

# 5. Re-enable autonomy if it was paused before your absence
gh variable set IF_AUTONOMY_ENABLED -b "true" --repo InitialForce/wpf
```

---

## Incident Runbooks

### I-1: Bad NuGet Shipped to SC Production

**Detection:** SC production crash reports spike; `TypeLoadException` or behavioral regression traced to `InitialForce.WPF` version.

**Containment:**
```bash
gh variable set IF_AUTONOMY_ENABLED -b "false" --repo InitialForce/wpf
BAD_VERSION=10.0.X-if.YYYYMMDD.N
```

**Diagnosis:**
```bash
GOOD_TAG=if-10.0.X-perf.YYYYMMDD_PREV
BAD_TAG=if-10.0.X-perf.YYYYMMDD_BAD
git log $GOOD_TAG..$BAD_TAG --oneline
jq 'select(.event=="cherry_picked" and .ts > "GOOD_TAG_DATE")' .if-fork/patch-ledger.jsonl
```

**Remediation:**
```bash
# Step 1: Unlist from GitHub Packages (HUMAN ONLY — requires NUGET_FEED_PAT)
# SETUP CHECKLIST: NUGET_FEED_PAT must have BOTH scopes:
#   - write:packages  (required for dotnet nuget push)
#   - delete:packages (required for the DELETE call below — different scope)
# If NUGET_FEED_PAT lacks delete:packages, the DELETE returns HTTP 403.
# FALLBACK if 403: use the GitHub web UI:
#   https://github.com/orgs/InitialForce/packages/nuget/InitialForce.WPF/versions
#   Click the bad version → "Delete version". No token needed for UI deletion.
gh api /orgs/InitialForce/packages/nuget/InitialForce.WPF/versions \
  --jq ".[] | select(.name==\"$BAD_VERSION\") | .id"
VERSION_ID=<id>
GH_TOKEN=$NUGET_FEED_PAT \
  gh api -X DELETE /orgs/InitialForce/packages/nuget/InitialForce.WPF/versions/$VERSION_ID

# Step 1b: Unlist the version on nuget.org (nuget.org 'delete' unlists; it cannot remove).
# Do this for both InitialForce.WPF and InitialForce.WPF.RuntimeOverride.
dotnet nuget delete InitialForce.WPF $BAD_VERSION \
  --source https://api.nuget.org/v3/index.json --api-key <nuget.org key> --non-interactive

# Step 2: Pin SC to last known-good in Directory.Packages.props
# Step 3: Rebuild from previous good tag (workflow_dispatch input is named "tag", not "version_override")
gh workflow run release.yml --repo InitialForce/wpf --ref $GOOD_TAG \
  -f tag=$GOOD_TAG
```
MTTR target: < 2 hours detection-to-SC-hotfix.

---

### I-2: Force-Push Lost the Tip of `if/release/10.0`

**Detection:** CI fails on PR with "base branch not found"; missing commits.

**Recovery:**
```bash
git fetch if --tags
GOOD_TAG=$(git tag -l "if-10.0.*" | sort -V | tail -1)
git fetch if refs/heads/if/mirror/release/10.0
git checkout -b if/recovery if/mirror/release/10.0
git diff if/recovery if/$GOOD_TAG  # should be empty
git push if if/recovery:refs/heads/if/release/10.0 --force-with-lease
# If mirror also gone: use reflog (GitHub preserves dangling commits 90 days)
git push if $GOOD_TAG^{}:refs/heads/if/release/10.0 --force
```

**IMPORTANT — branch protection blocks force-push for human operators.**
RISK-004 documents that `if/release/10.0` has branch protection with no-force-push for non-bot accounts. A human operator's force-push will be rejected with HTTP 422. You must temporarily disable branch protection before force-pushing, then re-enable it immediately after.

```bash
# Step A: Temporarily disable branch protection (requires org-owner or admin PAT)
gh api -X DELETE /repos/InitialForce/wpf/branches/if%2Frelease%2F10.0/protection

# Step B: Perform the force-push recovery (commands above)

# Step C: Re-enable branch protection immediately after recovery
gh api -X PUT /repos/InitialForce/wpf/branches/if%2Frelease%2F10.0/protection \
  --input - <<'EOF'
{
  "required_status_checks": null,
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
# Alternatively: re-run P0-5 from the bootstrap runbook to restore the full rule set.
```

Write an entry in `docs/DECISION_LOG.md` recording the protection-disable window, the reason, and the time protection was re-enabled.

MTTR target: 30 min.

---

### I-3: Nightly Upstream Sync Blocked

**Detection:** an open issue titled "Nightly upstream sync blocked". It names the upstream SHA, links the run, and includes the escalation report: denylisted conflict files, Claude's reasons, or files the merge touched that upstream did not change.

**Diagnosis:**
```bash
gh run view <run-id> --repo InitialForce/wpf --log | grep -E "Conflicts:|Conflict markers|denylist|upstream did not change"
```

**Remediation:** merge by hand as in "Run the upstream sync by hand" above. The job records one `rebase_failed` ledger event per upstream SHA and does not repeat the issue for the same SHA, so nothing piles up while you work.

---

### I-4: Manual Freeze After Detecting a Regression

**Containment:**
```bash
gh variable set IF_AUTOMERGE_FROZEN -b "true" --repo InitialForce/wpf
```

**Bisect to find the offending patch:**
```bash
jq 'select(.event=="cherry_picked")' .if-fork/patch-ledger.jsonl | \
  jq -s 'sort_by(.ts) | last(.[]) | .applied_commit'
git bisect start && git bisect bad HEAD && git bisect good $LAST_KNOWN_GOOD_TAG
# At each step: dotnet test test/InitialForce.WpfSmoke/ -c Release
# Then: git bisect good/bad
```

**Remediation:**
```bash
git revert <bad-commit-sha> --no-edit
git push if HEAD:refs/heads/if/release/10.0  # via PR
python tools/ledger-event.py --event reverted --pr-number NNNN \
  --reason "Regression detected during manual SC validation"
gh variable set IF_AUTOMERGE_FROZEN -b "false" --repo InitialForce/wpf
```

---

### I-5: Anthropic API Outage or Rate-Limit Exhaustion

**Detection:** Claude-invoking workflows fail with `529 Too Many Requests` or `503`.

**Containment:**
```bash
gh variable set IF_AUTONOMY_ENABLED -b "false" --repo InitialForce/wpf
```
Check `https://status.anthropic.com`. After resolution or quota reset (daily caps reset midnight UTC):
```bash
gh variable set IF_AUTONOMY_ENABLED -b "true" --repo InitialForce/wpf
gh workflow run pr-discovery.yml --repo InitialForce/wpf
```

---

### I-6: GitHub Packages Feed Outage

**Detection:** SC CI fails at `dotnet restore` with `Unable to resolve 'InitialForce.WPF'`.

Check `https://www.githubstatus.com` — "GitHub Packages" component. Typical outages are < 1 hour; wait. If > 4 hours: pin SC to a cached version. Log in DECISION_LOG.md if > 4 hours.

---

### I-7: CVE Drops on WPF `release/10.0`

**Detection:** GitHub advisory for `dotnet/wpf`; or `security`-labelled issue from `pr-discovery.yml`; 24-hour SLA from public disclosure.

**Remediation:**
```bash
SECURITY_SHA=<upstream-fix-commit>
git checkout -b claude/security-CVE-YYYY-NNNNN if/release/10.0
git cherry-pick -x $SECURITY_SHA
git commit --amend -m "[if-port] Fix CVE-YYYY-NNNNN: <title>
Upstream-Commit: $SECURITY_SHA
If-Tier: S
Security: CVE-YYYY-NNNNN"
git push if claude/security-CVE-YYYY-NNNNN
gh pr create --repo InitialForce/wpf \
  --title "security: cherry-pick CVE-YYYY-NNNNN fix" \
  --label "security" --base if/release/10.0
gh workflow run release.yml --repo InitialForce/wpf --ref if/release/10.0
```

**Emergency CVE override when `IF_AUTOMERGE_FROZEN=true`:**

If autonomy is frozen (e.g., an active regression investigation under I-4) at the time a CVE requires response, the `autonomy-check.yml` gate will block the security PR from auto-merging. The `IF_AUTOMERGE_FROZEN` variable gates the automated pipeline only — a human operator can bypass it by merging manually:

```bash
# Check freeze state first
gh variable get IF_AUTOMERGE_FROZEN --repo InitialForce/wpf
# If true, merge the security PR manually via gh — this bypasses the auto-merge gate
# (human operators are not subject to IF_AUTOMERGE_FROZEN)
gh pr merge <PR-NUMBER> --repo InitialForce/wpf --squash --admin \
  --subject "security: cherry-pick CVE-YYYY-NNNNN fix [emergency manual merge]"
```

After the manual merge, write an audit-log entry:

```bash
python tools/ledger-event.py \
  --event cherry_picked \
  --pr-number <PR-NUMBER> \
  --upstream-commit $SECURITY_SHA \
  --detection-method manual_cve_response \
  --reason "Emergency manual merge during freeze — CVE-YYYY-NNNNN"
```

Document the override in `docs/DECISION_LOG.md` with timestamp and justification.

MTTR target: 24 hours from CVE public disclosure.

---

### I-8: GitHub App Private Key Compromised

**Containment:** Revoke the installation token immediately in GitHub App settings (Settings → Developer Settings → GitHub Apps → initial-force-wpf-bot → Installations → Revoke). Then:
```bash
gh variable set IF_AUTONOMY_ENABLED -b "false" --repo InitialForce/wpf
```

**Remediation:**
```bash
# Generate new private key from GitHub App settings; download new-private-key.pem
gh secret set GH_APP_PRIVATE_KEY --env bot-credentials --repo InitialForce/wpf
# Delete old key fingerprint from GitHub App settings UI
gh workflow run pr-discovery.yml --repo InitialForce/wpf  # verify new key works
gh variable set IF_AUTONOMY_ENABLED -b "true" --repo InitialForce/wpf
```
Postmortem required. Audit the `audit/` branch for unauthorized actions committed under the old key.

---

### I-9: Allowlisted Contributor Account Compromised

**Containment:**
```bash
gh variable set IF_AUTONOMY_ENABLED -b "false" --repo InitialForce/wpf
```

**Audit patches from this author:**
```bash
AUTHOR=h3xds1nz
jq --arg a "$AUTHOR" 'select(.author==$a and (.event=="cherry_picked" or .event=="published"))' \
  .if-fork/patch-ledger.jsonl | jq -s '.'
```

**Remediation:**
```bash
git revert <applied-commit-sha> --no-edit
python tools/ledger-event.py --event reverted --pr-number NNNN \
  --reason "Author account compromise suspected"
# Remove from allowlist via PR to .if-fork/config.yaml (requires CODEOWNERS approval)
gh variable set IF_AUTONOMY_ENABLED -b "true" --repo InitialForce/wpf
```

---

### I-10: Ledger Tampering Detected

**Detection:** `tools/ledger-validate.py` fails with "non-trailing line modified" or regenerated state doesn't match committed `patch-state.json`.

**Containment:**
```bash
gh variable set IF_AUTONOMY_ENABLED -b "false" --repo InitialForce/wpf
```

**Bisect to find the tampering commit:**
```bash
git log --all -- .if-fork/patch-ledger.jsonl | head -20
git bisect start HEAD <last-known-good-commit>
# NOTE: ledger-validate.py uses --ledger-path (named flag); positional args are not accepted.
git bisect run python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl
```

**Remediation:**
```bash
git revert <bad-commit>
git push if HEAD:refs/heads/if/release/10.0  # via PR, not direct push
# If external actor: rotate ALL secrets
gh secret set GH_APP_PRIVATE_KEY --env bot-credentials --repo InitialForce/wpf
gh secret set ANTHROPIC_API_KEY --env bot-credentials --repo InitialForce/wpf
gh secret set NUGET_FEED_PAT --env wpf-nuget-publish --repo InitialForce/wpf
```
High-severity security event — file a separate incident report.

---

### I-11: `if/release/10.0` Accidentally Deleted

**Recovery:**
```bash
git fetch if refs/heads/if/mirror/release/10.0
git log if/mirror/release/10.0 --oneline | head -3
git push if if/mirror/release/10.0:refs/heads/if/release/10.0
# Verify tip matches last immutable tag:
LAST_TAG=$(git tag -l "if-10.0.*" | sort -V | tail -1)
git log if/release/10.0..$LAST_TAG --oneline  # should be empty
# If mirror also deleted, recover from last tag:
git push if refs/tags/$LAST_TAG^{}:refs/heads/if/release/10.0
# Re-apply branch protection (re-run P0-5 from bootstrap runbook)
```
MTTR target: 1 hour.

---

## Phase-0 Hand-off Procedures

These stubs cover the one-time transition activities from `BOOTSTRAP_STATUS.md`. Each must be completed before the pipeline is considered steady-state.

### wpf-2hh: Bulk-process 223 PR candidates

**Goal:** Run `pr-review.yml` across all 223 candidate issues opened during bootstrap so the review queue starts at zero.

**Procedure (stub):**
1. List all open `candidate` issues: `gh issue list --repo InitialForce/wpf --label candidate --state open --limit 300`
2. For each batch of 20, trigger review: `gh workflow run pr-review.yml --repo InitialForce/wpf -f batch_size=20`
3. Monitor for `review-disagreement` issues opened by the bot; these feed into wpf-j79.
4. Repeat until `gh issue list --label candidate --state open | wc -l` returns 0.

Estimated time: 4–8 hours spread across multiple sessions. Do not attempt in one sitting.

### wpf-j79: Triage all review-disagreement issues to zero

**Goal:** Every `review-disagreement` issue must receive a human resolution comment before steady-state operation begins.

**Procedure (stub):**
1. List open disagreements: `gh issue list --repo InitialForce/wpf --label review-disagreement --state open --limit 100`
2. For each: `gh issue view <N> --repo InitialForce/wpf` — read both reviewer rationales.
3. Post resolution comment: `gh issue comment <N> --repo InitialForce/wpf --body "human-resolution: safe"` or `human-resolution: unsafe`.
4. For a PR you judge safe, start `pr-ingestion.yml` by hand (see "Escalate a review-disagreement issue").
5. Close when disagreement count reaches zero.

### wpf-2xo: First release.yml run

**Goal:** Validate the full release pipeline end-to-end with a real tag and human approval gate.

**Procedure (stub):**
1. Ensure `if/release/10.0` tip is clean and all smoke tests pass.
2. Create and push a signed tag: `git tag -s if-10.0.1-perf.$(date +%Y%m%d) -m "First IF release" && git push if --tags`
3. Monitor `release.yml` until the `publish` job reaches the `wpf-nuget-publish` approval gate.
4. Review the NuGet packages in the artifacts before approving.
5. Approve in the GitHub Actions UI; verify the package appears in GitHub Packages.
6. Verify SC can resolve the package: test `dotnet restore` in a local SC clone pinned to the new version.

### wpf-238: Set branch protection

**Goal:** Apply the documented branch protection rules to `if/release/10.0` and `if/main`.

**Procedure (stub):**
Refer to bootstrap runbook step P0-5 for the exact API calls. Quick reference:
```bash
# Apply protection to if/release/10.0 (URL-encode the slash as %2F)
gh api -X PUT /repos/InitialForce/wpf/branches/if%2Frelease%2F10.0/protection \
  --input .if-fork/branch-protection-release.json
gh api -X PUT /repos/InitialForce/wpf/branches/if%2Fmain/protection \
  --input .if-fork/branch-protection-main.json
```
Verify after applying:
```bash
gh api /repos/InitialForce/wpf/branches/if%2Frelease%2F10.0/protection \
  --jq '{allow_force_pushes: .allow_force_pushes.enabled, required_reviews: .required_pull_request_reviews}'
```
Expected: `allow_force_pushes: false`.

### wpf-29a: Operator runbook validated by Oystein

**Goal:** Sign off that this runbook is executable and accurate before the first production release.

**Acceptance criteria:**
- [ ] Complete the daily checklist once with live data (all three `gh search issues` commands return output or confirmed-empty).
- [ ] Complete the weekly checklist once (ledger integrity check passes with `--strict-signature`).
- [ ] Execute I-1 diagnosis steps (not remediation) on a test bad-version to verify the `gh api` lookup works with the correct PAT scopes.
- [ ] Verify `gh run view --log` returns conflict data for a historical `nightly-rebase.yml` failure run.
- [ ] Sign off: add a comment to bead `wpf-29a` with the date and "runbook validated — all acceptance criteria met".
