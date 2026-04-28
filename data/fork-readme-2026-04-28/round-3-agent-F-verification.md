## Verified facts

| Claim | Source | Verified? | Independent value | Notes |
|---|---|---|---|---|
| 159 files added (A) | Agent A | CONFIRMED | `git diff --name-status … \| awk '{print $1}' \| sort \| uniq -c` → `159 A` | Exact match |
| 3 files modified (M) | Agent B | CONFIRMED | Independent count → `3 M` | Exact match |
| 5 files deleted (D) | Agent B | CONFIRMED | Independent count → `5 D` | Exact match |
| 24 commits on fork branch | Agent B | CONFIRMED | `git log --oneline upstream/release/10.0..if/main \| wc -l` → `24` | Exact match |
| `upstream/release/10.0` is the correct baseline | All agents | CONFIRMED | `git rev-parse upstream/release/10.0` resolves to `e2d332a…` — ref is valid and present | |
| 223 ledger entries | Agent C | CONFIRMED | `wc -l patch-ledger.jsonl` → `223` | Exact match |
| All 223 entries are `discovered` type | Agent C | CONFIRMED | Python counter: `discovered=223`, all other types=0 | Exact match |
| Tier breakdown S=106, A=83, B=34 | Agent C | CONFIRMED | Python counter from ledger | Exact match |
| Already-applied breakdown S=36, A=41, B=21 (total 98) | Agent C | CONFIRMED | Python from `if_already_applied_in_local_main` field | Exact match |
| Pending 125: 116 MERGED + 9 OPEN | Agent C | CONFIRMED | Python per-state count: MERGED S/A/B=64/39/13=116, OPEN S/A/B=6/3/0=9 | Exact match |
| PR date range 2024-05-30 to 2026-01-20 | Agent C | CONFIRMED | Python min/max on `created_at` field | Exact match |
| Line totals +30808/-64719 all-223 | Agent C | CONFIRMED | Python sum from ledger `additions`/`deletions` fields | Exact match |
| Line totals +15355/-27046 pending-125 | Agent C | CONFIRMED | Python sum on not-applied entries | Exact match |
| 222/223 entries have `Community Contribution` label; exception is PR 10877 | Agent C | CONFIRMED | Python counter; PR 10877 has empty `labels: []` | Exact match |
| No files under `src/` were modified | Agent B | CONFIRMED | `git diff --name-only … -- 'src/*'` → empty | Exact match |
| `pr-discovery.yml` schedule 04:17 UTC | Agent D | CONFIRMED | cron `"17 4 * * *"` | Exact match (Agent D says "04:17 UTC", correct) |
| `nightly-rebase.yml` schedule 03:07 UTC | Agent A | CONFIRMED | cron `"7 3 * * *"` | Exact match |
| Review models: both `claude-opus-4-7` | Agent D | CONFIRMED | Both `--model claude-opus-4-7` in pr-review.yml | Exact match |
| Discovery model: Claude Haiku | Agent D | CONFIRMED | `--model claude-haiku-4-5` in pr-discovery.yml | Exact match |
| Cherry-pick model: Claude Sonnet | Agent D | CONFIRMED | `--model claude-sonnet-4-6` in pr-ingestion.yml | Exact match |
| Release-notes model: Claude Sonnet | Agent D | CONFIRMED | `--model claude-sonnet-4-6` in release.yml | Exact match |
| 11 workflow files | Agent A | CONFIRMED | `ls .github/workflows/` → 11 files | Exact match |
| Review max-turns: reviewer 1 and 2 at specified limits | Agent D | DISCREPANCY | Agent D says "max 8 turns" for Haiku and "max 15 turns" for cherry-pick | See Issues below — review agents actually use max 12, not implied otherwise |
| Rebase conflict resolution: max 20 turns | Agent D | CONFIRMED | `--max-turns 20` in nightly-rebase.yml | Exact match |
| Cherry-pick max 15 turns | Agent D | CONFIRMED | `--max-turns 15` in pr-ingestion.yml | Exact match |
| Cherry-pick conflict resolution max 10 turns | Agent D | CONFIRMED | `--max-turns 10` in pr-ingestion.yml (second invocation) | Exact match |
| Reviewer temperatures 0.0 and 0.7 | Agent D | CONFIRMED | `REVIEW_TEMPERATURE: "0.0"` and `"0.7"` in pr-review.yml | Exact match |

