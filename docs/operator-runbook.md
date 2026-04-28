# Operator Runbook — InitialForce WPF Fork

**Owner:** Øystein Krog (Initial Force)
**Date:** 2026-04-27
**Authoritative for:** Steady-state operations and incident response post-bootstrap.
**Not authoritative for:** Bootstrap steps (see `exec-docs/50-runbooks.md` §50.1).

Honest load estimate: 6 predictable hours/month (daily + weekly) plus 2–14 hours of interrupt-driven work (escalations, conflict resolution, regression investigations). Do not budget less than 10 hours/month in a mature steady state.

---

## Dashboard References

| Signal | Where |
|---|---|
| Workflow run history | `https://github.com/InitialForce/wpf/actions` |
| Latest triage-summary issue | `gh issue list --label triage-summary --repo InitialForce/wpf --limit 1` |
| Kill-switch state | `gh variable get IF_AUTONOMY_ENABLED --repo InitialForce/wpf` |
| Freeze state | `gh variable get IF_AUTOMERGE_FROZEN --repo InitialForce/wpf` |
| Ledger current state | `.if-fork/patch-state.json` on `if/release/10.0` |
| Audit branch | `git log audit --oneline` |
| Perf series | `perf/series.jsonl` (last entry = most recent benchmark) |

---

## Daily Checklist (~5 min; ~15 min if issues flagged)

1. Scan for flagged labels:
   ```bash
   gh issue list --label "security,review-disagreement,rebase-conflict" \
     --repo InitialForce/wpf --state open
   ```
   Any hit requires action before end of day. `security` issues with no human comment after 48 hours trigger an email to oncall — do not let that fire.

2. Skim the weekly health-pulse line in the latest `triage-summary` issue:
   ```bash
   gh issue list --label "triage-summary" --repo InitialForce/wpf --limit 1
   ```
   If the summary line says `cost: $XX > $60` or flags a perf regression, investigate before leaving.

3. Nothing to do if: no flagged labels, health pulse is green, cost under $60/month.

---

## Weekly Checklist (~20–60 min)

Budget 45–60 minutes on rebase weeks. A 200-file rebase diff is not a 20-minute review.

1. Review the rebase PR opened by the bot (`claude/rebase-YYYYMMDD`):
   - Read the diff-summary comment; check for `dropped-hunk` or `escalation` warnings.
   - Verify CI is green: build + smoke + perf-gate.
   - Check `.if-fork/patch-state.json` for entries with state `graduated_upstream` — these patches can be dropped.
   - Approve and merge if clean; leave a comment explaining the conflict if escalating.

2. Check for a weekly differential report:
   ```bash
   gh issue list --label "differential,weekly-report" --repo InitialForce/wpf --limit 1
   ```

3. If `IF_AUTOMERGE_FROZEN=true` (manually set after detecting a regression): investigate, fix, then clear:
   ```bash
   gh variable set IF_AUTOMERGE_FROZEN -b "false" --repo InitialForce/wpf
   ```

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
4. The `pr-ingestion.yml` workflow re-checks resolution and proceeds or closes.

### Force a manual rebase
```bash
git fetch upstream
git checkout -b claude/rebase-$(date +%Y%m%d) if/release/10.0
git rebase --onto upstream/release/10.0 $PREV_UPSTREAM_TAG --strategy-option=patience
git config rerere.enabled true && git config rerere.autoupdate true
# Push and open a PR as usual.
```

### Check patch-ledger integrity manually
```bash
python tools/ledger-validate.py .if-fork/patch-ledger.jsonl
python tools/regenerate-state.py .if-fork/patch-ledger.jsonl > /tmp/regenerated-state.json
diff /tmp/regenerated-state.json .if-fork/patch-state.json
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

# 2. Issues opened since last check (substitute last-check date)
LAST_VISIT=2026-05-01
gh issue list --repo InitialForce/wpf --state open \
  --search "created:>$LAST_VISIT" \
  --label "security,review-disagreement,rebase-conflict"

# 3. Ledger changes since last visit
git log --since=$LAST_VISIT --oneline -- .if-fork/patch-state.json

# 4. Audit branch summary
git log audit --since=$LAST_VISIT --oneline | head -30

# 5. Open PRs requiring human action
gh pr list --repo InitialForce/wpf --label "awaiting-human,review-disagreement"

# 6. Re-enable autonomy if it was paused before your absence
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
gh api /orgs/InitialForce/packages/nuget/InitialForce.WPF/versions \
  --jq ".[] | select(.name==\"$BAD_VERSION\") | .id"
VERSION_ID=<id>
gh api -X DELETE /orgs/InitialForce/packages/nuget/InitialForce.WPF/versions/$VERSION_ID

# Step 2: Pin SC to last known-good in Directory.Packages.props
# Step 3: Rebuild from previous good tag
gh workflow run release.yml --repo InitialForce/wpf --ref $GOOD_TAG \
  -f version_override=$GOOD_VERSION
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
MTTR target: 30 min.

---

### I-3: Nightly Rebase Failed with Conflicts on Multiple Files

**Detection:** `nightly-rebase.yml` opens `rebase-conflict` issue; conflict count exceeds 30% threshold.

**Diagnosis:**
```bash
gh run download <run-id> --name conflict-artifacts --dir /tmp/conflicts
bash tools/cherry-pick-pre-flight.sh \
  --base upstream/release/10.0 --patches-dir patches/ \
  --output /tmp/graduation-candidates.txt
```

**Remediation:**
```bash
# Drop graduated patches
python tools/ledger-event.py --event graduated_upstream --pr-number NNNN \
  --upstream-commit SHA --detection-method hunk_match
# Manual rebase
git fetch upstream
git checkout claude/rebase-YYYYMMDD
git rebase --onto upstream/release/10.0 $PREV_UPSTREAM_TAG --strategy-option=patience
git config rerere.enabled true && git config rerere.autoupdate true
```

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
git bisect run python tools/ledger-validate.py .if-fork/patch-ledger.jsonl
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
