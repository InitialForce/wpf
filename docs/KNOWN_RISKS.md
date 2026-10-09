# Known Risks — InitialForce WPF Fork

**Date:** 2026-04-27, corrected against the pipeline on 2026-10-09
**Owner:** Øystein Krog
**Sources:** PLAN.md §11, round-3-critique (pessimist, security, operations), round-4-critique (2× review skeptic)

Each entry: severity (critical/high/med/low), mitigation status, owner, related runbook/register ID.
Statements about the pipeline describe branch `fix/autonomy-pipeline` as of 2026-10-09; repository settings were read from GitHub the same day.

---

## RISK-001: Correlated LLM reviewer failure modes (2× review)

**Severity:** high
**Status:** monitoring

Both reviewers run the same model, `claude-fable-5-1` (`claude_models.review_1` and `review_2` in `.if-fork/config.yaml`), on the same pinned diff, with the same hard-fail pattern list. Same-model, same-input reviewers have correlated errors. A prompt injection that fools one instance may fool both.

**Mitigation:**
- The two reviewers have different prompts (`pr-review-1.md`, `pr-review-2.md`) and run as separate jobs. Each verdict is accepted only from its own job's artifact.
- `tools/merge-verdicts.py` applies the hard-fail patterns, `file_denylist` and the tier confidence floor deterministically to the pinned diff after the reviews. A hit turns `approved` into `escalated`, whatever the reviewers said.
- A conflict resolution written by Claude after review is never merged without a person (RISK-017).
- Roslyn analyzer (deterministic third gate): not built.

**Owner:** Øystein Krog

**Related:** DECISION_LOG.md DEC-003, round-4-critique/01-2x-review-skeptic.md finding #1, risk-register.md R1

---

## RISK-002: Prompt injection via upstream PR content

**Severity:** high
**Status:** partly mitigated

An attacker who can open a PR on `dotnet/wpf` can put instructions in the PR body, a code comment or a conflict hunk, to steer a reviewer or the conflict agent.

**Mitigation:**
- Reviewers read only a pinned diff artifact (`review-input/`), never the live PR. Prompts mark that content as `<untrusted_input>`.
- Hard-fail patterns in `review_hard_fail_patterns` (`[DllImport`, `unsafe`, `extern`, `base64`, URLs outside github.com and microsoft.com, `[SecurityCritical]`, `[LinkDemand]`, `Demand()`, `Process.Start`, `AppContext.TryGetSwitch`, `Assembly.Load*`, `BinaryFormatter`, `XmlSerializer.Deserialize`) are checked by `merge-verdicts.py` on the diff, not by the model. A match escalates.
- Every Claude step loads only trusted settings (`--setting-sources user`, `--strict-mcp-config`, hooks off), has no shell, and has reads of `/proc` and the runner's `_temp` folder denied. Tool rules limit writes to the step's output files.
- No Claude job holds the App token, the GPG key or a stored git credential. They do hold `ANTHROPIC_API_KEY`.
- Conflict resolutions go to a person before merge (RISK-017).

**Residual risk:** Read deny rules are applied by Claude Code on a best-effort basis to Grep and Glob; nobody has tested on GitHub whether `/proc/self/environ` is out of reach in every tool. A leaked `ANTHROPIC_API_KEY` costs money but gives no write access to the repository.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.9, round-3-critique/04-security.md finding #3

---

## RISK-003: No Authenticode signing — dependency confusion attack surface

**Severity:** med
**Status:** needs review

The packages are not Authenticode-signed. `release.yml` publishes `InitialForce.WPF` and `InitialForce.WPF.RuntimeOverride` to public nuget.org (24 versions are public there) and to GitHub Packages. The original mitigation assumed a private GitHub Packages feed only.

**Mitigation:**
- The package IDs are published from this repository's nuget.org account, so nobody else can publish under those exact IDs.
- `packageSourceMapping` in SC's `NuGet.config` should map `InitialForce.*` to one feed. Not checked against SC's current config.
- Whether the `InitialForce.*` prefix is reserved on nuget.org is not checked.
- Authenticode signing: deferred (known-limitations.md §Signing).

**Owner:** Øystein Krog

**Related:** DECISION_LOG.md DEC-002, DEC-009, known-limitations.md §Signing, round-3-critique/04-security.md finding #1

---

## RISK-004: Repository-level secrets and the App's ruleset bypass

