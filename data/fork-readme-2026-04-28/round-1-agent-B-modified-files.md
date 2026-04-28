# Round-1 Agent B: Modified & Deleted Files Inventory

**Scope:** `git diff --name-status upstream/release/10.0..if/main`
**Focus:** M (modified) and D (deleted) paths only — new files (A) are covered by Agent A.

---

## Summary

| Category | Count |
|---|---|
| Modified files (M) | 3 |
| Deleted files (D) | 5 |
| Fork commits | 24 |

---

## Modified Files

### 1. `.github/ISSUE_TEMPLATE/config.yml` — `+2 / -21`

Stripped all upstream contact links (dotnet/runtime, winforms, efcore, roslyn, aspnetcore, SDK) and disabled blank issues. Replaced with a two-line stub: `blank_issues_enabled: false` / `contact_links: []`. The fork has its own issue templates (cherry-pick-failure, operator-followup, perf-regression, review-disagreement) added as new files alongside this change.

### 2. `.gitignore` — `+10 / -0`

Appended a new section ("Initial Force WPF fork additions") adding Python toolchain artifacts: `__pycache__/`, `*.pyc`, `*.pyo`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.venv/`, `*.egg-info/`. These correspond to the Python-based CI tooling (`pyproject.toml`, `tests/` directory) added by the fork. The upstream content is untouched.

### 3. `README.md` — `+4 / -0`

Prepended a 4-line fork banner at the very top of the file:

```
> **Initial Force WPF fork** — this is a community fork of `dotnet/wpf` maintained by
> [Initial Force AS](https://initialforce.com) for our internal use, with additional
> community PRs cherry-picked under a 2× independent automated review gate.
> See [`docs/BOOTSTRAP_STATUS.md`](docs/BOOTSTRAP_STATUS.md) and
> [`NOTICE.md`](NOTICE.md) for details. The upstream README follows.
```

Followed by a horizontal rule. All upstream content is preserved verbatim below the separator.

---

## Deleted Files

| Path | Upstream lines | Reason for deletion |
|---|---|---|
| `.github/PULL_REQUEST_TEMPLATE.md` | 23 | Upstream PR template for dotnet/wpf contributors. Deleted because the fork does not accept external PRs in the same manner; the autonomous workflow uses `pr-ingestion.yml` instead. |
| `.github/workflows/backportPRs.yml` | 28 | Upstream workflow to backport PRs between dotnet/wpf branches via `/backport` comments. Not applicable — the fork has its own cherry-pick pipeline (`pr-ingestion.yml`, `pr-discovery.yml`). |
| `.github/workflows/locker.yml` | 36 | Upstream workflow that locked stale issues/PRs daily. The fork's issue lifecycle is managed differently (autonomous-check / operator-followup templates). |
| `.github/workflows/main.yml` | 13 | Upstream inter-branch merge workflow (triggered on push to `release/**`). Superseded by the fork's `build.yml` and `nightly-rebase.yml`. |
| `CODEOWNERS` | 6 | Upstream file assigning `@dotnet/wpf-developers` as default reviewer for all paths. Replaced by `.github/CODEOWNERS` (a new file added by the fork, covering fork-specific paths and the Initial Force team). |

---

## Fork Commit Log — 24 commits grouped by theme

### Bootstrap (1 commit)
- `958b648` Bootstrap Initial Force WPF autonomous-fork tooling

### Security / hardening fixes (12 commits — largest theme)
- `fc915e0` fix(pr-review.yml): kill-switch enforcement, merge-verdict needs gate, fail-closed double-review (CRIT-1/4, HIGH-1)
- `8fd70da` fix(build.yml): drop invalid job-level runner.os reference (was failing GA parse)
- `490d84c` fix(pr-ingestion.yml): kill-switch + ledger CLI + SHA smuggle + denylist range + needs + run race + automerge default (CRIT-1/2, HIGH-3..6, MED-6)
- `81c68a8` fix(check-regression.py): zero-baseline, new-scenarios, empty-comparison fail-closed (MED-1)
- `75da9cb` fix(release.yml + autonomy-check.yml): kill-switch + ledger CLI + tag reach/sig + needs + sha256 + default-deny (CRIT-1/2, HIGH-5/7, MED-3, LOW-3)
- `cefd5d2` fix(packaging): RID-condition WPF asset removal, fail-closed for unsupported RIDs, drop OverwriteReadOnlyFiles (HIGH-8, LOW-2)
- `dd185e6` fix(ledger): push commits, fail-closed GPG in CI, shared schema, line-hash trailer (CRIT-2/3, HIGH-2, MED-4/5, LOW-1)
- `7dcc5c2` test: align build_workflow + smoke_harness tests with post-fixup state
- `28c02e8` fix(ledger_schema): add rebase_failed, failure_analyzed, pre_flight_failed, review_single_path_warning event types
- `faaf9fa` test: add behavioral test for workflow event names match VALID_EVENTS schema (Review-6 finding)
- `886f107` fix(ledger-event): handle single-line ledger + rebase-conflict in retry (Review-4 findings)
- `d55e876` fix(workflows): gate-in-needs invariant + ledger CLI + HIGH-6 run-watch + event mismatches + verdict CLI + concurrency (CRIT-1/2, HIGH-6, multiple)

### Docs / operator runbook (1 commit)
- `cdee60d` docs(operator-runbook): fix command errors (I-1 NuGet scope, I-3/I-10/I-7/I-2 args, label OR search, version_override->tag, recovery procedures)

### Test / schema (4 commits)
- `bfe14ea` test(event-schema): document operator-only/Phase-0 future-use events as unused-OK
- `a3783e5` fix: scrub local-dev path leaks before public push
- `d04c26b` chore: remove inherited upstream workflows (not relevant to autonomous fork)
- `75da9cb` *(also counted above)*

### Python / CI tooling (4 commits)
- `56f5196` fix(ci): types-PyYAML, mypy tools-only, build placeholder marker, build-and-test needs cherry-pick
- `66c614a` style: wrap long lines to satisfy ruff E501 (line-length=100)
- `4b29772` fix(ci): use RSA-2048 for ephemeral GPG key (ed25519 batch syntax invalid)
- `f0fe81c` fix(ci): add jsonschema, ephemeral GPG key for ledger tests
- `3b174ba` fix(build.yml): make Strawberry Perl install idempotent
- `f75f8b0` test(conftest): scrub CI env per-test by default
- `64b0ad2` fix(conftest): remove unused 'import os'

> Note: some commits span multiple themes; the grouping above reflects their primary focus.

---

## Key Observations

1. **No WPF source files were modified or deleted.** Every M/D path is in `.github/` or a top-level meta-file. The fork leaves all of `src/`, `eng/`, and WPF runtime code untouched relative to `upstream/release/10.0`.

2. **Deletions are upstream-workflow cleanup.** The five deleted files are all upstream GitHub automation (PR template, backport bot, locker, inter-branch merge, dotnet CODEOWNERS) that have no relevance to an autonomous commercial fork.

3. **Modifications are additive or narrowing.** README and `.gitignore` are purely additive. The issue template config is narrowed (blank issues disabled, upstream contact links removed).

4. **24 commits are exclusively fork infrastructure.** All commits concern CI/CD workflows, Python test harness, packaging, ledger/audit tooling, and documentation — zero upstream WPF code changes introduced by the fork itself.
