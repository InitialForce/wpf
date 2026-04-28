# Round-2 Agent C: Patch Ledger Analysis

**Source:** `.if-fork/patch-ledger.jsonl` (223 entries)
**Date:** 2026-04-28
**Status:** All 223 entries are `discovered` — no entries of any other type exist yet. No patches have been cherry-picked through the automated pipeline. This report describes the candidate queue, not applied patches.

---

## Event-Type Summary

| Event type | Count | Note |
|---|---|---|
| `discovered` | 223 | All ledger entries — candidates queued for 2x review |
| All other types | 0 | `review_1`, `approved`, `cherry_picked`, `published`, etc. |

The pipeline is in its pre-operational state. Every entry was written by `seed-bulk-import` on 2026-04-28T14:22–14:29Z as part of the initial ledger bootstrap.

---

## Candidate Breakdown

### By tier

| Tier | Definition | Count | Already in fork (pre-ledger) | Pending review |
|---|---|---|---|---|
| **S** | Highest value — correctness, thread-safety, perf wins | 106 | 36 | 70 |
| **A** | Strong value — allocation reduction, dead-code removal | 83 | 41 | 42 |
| **B** | Useful but lower urgency — style, test coverage, infra | 34 | 21 | 13 |
| **Total** | | **223** | **98** | **125** |

The 98 "already in fork" entries are PRs that were applied directly to `if/main` before the ledger was established (pre-ledger bootstrap). They are tracked for audit completeness but do not require pipeline processing.

### By upstream state (pending-only, 125 entries)

| Upstream state | S | A | B | Total |
|---|---|---|---|---|
| MERGED into `dotnet/wpf` main | 64 | 39 | 13 | **116** |
| Still OPEN in `dotnet/wpf` | 6 | 3 | 0 | **9** |

None of these 125 are in `upstream/release/10.0` — that is the selection criterion: the fork tracks merged-but-not-yet-released community improvements.

### PR date range

All 223 candidates were originally opened in `dotnet/wpf` between **2024-05-30** and **2026-01-20**.

### Aggregate line impact

| Scope | Additions | Deletions | Net |
|---|---|---|---|
| All 223 candidates | +30 808 | −64 719 | −33 911 |
| 125 pending (not yet in fork) | +15 355 | −27 046 | −11 691 |

The strongly negative net reflects the dominant theme: dead-code removal, de-allocation, and modernisation replacing legacy patterns with leaner equivalents.

---

## Showcase: Top 10 Candidates by Tier + Size

All PRs are in `dotnet/wpf`. "Fork" column = already applied pre-ledger.

| PR | Tier | Upstream | +lines | −lines | Fork | Title (truncated to 60 chars) |
|---|---|---|---|---|---|---|
| [#10684](https://github.com/dotnet/wpf/pull/10684) | S | OPEN | +93 | −105 | yes | Replace boxing Hashtable in Grid's Measure, improve perf |
| [#10874](https://github.com/dotnet/wpf/pull/10874) | S | OPEN | +30 | −168 | yes | Remove non-CLS exception handlers in LineServicesCallbacks |
| [#9967](https://github.com/dotnet/wpf/pull/9967) | S | MERGED | +104 | −94 | **no** | Replace ArrayList with List\<T\> in BamlMapTable |
| [#9888](https://github.com/dotnet/wpf/pull/9888) | S | OPEN | +79 | −117 | yes | Optimize FigureLength struct conversion, reduce allocs |
| [#10630](https://github.com/dotnet/wpf/pull/10630) | S | OPEN | +88 | −101 | yes | Optimize ComputerInkBoundingBox(LtoR), remove extra branch |
| [#10668](https://github.com/dotnet/wpf/pull/10668) | A | OPEN | +365 | −377 | yes | [StyleCleanUp] Use GlobalSuppressions (IDE0090) |
| [#9981](https://github.com/dotnet/wpf/pull/9981) | A | OPEN | +233 | −488 | yes | Optimize EllipseGeometry/RectangleGeometry, reduce allocs |
| [#9468](https://github.com/dotnet/wpf/pull/9468) | A | MERGED | +111 | −570 | **no** | AvTrace: use params ReadOnlySpan\<object\> |
| [#10021](https://github.com/dotnet/wpf/pull/10021) | B | MERGED | +3 808 | −4 076 | **no** | [StyleCleanUp] Add missing accessibility modifiers (IDE0040) |
| [#10903](https://github.com/dotnet/wpf/pull/10903) | B | OPEN | +3 111 | −4 356 | yes | Remove ElementUtil allocs, stop boxing in DispatcherOperation |

All 223 entries carry the `Community Contribution` label (222 of 223; one unlabelled entry is [#10877](https://github.com/dotnet/wpf/pull/10877)). The overwhelming majority of these PRs originate from a single prolific community contributor to `dotnet/wpf` — **h3xds1nz** — who is the dominant author of the allocation-reduction and dead-code-removal work tracked here.

---

## Manual Candidates (outside the automated pipeline)

`docs/manual-candidates.md` lists 5 patches that cannot be processed by `pr-review.yml` because they have no real `dotnet/wpf` PR number:

| Synthetic # | Source fork | Tier | Summary |
|---|---|---|---|
| 90001 | `dotnet/wpf` (author: miloush) | S | Bucket placeholder — individual PR numbers TBD |
| 90010 | `Faithlife/wpf` (`traceroutedevent-allocations`) | A | Reduce allocations in EventRoute |
| 90011 | `Faithlife/wpf` (`improve-read-interop`) | S | Eliminate allocation in StreamAsIStream.Read |
| 90012 | `Faithlife/wpf` (`close-image-stream`) | S | Fix stream leak when creating ImageSource from Uri |
| 90013 | `dotnet-campus/wpf` (`t/lindexi/WeakEventTable`) | S | Add thread-safety lock in WeakEventTable |

These require manual operator cherry-pick via direct `cherry-pick.md` prompt invocation once Phase 0/1 GitHub setup is live. They are NOT in `.if-fork/patch-ledger.jsonl`.

---

## Current Pipeline State

As of 2026-04-28, the fork is at **Phase 0** (bootstrap complete, pipeline not yet live):

- **Zero** patches have been through the automated 2x review gate.
- **98** patches were applied to `if/main` directly during the bootstrap phase and are recorded in the ledger for audit completeness.
- **125** candidates (116 upstream-merged + 9 upstream-open) are queued and awaiting the first `pr-review.yml` run.
- The pipeline trigger (`pr-discovery.yml` → `pr-ingestion.yml` → `pr-review.yml`) becomes active once the Phase 0/1 operator gates in `docs/BOOTSTRAP_STATUS.md` are cleared.