**Severity:** high
**Status:** open (owner action planned)

All seven secrets (`ANTHROPIC_API_KEY`, `GH_APP_ID`, `GH_APP_PRIVATE_KEY`, `GH_BOT_GPG_FINGERPRINT`, `GH_BOT_GPG_KEY`, `GH_BOT_GPG_KEY_ID`, `NUGET_ORG_API_KEY`) are repository secrets. The environments `bot-credentials` and `wpf-nuget-publish` hold none and have no branch policy. So any workflow on any branch of this repository, including a same-repository PR's own copy of a workflow, can read every secret. The App has `contents: write`, and the ruleset `if-fork-protected-branches` (deletion and force-push rules on `if/main` and `if/release/*`) lists the App and repository admins as bypass actors. The ruleset requires no status check and no code-owner review.

**Mitigation:**
- Pipeline jobs that run candidate code or an agent hold no App token and no GPG key, and check out without stored credentials.
- `automerge.yml` merges only bot-signed pipeline PRs that pass `tools/automerge-verify.sh`, and never a change to a fork-owned path (`.github/`, `.if-fork/`, `tools/`, `docs/` and others).
- Planned by the owner: move `NUGET_ORG_API_KEY` into `wpf-nuget-publish`, `GH_APP_*` and `GH_BOT_GPG_*` into `bot-credentials`, and `ANTHROPIC_API_KEY` into a new `claude` environment, each limited to `if/main`. Every job that reads a secret must then name its environment.
- App key rotation: monthly per the operator runbook ("Rotate a secret").
- CODEOWNERS lists `@oysteinkrog` for every path, but nothing enforces it until the ruleset requires code-owner review.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.7, round-3-critique/04-security.md finding #2, operator-runbook.md "Rotate a secret"

---

## RISK-005: Ledger tampering

**Severity:** high
**Status:** partly mitigated

`patch-ledger.jsonl` decides what gets applied: ingestion and automerge require an `approved` outcome for the exact upstream head. The bot and any repository admin can write it.

**Mitigation:**
- Each entry carries the hash of the previous one. `tools/ledger-validate.py` runs in every `Build` and fails when an old line changed or was removed.
- Ledger commits are GPG-signed by the bot. `--strict-signature` checks that, but only when run by hand (weekly checklist).
- `.if-fork/` is fork-owned: `automerge.yml` never merges a pipeline PR that changes it.
- There is no separate audit branch and no external transparency log. `patch-state.json` is stale and not checked.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.10, operator-runbook.md I-10, round-3-critique/04-security.md finding #7

---

## RISK-006: Allowlisted contributor account compromise

**Severity:** high
**Status:** monitoring

`h3xds1nz` and other allowlisted contributors are known, targetable GitHub identities. An attacker who controls one can open a PR that discovery picks up and two Fable reviewers may approve. If it applies cleanly, touches only `src/` or `eng/`, and passes Build, `automerge.yml` merges it and the next `auto-release.yml` publishes it.

**Mitigation:**
- Two independent reviews must both approve, and the deterministic checks in `merge-verdicts.py` must pass.
- The ledger records each PR's events; incident response can find every patch from an author (operator-runbook.md I-9).
- `author_allowlist` lives in `.if-fork/config.yaml`, which is fork-owned, so only a person can change it.
- There is no velocity check on allowlisted authors.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.9, round-3-critique/04-security.md finding #4, operator-runbook.md I-9

---

## RISK-007: Cherry-pick SHA smuggling (force-push between review and ingestion)

**Severity:** high
**Status:** mitigated

Between review and ingestion a contributor can force-push a new commit to their PR branch. If ingestion applied the new head, it would apply a commit nobody reviewed.

**Mitigation:**
- Review pins `head_sha`, and the review dispatch carries it.
- `tools/check-approval.py` requires an `approved` outcome for that exact `(pr_number, head_sha)`, with no later `rejected` or `escalated`.
- `tools/apply-upstream-pr.sh` refuses when the PR's current head is not the pinned SHA (`head_moved_after_review`).
- `automerge.yml` checks the `Upstream-PR-Head` trailer against the ledger again before merging.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.10, round-3-critique/04-security.md finding #5

---

## RISK-008: Native renderer drift — `PresentationNative_cor3.dll` consumed from MS runtime pack

**Severity:** med
**Status:** accepted

