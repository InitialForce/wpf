# Round 1 — Agent A: New Files Inventory
**Branch:** `if/main` vs `upstream/release/10.0`
**Total added files:** 159

---

## Summary Table

| Category | Path glob | File count | Purpose |
|---|---|---|---|
| GitHub CI/CD | `.github/workflows/` | 11 workflows | Fork-specific GitHub Actions automation pipeline |
| GitHub meta | `.github/CODEOWNERS`, `.github/ISSUE_TEMPLATE/` | 5 | Ownership rules + issue templates for the autonomous pipeline |
| Fork config | `.if-fork/` | 14 | Policy YAML, patch ledger, Claude prompt library |
| Python tools | `tools/` | 19 | CLI scripts powering CI workflows (ledger signing, review, regression checks) |
| Python tests | `tests/` | 55 | pytest suite verifying all `tools/` scripts and CI workflow contracts |
| .NET smoke tests | `test/InitialForce.WpfSmoke/` | 22 | NUnit smoke + BenchmarkDotNet perf harness for the WPF fork |
| .NET hello-world app | `test/InitialForce.WpfHelloWorld/` | 6 | Minimal WPF app used as a build/smoke sanity target |
| NuGet packaging | `packaging/` | 11 | `InitialForce.WPF` and `InitialForce.WPF.RuntimeOverride` package projects |
| Documentation | `docs/` | 8 | Operator runbook, risk register, decision log, known limitations |
| Perf baseline | `perf/` | 1 | Example BenchmarkDotNet JSON baseline for regression checks |
| Root-level | `NOTICE.md`, `pyproject.toml` | 2 | Legal attribution + Python project descriptor |

---

## Group Details

### `.github/workflows/` — 11 files
CI/CD workflows implementing the fully autonomous upstream-PR review-and-merge pipeline.

| File | Description |
|---|---|
| `build.yml` | Builds the fork on every PR to `if/main`/`if/staging` |
| `nightly-rebase.yml` | Scheduled (03:07 UTC daily) rebase onto `upstream/release/10.0` |
| `pr-discovery.yml` | Scans upstream `dotnet/wpf` for new community PRs and creates ledger entries |
| `pr-ingestion.yml` | Triggered by `pr-discovered` dispatch; pulls PR diff and kicks off review |
| `pr-review.yml` | Runs 2× independent Claude Opus reviewers, then merges verdicts |
| `release.yml` | Packs and publishes `InitialForce.WPF` NuGet packages (gated to `@oysteinkrog`) |
| `upstream-stable-adoption.yml` | Applies PRs that graduated from staging to `if/main` |
| `weekly-differential.yml` | Weekly diff of fork vs upstream to surface drift |
| `autonomy-check.yml` | Periodic health-check of the autonomous pipeline itself |
| `test-autonomy-check.yml` | CI job that runs the pytest suite for `tools/` |
| `claude-on-failure.yml` | Fires Claude analysis on any failed workflow run |

### `.github/ISSUE_TEMPLATE/` — 4 files + `CODEOWNERS`
Structured issue templates used by automated workflows to report pipeline events to human operators.

| File | Description |
|---|---|
| `cherry-pick-failure.yml` | Filed when a graduated PR fails to apply cleanly |
| `perf-regression.yml` | Filed when BenchmarkDotNet detects a regression vs baseline |
| `review-disagreement.yml` | Filed when the two Opus reviewers cannot reach consensus |
| `operator-followup.yml` | General escalation template for items requiring human action |
| `CODEOWNERS` | Enforces `@oysteinkrog` review for policy files, ledger, prompt library, and release/review workflows |

### `.if-fork/` — 14 files
Fork-specific configuration and Claude prompt library.

| File | Description |
|---|---|
| `config.yaml` | Canonical policy file: denylist, tier thresholds, allowlists; all Claude workflows read this |
| `patch-ledger.jsonl` | Append-only hash-chained JSONL ledger recording every discovered/reviewed/applied PR |
| `patch-state.json` | Regenerated summary of current ledger state (graduated/applied counts by tier) |
| `seed-input.json` | Bulk-import seed list of upstream PRs to pre-populate the ledger |
| `prompts/preamble.md` | 12 hard prohibitions inherited by every Claude prompt in this repo |
| `prompts/pr-review-1.md`, `pr-review-2.md` | Independent reviewer prompts (roles differ to avoid anchoring) |
| `prompts/cherry-pick.md` | Prompt driving the graduation/cherry-pick workflow |
| `prompts/rebase.md` | Prompt for the nightly rebase job |
| `prompts/failure-analysis.md` | Prompt for post-failure root-cause analysis |
| `prompts/release-notes.md` | Prompt generating release notes from ledger diffs |
| `prompts/resolve-rebase-conflict.md` | Prompt for conflict resolution during rebase |
| `prompts/pr-discovery.md` | Prompt for scanning upstream and scoring new PRs |
| `prompts/audit-logger.md` | Prompt for structured audit logging of Claude actions |

### `tools/` — 19 files
Python and shell scripts that implement the logic referenced by GitHub Actions workflows.

