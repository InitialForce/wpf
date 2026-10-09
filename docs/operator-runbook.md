# Operator Runbook — InitialForce WPF Fork

**Owner:** Øystein Krog (Initial Force)
**Date:** 2026-04-27, pipeline sections updated 2026-10-09
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
| PRs waiting for a person | `gh pr list --label needs-human-merge --repo InitialForce/wpf` |

---

## How the pipeline runs

The pipeline runs without a person in the loop; [FORK.md](../FORK.md#pipeline) describes each stage. In short:

- Every change reaches `if/main` through a PR labelled `automated`: one sync PR `if/staging -> if/main` for upstream `release/10.0`, and one `claude/cherry-pick-pr-<N>` PR per approved upstream PR. `automerge.yml` merges each one when its `Build` run passes.
- `automerge.yml` leaves a PR to you when Claude resolved a conflict in it (label `needs-human-merge`), when it changes a fork-owned file, or when a cherry-pick changes anything outside `src/` and `eng/`. It says why in a comment on the PR. See "Merge a PR that needs a person" below.
- The perf gates do not measure WPF yet: the `Build` perf job runs nothing, and the release perf gate runs placeholder benchmarks against an unmeasured baseline.
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
   A PR open for more than a day has a failed build, has a refusal comment from `automerge.yml`, carries `needs-human-merge`, or auto-merge is frozen. The comments on the PR say which.

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

2. **Decision spot-check:** pick 3 ledger events from the last 30 days and open their runs:
   ```bash
   git log origin/if/main --since="30 days ago" --format=%H -- .if-fork/patch-ledger.jsonl | shuf | head -3
   ```
   For a review, read both verdicts' reasoning in the ledger entry. For a merged cherry-pick, compare the PR diff with the upstream PR. Check that the rationale is coherent and no unexpected file changed.

3. **Rotate the GitHub App private key** every month. See "Rotate a secret" below.

4. **CVE scan:**
   ```bash
   gh api /repos/dotnet/wpf/security-advisories --jq '.[].summary' 2>/dev/null || \
     gh search issues --repo dotnet/wpf --label "area-Security" --state open
   ```
   Cross-reference with the ledger (`.if-fork/patch-ledger.jsonl`; `patch-state.json` is stale). Any upstream security fix not present as `cherry_picked` or `graduated_upstream` needs action: see I-7.

5. **DECISION_LOG.md review:** Open `docs/DECISION_LOG.md`. Verify every `config.yaml` change in the last 30 days has a corresponding entry. If Claude changed a threshold autonomously without a log entry, add one manually and open an issue to improve the prompt.

6. **Issue debt check:**
   ```bash
   gh issue list --repo InitialForce/wpf --state open --label "automated" | wc -l
   ```
   If over 20, suspend new triage runs and clear the backlog first.

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
4. If you decide the PR is safe, record an `approved` outcome for the reviewed head SHA (the `review_1` and `review_2` ledger entries for the PR hold it), then start ingestion. Ingestion refuses any head without an `approved` outcome, and a later `rejected` or `escalated` event for the same head cancels it.
   ```bash
   git checkout if/main && git pull
   python tools/ledger-event.py --event approved --pr-number <N> --head-sha <reviewed sha> \
     --actor <your GitHub login> --details-json '{"reason":"human-resolution"}' --push
   gh workflow run pr-ingestion.yml --repo InitialForce/wpf -f pr_number=<N> -f head_sha=<reviewed sha>
   ```
   If you decide it is unsafe, do nothing more. Close the issue in both cases.

### A review escalated with `prepare_failed` or `origin_errors`
- `prepare_failed`: the `prepare` job could not build the review input three times. Read the `prepare` log of the latest `pr-review.yml` run. Common causes: the PR head moved, the base SHA is unusable, the diff is empty, or a file name has a newline.
- `origin_errors` (the `merge-verdict` job is red): a verdict did not come from its own review job, so one review job may have tried to stand in for the other. Treat it as a security incident: pause autonomy and read both reviewer logs.

To retry either case, run `pr-review.yml` by hand with the same `pr_number` and `head_sha` (see "Review one upstream PR by hand").

### Run the upstream sync by hand
```bash
# Rebuild if/staging even if the open sync PR already has the current upstream SHA,
# or retry an upstream SHA that is already reported blocked:
gh workflow run nightly-rebase.yml --repo InitialForce/wpf -f force_rebase=true
```
Once an upstream SHA is blocked (open "Nightly upstream sync blocked" issue, or a `rebase_failed` ledger event for it), the nightly run skips that SHA without calling Claude until upstream moves or you set `force_rebase`.

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
Your merge commit is not signed by the bot, so `automerge.yml` will not merge this PR. Merge it by hand with a merge commit once Build passes. The next nightly run closes the "Nightly upstream sync blocked" issue.

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

### Merge a PR that needs a person
`automerge.yml` refuses these, and comments once per PR head with the reason:
- **Claude resolved a conflict** (label `needs-human-merge`, commit trailer `Conflict-Resolved-By: claude`). The two reviews covered the upstream diff only; nobody has checked the resolution. Review the conflicted files: for a cherry-pick, compare the PR with the upstream PR; for the sync PR, compare each resolved file with both sides (`git diff <merge>^1 <merge> -- <file>` and `git diff <merge>^2 <merge> -- <file>`). Look for anything that is in neither side.
- **Fork-owned files** (`.github/`, `.if-fork/`, `tools/`, `tests/`, `test/`, `docs/`, `perf/`, `packaging/InitialForce.*`, agent configuration such as `CLAUDE.md` or `.mcp.json`). Upstream has none of these, so a change there is unexpected. Find out why before merging.
- **A cherry-pick outside `src/` and `eng/`** (for example the root `Directory.Build.props`). Check that the change is what the upstream PR intended.

When it is right and `Build` passed, merge with a merge commit: `gh pr merge <N> --repo InitialForce/wpf --merge`. If it is wrong, close the PR and record the decision for that upstream head:
```bash
python tools/ledger-event.py --event escalated --pr-number <upstream N> --head-sha <reviewed sha> \
  --actor <your GitHub login> --details-json '{"reason":"human_rejected_resolution"}' --push
```
For a wrong sync PR, do not just close it: the next nightly run would ask Claude again. Merge upstream by hand instead (see "Run the upstream sync by hand") and push the result as `if/staging`.

### Auto-merge trust checks
`automerge.yml` runs when Build finishes on a pipeline branch, every 30 minutes, and by hand (`gh workflow run automerge.yml --repo InitialForce/wpf`). Runs go one at a time (concurrency group `automerge-if-main`). Every run calls `tools/automerge-run.sh`, which goes through all open PRs labelled `automated`, so a run that GitHub drops from the queue loses nothing. A PR is merged only when all of these hold:
- The newest `pull_request` Build run on the PR head concluded `success`.
- The PR is open, from this repository (not a fork), and targets `if/main`.
- The head branch is `if/staging` or `claude/cherry-pick-pr-<N>`.
- The PR was opened by this GitHub App's own bot login and carries the `automated` label.
- `tools/automerge-verify.sh` passes. It runs from a separate checkout of `if/main` and uses that checkout's tools, `config.yaml`, ledger and `docs/bot-gpg-pubkey.asc`, never the PR's copies. The key's fingerprint must equal the `GH_BOT_GPG_FINGERPRINT` secret.
  - The PR changes no fork-owned file: `.github/`, `.if-fork/`, `tools/`, `tests/`, `test/`, `docs/`, `perf/`, `data/`, `packaging/InitialForce.*`, `FORK.md`, `NOTICE.md`, `pyproject.toml`, or agent configuration (`.claude/`, `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `.mcp.json`). For the sync PR this includes changes upstream itself brings.
  - A cherry-pick PR changes only files under `src/` and `eng/`.
  - No changed file is on `file_denylist`. For a cherry-pick PR this is the whole diff. For the sync PR it is the files whose content differs from what `git merge` produces on its own, which are the conflict resolutions.
  - Claude resolved no conflict: the head commit has no `Conflict-Resolved-By` trailer, and for the sync PR no file differs from git's own merge.
  - Cherry-pick PR: exactly one commit, signed by the bot key. Its `Upstream-PR` trailer matches the branch number, and its `Upstream-PR-Head` has an `approved` outcome in the `if/main` ledger with no later `rejected` or `escalated`.
  - Sync PR: the head is a merge commit signed by the bot key, its second parent is in upstream `release/10.0`, and every other new commit comes from upstream.
  - The PR contains the current `if/main` tip. Commits on `if/main` that only change `.if-fork/patch-ledger.jsonl` do not count.
- `if/main` is still at the commit that was checked when the merge call starts. GitHub has no merge call that checks the base commit, so a push to `if/main` in the moment between the last fetch and the merge call is not caught here. Only `automerge.yml`, a person and ledger commits write `if/main`.
- `IF_AUTONOMY_ENABLED=true` and `IF_AUTOMERGE_FROZEN` is not `true`.

When `if/main` has moved on, `automerge.yml` does not use GitHub's "update branch" (its merge commit would not be bot-signed). It sends `pr-ingestion-requested` again for a cherry-pick PR, or `nightly-rebase` with `force: true` for the sync PR, once per PR head (a `<!-- automerge-rebuild:<sha> -->` comment records it). The rebuilt branch gets a new Build, which runs `automerge.yml` again. If the nightly force run is replaced in the queue by a scheduled one, the scheduled run rebuilds the sync PR by itself, because `if/staging` is behind `if/main`.

### Where Claude runs, and what it can reach
Claude resolves merge and cherry-pick conflicts in a job of its own (`resolve-conflicts` in `pr-ingestion.yml` and `nightly-rebase.yml`). That job has `contents: read`, no environment, no App token, no GPG key and no stored git credentials. It does hold `ANTHROPIC_API_KEY` (a repository secret today), so the Claude step is locked down:
- `--setting-sources user`, `--strict-mcp-config` and `disableAllHooks`: Claude Code loads only the settings the action writes, not the work tree's `.claude/`, `CLAUDE.md`, `.mcp.json` or hooks.
- `tools/prepare-resolver.sh` refuses to start Claude when the work tree holds any of those files, or when a conflicted path has characters a tool rule cannot hold. It writes `.escalation.md` instead, which the trusted job turns into an escalation.
- No shell and no web tools. Claude reads the work tree; the base, fork and upstream sides of each conflict are written to `.resolver-input/` beforehand. It may edit only the conflicted files, `.escalation.md` and `.resolver-delete.txt` (where it lists files to delete). Reads of `/proc` and the runner's `_temp` folder are denied.
- Every job of one run uses the same `if/main` commit, recorded by the first job.

The job uploads only the conflicted files as an artifact. The next job (`finalize`) runs `if/main`'s own scripts. It repeats the merge or cherry-pick itself on that same commit and takes only the conflicted paths from the artifact. It refuses symlinks, missing files and leftover conflict markers. It then runs `file_denylist` from `if/main` on the whole branch diff and checks for files upstream did not change. Only after that does it import the bot key, sign with the trailer `Conflict-Resolved-By: claude` and push. Nothing is pushed before that check, so an open PR cannot be updated with denylisted content. `automerge.yml` refuses that trailer, so a person merges the PR.

`claude-on-failure.yml` runs Claude with the same settings. It reads only `failure-input/` (the failed log, an excerpt and recent commits) and `.if-fork/config.yaml`, and writes only `failure-output/analysis.md`.

A PR that only changes `docs/**` or Markdown gets no Build run (the PR trigger ignores those paths), so merge it by hand.

### Review one upstream PR by hand
```bash
gh workflow run pr-review.yml --repo InitialForce/wpf -f pr_number=<N> -f head_sha=<sha>
```
The daily review rate is `discovery.max_reviews_per_run` in `.if-fork/config.yaml` (10).

### Releases
`auto-release.yml` (09:23 UTC) tags at most once a day, and only when `src/` changed since the last published release. It then dispatches `release.yml` on `if/main` with the tag. A release that failed or whose dispatch was lost is retried: for a release tag at most 7 days old with no GitHub Release and no successful or running Release run, `auto-release.yml` dispatches `release.yml` again (up to 3 runs per tag) instead of making a new tag. `force=true` skips that retry. The tag is checked by `tools/verify-release-tag.sh` against `docs/bot-gpg-pubkey.asc` and the `GH_BOT_GPG_FINGERPRINT` secret; no repository variable is involved. `release.yml` runs only from `if/main` (any other ref is refused) and treats the tag as data. It calls `build.yml` on the tag's commit, runs the perf gate on the pack (placeholder benchmarks), and publishes to nuget.org, GitHub Packages and a GitHub Release. No `.snupkg` is made; the Windows PDBs ship inside the `.nupkg`. The `publish` job uses the `wpf-nuget-publish` environment, which still requires `oysteinkrog` to approve each release until that reviewer is removed.
```bash
# Release now, skipping the once-a-day and src/-changed checks:
gh workflow run auto-release.yml --repo InitialForce/wpf -f force=true
# Re-run the release for an existing tag:
gh workflow run release.yml --repo InitialForce/wpf --ref if/main -f tag=<tag>
```

### Regenerate the perf baseline
`perf/baseline.json` holds seed numbers that were never measured, and the harness benchmarks are placeholder loops, so the release perf gate measures nothing real either way. Regenerating the baseline only makes the gate consistent with the placeholder harness. `tools/check-regression.py` fails any benchmark without a finite `Mean` and a finite, non-negative `BytesAllocatedPerOperation` (a measured 0 is fine), and regeneration refuses such reports too.
1. `gh workflow run release.yml --repo InitialForce/wpf --ref if/main -f tag=<tag> -f regenerate_perf_baseline=true`
2. Download the `perf-baseline-<version>` artifact from that run and review it.
3. Commit it as `perf/baseline.json` on `if/main`.
4. Re-run the release: `gh workflow run release.yml --repo InitialForce/wpf --ref if/main -f tag=<tag>`

### Check patch-ledger integrity manually
```bash
python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl
python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl --strict-signature
# Expected output: "Ledger OK: N entries, all hashes valid, all commits signed"
# Any other output is an incident — pause autonomy and investigate.
# .if-fork/patch-state.json is stale and nothing reads it, so do not diff it.
```

Run `--strict-signature` at minimum once per week (add to weekly checklist). It is not called by any CI workflow; proactive detection of unsigned commits relies on this manual check.

### Recovery from exhausted ledger push retries

`tools/ledger-event.py` retries ledger pushes 3 times. If all 3 fail (network partition, racing force-push), it exits with code 6 and the ledger has a local uncommitted change that was never pushed. The ledger is in an inconsistent state.

**Detect:**
```bash
# Check whether the local ledger commit is ahead of origin
git log --oneline origin/if/main..HEAD -- .if-fork/patch-ledger.jsonl
# Non-empty output = local-only ledger commit that needs pushing
```

**Recover:**
```bash
# Pull down any remote changes since the failed push
git fetch origin if/main
git pull --rebase origin if/main
# If a conflict appears in patch-ledger.jsonl, the lines are append-only;
# keep BOTH sets of lines (local and remote) in chronological order by ts field.
# After resolving:
git add .if-fork/patch-ledger.jsonl
git rebase --continue
git push origin HEAD:refs/heads/if/main
# Validate after push:
python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl --strict-signature
```

### Rotate a secret
All seven secrets are **repository secrets today** (`ANTHROPIC_API_KEY`, `GH_APP_ID`, `GH_APP_PRIVATE_KEY`, `GH_BOT_GPG_FINGERPRINT`, `GH_BOT_GPG_KEY`, `GH_BOT_GPG_KEY_ID`, `NUGET_ORG_API_KEY`). Set them at repository level. Do not add `--env`: a secret set on an environment is read only by jobs that name that environment, and several jobs that need these secrets name none, so they would get an empty value.
```bash
gh secret set GH_APP_PRIVATE_KEY --repo InitialForce/wpf < new-private-key.pem
gh secret set ANTHROPIC_API_KEY  --repo InitialForce/wpf
gh secret set NUGET_ORG_API_KEY  --repo InitialForce/wpf
# Bot GPG key: all three together, and commit the new public key as
# docs/bot-gpg-pubkey.asc on if/main first (automerge-verify.sh and release.yml
# check signatures against that file and GH_BOT_GPG_FINGERPRINT).
gh secret set GH_BOT_GPG_KEY         --repo InitialForce/wpf < bot-private.asc
gh secret set GH_BOT_GPG_KEY_ID      --repo InitialForce/wpf
gh secret set GH_BOT_GPG_FINGERPRINT --repo InitialForce/wpf
```
For the App key, delete the old key in the GitHub App settings afterwards.

**After the planned move to environments**, the commands get `--env`: `NUGET_ORG_API_KEY` in `wpf-nuget-publish`, `GH_APP_*` and `GH_BOT_GPG_*` in `bot-credentials`, `ANTHROPIC_API_KEY` in `claude`, each limited to `if/main`. Make that move only together with a workflow change that adds the matching `environment:` to every job that reads the secret, then delete the repository copies.

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
# Step 1: Delete the version from GitHub Packages. release.yml pushes there with
# the workflow's own token; deleting needs your own login with the
# delete:packages scope (gh auth refresh -s read:packages,delete:packages),
# or the web UI: https://github.com/orgs/InitialForce/packages/nuget/InitialForce.WPF/versions
gh api /orgs/InitialForce/packages/nuget/InitialForce.WPF/versions \
  --jq ".[] | select(.name==\"$BAD_VERSION\") | .id"
VERSION_ID=<id>
gh api -X DELETE /orgs/InitialForce/packages/nuget/InitialForce.WPF/versions/$VERSION_ID

# Step 1b: Unlist the version on nuget.org (nuget.org 'delete' unlists; it cannot remove).
# Do this for both InitialForce.WPF and InitialForce.WPF.RuntimeOverride.
dotnet nuget delete InitialForce.WPF $BAD_VERSION \
  --source https://api.nuget.org/v3/index.json --api-key <nuget.org key> --non-interactive

# Step 2: Pin SC to last known-good in Directory.Packages.props
# Step 3: Revert the bad change on if/main through a PR (see I-4). The next
# auto-release publishes a new version without it.
```
MTTR target: < 2 hours detection-to-SC-hotfix.

---

### I-2: The Tip of `if/main` Was Lost or the Branch Was Deleted

**Detection:** commits missing from `if/main`, or the branch is gone; pipeline runs fail on checkout of `if/main`.

The ruleset `if-fork-protected-branches` blocks force pushes and deletion on `if/main` (and `if/release/*`). Repository admins and the bot App bypass it, so this happens only through one of those.

**Containment:**
```bash
gh variable set IF_AUTONOMY_ENABLED -b "false" --repo InitialForce/wpf
```

**Recovery:**
```bash
git fetch origin --tags
# Find the last good tip: a local clone that has it (git reflog), the merge
# commit of the newest merged pipeline PR (gh pr list --state merged --base if/main),
# or the newest release tag (git tag -l "if-10.0.*" | sort -V | tail -1).
GOOD=<sha>
CURRENT=<sha that if/main points at now, or empty when the branch is gone>
git log --oneline -5 $GOOD
# As an admin you bypass the ruleset; there is no need to turn it off.
git push origin $GOOD:refs/heads/if/main --force-with-lease=refs/heads/if/main:$CURRENT
python tools/ledger-validate.py --ledger-path .if-fork/patch-ledger.jsonl
gh variable set IF_AUTONOMY_ENABLED -b "true" --repo InitialForce/wpf
```
Ledger events written after `$GOOD` are lost with the branch tip. Discovery records new events again on its next run; re-run any ingestion whose `cherry_picked` event is missing.

Write an entry in `docs/DECISION_LOG.md` with what happened and which commit you restored.

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
jq -c 'select(.event=="cherry_picked") | {ts, pr_number, details}' .if-fork/patch-ledger.jsonl | tail -20
git bisect start && git bisect bad origin/if/main && git bisect good $LAST_KNOWN_GOOD_TAG
# At each step: dotnet test test/InitialForce.WpfSmoke/ -c Release
# Then: git bisect good/bad
```

**Remediation:** revert through a PR to `if/main`, merged by hand with a merge commit.
```bash
git checkout -b revert-<sha> origin/if/main
git revert <bad-commit-sha> --no-edit
git push origin revert-<sha>
gh pr create --repo InitialForce/wpf --base if/main --head revert-<sha> \
  --title "Revert <upstream PR>: regression found in SC validation"
# After merging:
python tools/ledger-event.py --event reverted --pr-number <upstream N> --head-sha <reviewed sha> \
  --actor <your GitHub login> --details-json '{"reason":"regression found in SC validation"}' --push
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

### I-6: Package Feed Outage

**Detection:** SC CI fails at `dotnet restore` with `Unable to resolve 'InitialForce.WPF'`.

`release.yml` publishes to nuget.org and to GitHub Packages. Check which feed SC restores from, then `https://status.nuget.org` or `https://www.githubstatus.com` ("Packages"). Typical outages are under an hour; wait. If over 4 hours: pin SC to a cached version, or point SC at the other feed. Log it in `docs/DECISION_LOG.md` if over 4 hours.

---

### I-7: CVE Drops on WPF `release/10.0`

**Detection:** GitHub advisory for `dotnet/wpf`; 24-hour SLA from public disclosure.

When the fix is on upstream `release/10.0`, the nightly sync brings it in. Run it now with `gh workflow run nightly-rebase.yml --repo InitialForce/wpf -f force_rebase=true`, merge the sync PR when Build passes (by hand if it needs a person), then release.

When the fix is only on upstream `main`, port it by hand:
```bash
SECURITY_SHA=<upstream-fix-commit>
git fetch upstream main
git checkout -b security/CVE-YYYY-NNNNN origin/if/main
git cherry-pick -x $SECURITY_SHA
git push origin security/CVE-YYYY-NNNNN
gh pr create --repo InitialForce/wpf --base if/main --head security/CVE-YYYY-NNNNN \
  --title "security: cherry-pick CVE-YYYY-NNNNN fix" --label security
```
`automerge.yml` never merges this PR, because it is not a pipeline branch. Merge it by hand with a merge commit when Build passes; `IF_AUTOMERGE_FROZEN` does not apply to people. Then release at once:
```bash
gh pr merge <PR-NUMBER> --repo InitialForce/wpf --merge
gh workflow run auto-release.yml --repo InitialForce/wpf -f force=true
```
Document the response in `docs/DECISION_LOG.md` with timestamp and justification.

MTTR target: 24 hours from CVE public disclosure.

---

### I-8: GitHub App Private Key Compromised

**Containment:** Revoke the installation token immediately in GitHub App settings (Settings → Developer Settings → GitHub Apps → initial-force-wpf-bot → Installations → Revoke). Then:
```bash
gh variable set IF_AUTONOMY_ENABLED -b "false" --repo InitialForce/wpf
```

**Remediation:** generate a new private key in the App settings, then set it as a repository secret (see "Rotate a secret"; no `--env` until the move to environments):
```bash
gh secret set GH_APP_PRIVATE_KEY --repo InitialForce/wpf < new-private-key.pem
# Delete the old key in the GitHub App settings.
gh variable set IF_AUTONOMY_ENABLED -b "true" --repo InitialForce/wpf
gh workflow run pr-discovery.yml --repo InitialForce/wpf  # checks that the new key works
```
Postmortem required. Review the bot's commits, PR merges and ledger events since the suspected leak (`git log origin/if/main --author=wpf-bot@initialforce.com`).

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
# Revert each applied change through a PR to if/main (as in I-4), then:
python tools/ledger-event.py --event reverted --pr-number <upstream N> --head-sha <reviewed sha> \
  --actor <your GitHub login> --details-json '{"reason":"author account compromise suspected"}' --push
# Remove the author from author_allowlist via a PR to .if-fork/config.yaml (CODEOWNERS review)
gh variable set IF_AUTONOMY_ENABLED -b "true" --repo InitialForce/wpf
```

---

### I-10: Ledger Tampering Detected

**Detection:** `tools/ledger-validate.py` fails on a hash mismatch, a changed or removed line, or (with `--strict-signature`) an unsigned ledger commit.

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
# Revert the tampering commit through a PR to if/main, merged by hand.
git checkout -b ledger-repair origin/if/main
git revert <bad-commit> --no-edit
git push origin ledger-repair
gh pr create --repo InitialForce/wpf --base if/main --head ledger-repair --title "Revert ledger tampering"
```
If an outside actor did it, rotate every secret as in "Rotate a secret" (repository secrets today, so no `--env`).
High-severity security event — file a separate incident report.

---

### I-11: `if/main` Accidentally Deleted

See I-2. The same recovery restores a deleted branch.

---

## First Runs on GitHub

The Phase-0 hand-off stubs (bulk review through candidate issues, a hand-pushed first tag, classic branch protection on `if/release/10.0`) no longer apply: discovery drains the backlog by itself at 10 reviews a day, `auto-release.yml` makes the tags, and a repository ruleset protects `if/main`. Before the pipeline is trusted to run alone, watch these first runs:

1. **Settings.** Remove the required reviewer from `wpf-nuget-publish` if releases should publish without you. Decide when to move the secrets into environments (see "Rotate a secret"). Set `IF_AUTOMERGE_FROZEN=false` when ready.
2. **Nightly sync.** One sync PR `if/staging -> if/main` opens. Without conflicts, it merges itself after Build. With conflicts, it carries `needs-human-merge`; review and merge it.
3. **First ingestion.** A `claude/cherry-pick-pr-<N>` PR opens and merges itself after Build, or explains in a comment why it needs you.
4. **automerge.yml.** The 30-minute runs show "No passed Build" or a merge for each open pipeline PR; none fail.
5. **First release.** `auto-release.yml` tags and dispatches `release.yml` on `if/main`. Check the packages on nuget.org and that SC restores the new version.
6. **Review disagreements.** Work each open `review-disagreement` issue as in "Escalate a review-disagreement issue".