---

## Potential issues for Agent E (or post-commit fix)

1. **[Agent D, Section 2 — Review turns]** Agent D states Haiku runs "max 8 turns" in the *discovery* context, and separately lists review agents without specifying their max-turns. The actual values: `pr-review.yml` uses `--max-turns 12` for both reviewer-1 and reviewer-2. Agent D does not explicitly claim 12, but if FORK.md repeats the "max 8 turns" figure for any reviewer it would be wrong. Verify what figure FORK.md places next to each stage's reviewer.

2. **[Agent C, Showcase table — PR 10668 title]** The table truncates the title as "[StyleCleanUp] Use GlobalSuppressions (IDE0090)". The actual ledger title is "[StyleCleanUp] Avoid legacy suppression format, use GlobalSuppressions **(IDE0077)**" — both the description wording and the IDE rule number differ. If FORK.md quotes this PR's purpose, it should use the accurate title or omit the IDE rule number.

3. **[Agent D, Section 3 — Ingestion job count]** Agent D lists "eight sequential jobs" in `pr-ingestion.yml`. The observable jobs are: gate, pre-flight, sha-smuggling-check, cherry-pick, denylist-check, build-and-test, perf-gate, open-pr — which is exactly 8. This is consistent. No issue here, just confirmation.

4. **[Agent C, Manual candidates]** The five manual candidates (90001–90013) are described as "NOT in `.if-fork/patch-ledger.jsonl`". This was not independently verified by counting ledger entries above 90000, but the `wc -l` count of 223 combined with the PR-number range (max observed in ledger: well below 90000) is consistent with this claim. Low risk.

5. **[Agent A, Root-level count]** Agent A lists "Root-level and misc — 3 files" (`NOTICE.md`, `pyproject.toml`, `perf/baseline-example.json`) but the section heading says "2" in the summary table row ("Root-level | `NOTICE.md`, `pyproject.toml` | 2"). The `perf/baseline-example.json` was counted separately in the `perf/` category (1 file), and `perf/` is a separate row. The discrepancy is that the section heading at the bottom says "3 files" but the `perf/` item is double-counted — the perf baseline is listed in both the `perf/` row (count=1) and the "Root-level and misc" section header (says "3"). Total count is still 159 (verified), so no overcounting in the aggregate. FORK.md should not claim `perf/` and the root section simultaneously contain `baseline-example.json`.

---

## Spot-check: PR numbers in ledger

| PR # | Claimed in | Exists in ledger? | Ledger state | Ledger title | Notes |
|---|---|---|---|---|---|
| 9967 | Agent C showcase table (tier S, MERGED, no fork, "Replace ArrayList in BamlMapTable") | YES | MERGED | "Replace use of ArrayList with List\<T\> in BamlMapTable for performance" | Title matches within truncation; tier S confirmed; applied=false confirmed |
| 9468 | Agent C showcase table (tier A, MERGED, no fork, "AvTrace: use params ReadOnlySpan") | YES | MERGED | "Modify AvTrace call chain to use params ReadOnlySpan\<object\> instead of an array" | Title matches; tier A confirmed; applied=false confirmed |
| 10877 | Agent C, "one unlabelled entry" | YES | OPEN | "Fix InvalidCastException in ComboBoxAutomationPeer / forward…" | `labels: []` confirmed — the one non-CC entry |

---

## Open uncertainties (don't block commit)

- The `docs/BOOTSTRAP_STATUS.md` "Phase 0/1 operator gates" referenced throughout Agent C/D has not been independently read. The pipeline-not-yet-live claim is consistent with all ledger entries being `discovered`-only, but the exact gate conditions are unverified.
- Agent C attributes "the overwhelming majority" of PRs to contributor **h3xds1nz**. This author attribution was not independently verified by parsing all 223 ledger entries for contributor metadata (the `seed-bulk-import` actor writes ledger entries, and individual author data was not spot-checked beyond the PR title patterns).
- The `patch-state.json` regenerated summary file's content has not been checked for consistency with the raw ledger counts. Low risk — it is a derived cache file.
- Agent D states rebase retries up to "3 retry attempts" for conflict resolution. The workflow file was not fully parsed for the retry loop count; only the `--max-turns 20` parameter was verified. If FORK.md quotes "3 retries", this should be confirmed against the workflow retry logic.