Our NuGet ships managed assemblies only. The native renderer is consumed unchanged from Microsoft's runtime pack on the consumer machine. If Microsoft fixes a native rendering bug in a new runtime pack version and SC does not update its .NET SDK/runtime, SC will continue using the buggy native renderer even after the fork ships a managed fix.

Additionally, a native CVE fixed in Microsoft's runtime pack is not visible in our `git log` scan and does not appear in `patch-ledger.jsonl`. SC may lag on native CVE fixes until Oystein notices the advisory manually.

**Mitigation:**
- Monthly CVE scan (operator-runbook.md monthly checklist) cross-references GitHub Advisories for `dotnet/wpf`.
- Known-limitations.md documents this gap explicitly.
- v2 plan: `InitialForce.WpfGfx.Native` if a specific native bug warrants native patching.

**Owner:** Øystein Krog

**Related:** known-limitations.md §Native Renderer, PLAN.md §3.3

---

## RISK-009: RuntimePackAsset swap fires incorrectly or silently fails

**Severity:** med
**Status:** monitoring

The `RuntimePackAsset` override target fires at `AfterTargets="Build;CopyFilesToOutputDirectory"`. If the hook fires too early (before the RuntimePack is fully resolved) or if a consumer uses a custom SDK that overrides `ResolveRuntimePackAssets`, the Microsoft DLL wins silently — the fork's fixes are not present in the output. No error is raised.

**Mitigation:**
- Phase 1 step P1-3 verifies the swap fires correctly by checking the informational version of the output DLL for the `if.YYYYMMDD` suffix.
- Release checklist includes a spot-check of the published NuGet's DLL versions.
- known-limitations.md §RuntimePackAsset documents this gap.

**Owner:** Øystein Krog

**Related:** known-limitations.md §Runtime Pack Asset Override, PLAN.md §3.4, round-3-critique/01-pessimist.md finding #3

---

## RISK-010: Roslyn analyzer (deterministic third gate) not built

**Severity:** med
**Status:** accepted

`Assembly.Load*`, `BinaryFormatter`, `Process.Start` and `AppContext.TryGetSwitch` are on `review_hard_fail_patterns`, which `merge-verdicts.py` matches as regular expressions against the diff text. A regex misses equivalent code written another way (reflection, an alias, a helper in another file). A Roslyn analyzer would see the semantics.

**Mitigation:**
- The hard-fail regexes, applied deterministically.
- Both reviewers' checklists.
- Roslyn analyzer (`tools/analyzers/RoslynForkPolicy/`, rules IFP001 to IFP005): planned, not built.

**Owner:** Øystein Krog

**Related:** known-limitations.md §Roslyn analyzer, round-4-critique/01-2x-review-skeptic.md finding #5

---

## RISK-011: Patch stack growth raises the conflict rate

**Severity:** med
**Status:** monitoring

As the fork carries more patches, more upstream changes conflict with them, both in the nightly merge of upstream `release/10.0` and in new cherry-picks. Each conflict that Claude resolves now needs a person to review and merge (RISK-017), so the person's load grows with the stack.

**Mitigation:**
- The ingestion pre-flight check (`tools/cherry-pick-pre-flight.sh`) skips a PR that upstream already contains.
- A blocked nightly sync is retried only when upstream moves or a person asks, so a stuck conflict costs one Claude run, not one a night.
- Not in place: a stack-size cap, a scheduled keep-or-retire review of old patches, `rerere`.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.2, round-3-critique/01-pessimist.md finding #8

---

## RISK-012: Single key holder for catastrophic ops

**Severity:** med
**Status:** accepted (revisit when second engineer joins)

Catastrophic operations (NuGet unlist, branch force-push recovery, credential rotation) formally require two human approvers. The second approver is not currently designated. If Oystein is unavailable during a production incident, no one else can execute these operations.

**Mitigation:**
- Documented as an explicit accepted risk (DECISION_LOG.md DEC-012).
- Operator runbook covers all incident runbooks in detail so a second engineer can be briefed quickly.
- Revisit trigger: when a second engineer joins Initial Force's platform team.

**Owner:** Øystein Krog

**Related:** DECISION_LOG.md DEC-012, PLAN.md §3 decision 12

---

## RISK-013: A package feed outage breaks SC restore

**Severity:** med
**Status:** accepted

