## Inherits from preamble.md

All 12 hard prohibitions in `preamble.md` are in effect for this prompt.

---

## Role

Read `.if-fork/config.yaml` before doing anything else.

This file describes the nightly upstream sync for any agent asked to reason about it. The
sync itself is plain shell in `nightly-rebase.yml`; Claude is only invoked for conflicts,
through `resolve-rebase-conflict.md`.

The fork does not rebase. `if/main` carries merge commits and its ruleset forbids force
pushes, so the sync merges upstream into the fork instead:

1. If upstream `release/10.0` is already in `if/main`, stop.
2. If the open PR `if/staging -> if/main` already contains the upstream SHA, stop.
3. Reset `if/staging` to `if/main` and run `git merge --no-ff --no-commit <upstream SHA>`.
4. Conflicts on files the fork deleted under `.github/` are resolved by keeping the deletion.
   Conflicts on denylisted files escalate. Other conflicts go to Claude.
5. The merge may only touch files upstream changed. The workflow commits it, force-pushes
   `if/staging` (a bot-owned branch) and keeps exactly one sync PR open.
6. `build.yml` runs on the PR. `automerge.yml` merges it with a merge commit when the build
   passes and `IF_AUTOMERGE_FROZEN` is not `true`.
7. If the sync is blocked, the workflow keeps one open issue titled
   "Nightly upstream sync blocked" and emits one `rebase_failed` ledger event per upstream SHA.

**Model:** `claude-sonnet-4-6` (see `config.claude_models.conflict_resolve`)
**Trigger:** `nightly-rebase.yml` (03:07 UTC, or the `nightly-rebase` dispatch)

---

## Allowed tools

- `Bash`: read-only git (`git log`, `git diff`, `git show`, `git status`)
- `Read`, `Grep`

**Forbidden:** `git rebase` of `if/main`; any push; direct ledger writes.

---

## Inputs

Environment variables:

| Variable | Description |
|---|---|
| `CONFIG_PATH` | Path to `.if-fork/config.yaml` |
| `UPSTREAM_SHA` | Upstream commit being merged (never a branch name) |
| `ESCALATION_ISSUE_PATH` | Write a report here if escalating |

---

## Output contract

No output of its own. Conflict resolution follows the contract in
`resolve-rebase-conflict.md`.
