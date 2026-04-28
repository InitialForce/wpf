# Bootstrap Status — InitialForce/wpf

Last updated: 2026-04-28

## Autonomous-implementation phase: COMPLETE

The autonomous Opus-swarm phase implemented every bead that does not require
external GitHub admin, nuget.org, or Windows-runner access.

**Closed:** 32 of 50 beads (64%) across 7 rounds + 1 cleanup pass.
**Tests:** 646/646 pytest, mypy --strict clean, ruff clean.
**Commits:** 34 atomic commits on `if/main`.
**Ledger:** 223 entries (223 real-PR seeded candidates, no genesis test event), `tools/ledger-validate.py` exits 0.

## What's done

- All Python tooling: `tools/ledger-event.py`, `ledger-validate.py`, `regenerate-state.py`, `check-graduated.py`, `check-denylist.py`, `check-regression.py` (with R4-3 `--current-sha`), `merge-verdicts.py`, `dispatch-approved.py`, `seed-ledger.py`, `diff-smoke-results.py`, `check-config-schema.py`, `check-prompt-schema.py`, `cherry-pick-pre-flight.sh`, `compute-version.ps1`, `verify-msquic-pattern.ps1`.
- All workflows: `autonomy-check.yml` (kill-switch reusable), `build.yml`, `pr-discovery.yml`, `pr-review.yml` (2× independent Opus), `pr-ingestion.yml`, `nightly-rebase.yml`, `upstream-stable-adoption.yml`, `release.yml`, `claude-on-failure.yml`, `weekly-differential.yml`.
- All Claude prompts: 10 files in `.if-fork/prompts/` covering preamble, discovery, 2× review, cherry-pick, rebase, conflict resolution, release notes, failure analysis, audit logger.
- Configuration: `.if-fork/config.yaml` (canonical policy) + JSON Schema validator.
- Packaging: `packaging/InitialForce.WPF/` and `packaging/InitialForce.WPF.RuntimeOverride/` csproj + targets per msquic-pattern.
- Test harness: `test/InitialForce.WpfSmoke/` with 22 NUnit scenarios + BenchmarkDotNet perf harness + pixel-diff helper. Plus `test/InitialForce.WpfHelloWorld/` for msquic-pattern verification.
- Ledger: 223 candidates seeded (214 h3xds1nz + 9 other-author Tier-S/A with real PR numbers; 5 cross-fork/unnumbered moved to `docs/manual-candidates.md`).
- Documentation: `docs/operator-runbook.md`, `DECISION_LOG.md`, `KNOWN_RISKS.md`, `risk-register.md`, `known-limitations.md`, `NOTICE.md`, `BOOTSTRAP_STATUS.md` (this file).
- GitHub repo metadata: `CODEOWNERS`, 4 issue templates, PR template, `.gitignore`.

## What still requires Oystein (Phase 0/1 human gates)

| Bead | Type | Action |
|---|---|---|
| wpf-3sm | GitHub admin | Create `InitialForce/wpf` repo, push this staging tree as initial commit. |
| wpf-13l | GitHub admin | Create `initial-force-wpf-bot` GitHub App + install on the repo. |
| wpf-238 | GitHub admin | Branch protection on `if/main`, `if/release/*`, `claude/*`. |
| wpf-2lc | GitHub admin | Three Environments (`bot-credentials`, `wpf-nuget-publish`, `branch-promotion`) with reviewer protection. |
| wpf-ts4 | GitHub admin | Set repo variables `IF_AUTONOMY_ENABLED=false`, `IF_AUTOMERGE_FROZEN=true`, `IF_REVIEW_DOUBLE_REQUIRED=true` (start in safe state). |
| wpf-3ar | GitHub admin | Create `if/release/10.0` from upstream `v10.0.X` tag. |
| wpf-1gn | nuget.org | Reserve `InitialForce.*` prefix defensively. |
| wpf-1pt | Windows CI | First clean upstream `release/10.0` build green on `windows-latest`. |
| wpf-2hh | Operational | Bulk-process 223 candidates through 2× Opus review (uses Claude API; ~223 × 2 = 446 invocations). |
| wpf-j79 | Operator | Triage all `review-disagreement` issues to zero. |
| wpf-3vk | Operational | Auto-apply approved patches via `pr-ingestion.yml`. |
| wpf-2xo | Operator | First `release.yml` run + manual approve at `wpf-nuget-publish`. |
| wpf-23n | Swing Catalyst | Update `src/Directory.Build.props` + `packageSourceMapping`. |
| wpf-1o9 | Manual QA | Manual UI smoke against patched DLLs. |
| wpf-29a | Manual QA | Operator runbook validated by Oystein. |
| wpf-2wi | Operational | Enable cron triggers — autonomous weekly cadence. |
| wpf-1sr | Long-term | Achieve 4 consecutive weekly publishes without human cherry-pick. |

## Hand-off recipe

1. (Already complete in commit `958b648`.) The autonomous-fork tooling overlay was pushed onto the fork's `if/main` branch.
2. Run the Phase-0 human checklist above to set up GitHub App + branches + protection + environments + variables.
3. Toggle `IF_AUTONOMY_ENABLED=true` once you've manually run `pr-discovery.yml` and `pr-review.yml` once each on a single test PR to validate the Claude wiring.
4. Toggle `IF_AUTOMERGE_FROZEN=false` only after 4-PR test batch validates the cherry-pick + smoke + perf gates.
5. Bulk-process the 223 auto-pipeline candidates: trigger `pr-review.yml` per ledger entry (or write a small driver script that reads `.if-fork/patch-state.json` and dispatches in batches of ~20/hour). The 5 cross-fork/unnumbered entries in `docs/manual-candidates.md` are handled separately by the operator.

## Ledger correction (pre-launch)

Pre-launch ledger correction: 5 synthetic PR numbers (90001, 90010, 90011, 90012, 90013) dropped; tracked separately in `docs/manual-candidates.md`. Ledger now contains 223 real-PR entries (214 h3xds1nz + 9 other-author Tier-S/A with real dotnet/wpf PR numbers).

## Concurrency-race notes

During the swarm, occasional commit-attribution drift occurred when two agents called `git commit` near-simultaneously and the second swept up the first's staged-but-uncommitted files. After Round 1 we mitigated this with `git commit --only -- <files>` to scope each commit. All file content is correct; only some commit messages don't perfectly describe their full diff. The patch ledger and bead-close trail are the authoritative attribution record.