| File | Description |
|---|---|
| `ledger-event.py` | Appends a signed, hash-chained event to `patch-ledger.jsonl` |
| `ledger-validate.py` | Validates ledger hash chain integrity |
| `ledger_schema.py` | Shared schema constants (valid event types, required fields) |
| `merge-verdicts.py` | Combines two reviewer JSON verdicts → `approved`/`escalated`/`rejected` |
| `check-regression.py` | Compares BenchmarkDotNet JSON vs baseline; exits non-zero on regression |
| `check-graduated.py` | Checks whether a PR has reached graduation tier in the ledger |
| `check-denylist.py` | Checks a PR diff against the config denylist patterns |
| `check-config-schema.py` | Validates `config.yaml` against `config-schema.json` |
| `check-prompt-schema.py` | Validates that all `.if-fork/prompts/*.md` files declare required headers |
| `diff-smoke-results.py` | Diffs fork vs upstream smoke test XML results; exits non-zero on regressions |
| `dispatch-approved.py` | Fires a `repository_dispatch` to trigger the graduation workflow |
| `regenerate-state.py` | Rebuilds `patch-state.json` from the full ledger |
| `seed-ledger.py` | Seeds the ledger from `seed-input.json` (one-time bootstrap) |
| `config-schema.json` | JSON Schema for `config.yaml` |
| `cherry-pick-pre-flight.sh` | Bash: detects if upstream PR is already absorbed before cherry-pick |
| `compute-version.ps1` | PowerShell: computes NuGet package version from git history |
| `verify-msquic-pattern.ps1` + `.Tests.ps1` | PowerShell: verifies msquic usage pattern; Pester test |

### `tests/` — 55 files
pytest suite providing unit and integration coverage for all `tools/` scripts and workflow contracts.

| Sub-group | File count | Coverage |
|---|---|---|
| `tests/fixtures/` | 23 | JSON/JSONL/XML/YAML/patch fixture files for deterministic test inputs |
| `tests/test_ledger_*.py` | 4 | Ledger schema, event structure, hash-chain validation, state regeneration |
| `tests/test_*_workflow.py` | 13 | Contract tests for each GitHub Actions workflow (event names, gate chains, required steps) |
| `tests/test_check_*.py` | 5 | Unit tests for `check-*` tools |
| `tests/test_*.py` (other) | 10 | Merge-verdicts, diff-smoke-results, cherry-pick pre-flight, csproj validation |
| `conftest.py` | 1 | Pytest autouse fixtures normalising CI vs local environment |

### `test/InitialForce.WpfSmoke/` — 22 files
NUnit smoke test suite and BenchmarkDotNet perf harness running against the actual WPF fork assemblies.

| File | Description |
|---|---|
| `Smoke/SmokeBase.cs` | NUnit base class: STA-thread application host for all smoke tests |
| `Smoke/PixelDiffHelper.cs` + `PixelDiffTests.cs` | Pixel-level screenshot comparison (golden-image regression detection) |
| `Smoke/AnimationTests.cs` | Verifies WPF animation timing and completion |
| `Smoke/DataBindingTests.cs` | Data-binding correctness tests |
| `Smoke/VirtualizingPanelTests.cs` | Virtualizing panel layout correctness |
| `Smoke/FrugalListTests.cs`, `ListCollectionViewTests.cs`, etc. | Targeted regression guards for fork-specific optimizations |
| `Perf/PerfHarness.cs` + `PerfProgram.cs` | BenchmarkDotNet harness; results fed to `check-regression.py` |
| `Perf/BenchmarkConfig.cs` | BenchmarkDotNet configuration (warmup, iterations, exporters) |

### `test/InitialForce.WpfHelloWorld/` — 6 files
Minimal single-window WPF application used as a build and smoke sanity target.

### `packaging/` — 11 files
Two SDK-style NuGet package projects with no source code — they package the fork's WPF assemblies.

| Package | Description |
|---|---|
| `InitialForce.WPF` | Primary consumer package; delivers fork WPF assemblies via `buildTransitive` MSBuild props/targets |
| `InitialForce.WPF.RuntimeOverride` | Companion package that redirects the .NET runtime to use the fork's assemblies at runtime |

### `docs/` — 8 files

| File | Description |
|---|---|
| `operator-runbook.md` | Steady-state operations guide (6–10 h/month estimate, incident response) |
| `DECISION_LOG.md` | Append-only log of architectural/governance decisions (human + Claude) |
| `KNOWN_RISKS.md` | Detailed risk catalogue with mitigation strategies |
| `risk-register.md` | Condensed on-call cheat sheet mapping risks to detection signals and response steps |
| `BOOTSTRAP_STATUS.md` | Current status of the initial fork bootstrap process |
| `autonomy-check-usage.md` | Usage guide for the autonomy-check workflow |
| `known-limitations.md` | Documented limitations of the autonomous pipeline |
| `manual-candidates.md` | PRs identified as requiring manual review rather than autonomous processing |

### Root-level and misc — 3 files

| File | Description |
|---|---|
| `NOTICE.md` | MIT license attribution acknowledging `dotnet/wpf` and .NET Foundation origin |
| `pyproject.toml` | Python project descriptor for `wpf-fork-tools`; declares dependencies for `tools/` scripts |
| `perf/baseline-example.json` | Example BenchmarkDotNet JSON baseline (Intel i7-10700K, .NET 10, Windows 11) |

---

## Notes

- The `data/` directory exists locally (contains review reports from this session) but has **no files tracked in `if/main`** — it is ephemeral/gitignored and should not be documented as a fork artifact.
- All 159 added files are unique to the InitialForce fork; none appear in `dotnet/wpf release/10.0`.