SC cannot build if the feed it restores `InitialForce.WPF` from is down and its NuGet cache is cold. The packages are on nuget.org and GitHub Packages.

**Mitigation:**
- SC CI caches the NuGet package directory, so a warm cache survives a short outage.
- The second feed can stand in for the first.
- Incident runbook I-6 covers the response.

**Owner:** Øystein Krog

**Related:** operator-runbook.md I-6, round-3-critique/01-pessimist.md finding #9

---

## RISK-014: The ledger is the only audit record

**Severity:** med
**Status:** accepted

There is no `audit/` branch and no Sigstore record; earlier plans described both. The audit trail is the ledger (RISK-005), the signed git history of `if/main`, PR comments and workflow run logs (kept 90 days by default). The bot can write all of these except run logs.

**Mitigation:**
- Ledger hash chain and signatures (RISK-005).
- Weekly `--strict-signature` check by hand.

**Owner:** Øystein Krog

**Related:** round-3-critique/04-security.md finding #7

---

## RISK-015: DncEng package accessibility not confirmed on clean runners

**Severity:** high
**Status:** open — must verify in Phase 1

The `Microsoft.DotNet.Wpf.DncEng` package (contains `bilinearspan.lib`, needed for the C++ build step) may not be accessible from a clean `windows-latest` GitHub Actions runner. Round-2 fact-checkers contradict each other on whether this package is on a public feed. If inaccessible, the bootstrap fails entirely at the C++ build step.

**Mitigation:**
- Phase 1 step P1-2 must perform a clean `dotnet restore` from outside Microsoft credentials before committing to the autonomous pipeline.
- Fallback: vendor the `PresentationNative_cor3.dll` binary directly in the fork — bypasses the DncEng dependency for managed-only builds.

**Owner:** Øystein Krog

**Related:** PLAN.md §12 open question 1, round-3-critique/01-pessimist.md finding #1

---

## RISK-016: The perf gates measure nothing

**Severity:** high
**Status:** open

The `perf` job in `build.yml` runs no benchmark. The release perf gate runs `test/InitialForce.WpfSmoke/Perf/PerfHarness.cs`, whose benchmarks are placeholder loops that do not exercise WPF, against `perf/baseline.json`, whose numbers were never measured. A WPF performance or allocation regression merges and ships without any gate noticing.

**Mitigation:**
- None in the pipeline. Regressions are found only by SC validation (operator-runbook.md I-4).
- Needed: benchmarks that load the packed WPF assemblies and exercise real paths, and a measured baseline.

**Owner:** Øystein Krog

**Related:** FORK.md "Pipeline status", operator-runbook.md "Regenerate the perf baseline"

---

## RISK-017: Unreviewed conflict resolutions by Claude

**Severity:** high
**Status:** mitigated by a human gate

The two reviews cover the upstream diff. When the cherry-pick or the nightly merge conflicts, Claude writes the conflicted files, and its input includes the conflict hunks, which come from upstream. Nothing checks that content automatically.

**Mitigation:**
- The trusted `finalize` job marks such commits with the trailer `Conflict-Resolved-By: claude`. `automerge.yml` refuses that trailer, and for the sync PR any file that differs from git's own merge. The PR gets the label `needs-human-merge`, and a person reviews and merges it.
- Claude cannot touch denylisted or fork-owned files: those conflicts escalate before Claude starts.

**Residual risk:** the person may approve without reading the resolution closely. The runbook ("Merge a PR that needs a person") says what to compare.

**Owner:** Øystein Krog

**Related:** operator-runbook.md "Merge a PR that needs a person"

---

## RISK-018: Unpinned base commit when automerge merges

**Severity:** med
**Status:** partly mitigated

GitHub's merge API checks the PR head (`--match-head-commit`) but not the base. If `if/main` moves between verification and the merge call, the PR merges onto a base it was not built with.

**Mitigation:**
- Merges run one at a time (concurrency group `automerge-if-main`), and only `automerge.yml`, a person and ledger commits write `if/main`.
- `tools/automerge-run.sh` fetches `if/main` again right before `gh pr merge` and does not merge when it moved. What remains is the moment between that fetch and the merge call.
- The next `Build` on `if/main` and the next release build test the merged result.
- A full fix needs GitHub's merge queue or a required, strict status check on `if/main`.

**Owner:** Øystein Krog

**Related:** FORK.md stage 5, operator-runbook.md "Auto-merge trust checks"
