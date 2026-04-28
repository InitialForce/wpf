# Manual Candidates — Cross-Fork and Unnumbered Patches

These 5 patches were removed from the auto-pipeline ledger because they do not have
real `dotnet/wpf` PR numbers. The automated `pr-review.yml` workflow fetches upstream
diffs by PR number from `dotnet/wpf`; synthetic or cross-fork numbers will fail to
fetch.

The operator processes these via direct `cherry-pick.md` prompt invocations once
GitHub setup is live (Phase 0/1 human gates complete). Do NOT add these to
`.if-fork/seed-input.json` or `.if-fork/patch-ledger.jsonl`.

---

## Entry 1 — miloush round-1-triage-bucket placeholder

- **Assigned synthetic PR number:** 90001 (invalid — not a real dotnet/wpf PR)
- **Author:** miloush
- **Source:** round-1-triage-bucket (round-1-triage placeholder)
- **Title:** [miloush bucket] miloush Tier-S/A patches (round-1-triage-bucket placeholder)
- **Tier:** S
- **Summary:** This was a placeholder bucket entry created during round-1 triage to
  represent miloush-authored patches that had not yet been individually identified with
  real PR numbers. No commit SHA or upstream PR exists.
- **Why not in auto-pipeline:** No real `dotnet/wpf` PR number. The bucket entry must be
  resolved into specific PRs with real numbers before it can be processed.
- **Suggested handling:** Identify the individual miloush PRs in `dotnet/wpf` and add each
  one individually to `seed-input.json` with their real PR numbers, then re-run
  `seed-ledger.py`. Alternatively, cherry-pick directly from the known commit SHA
  `2b4a28dd04b78151fcff28fcf8d5f9e8205b2de6` once confirmed.

---

## Entry 2 — Faithlife: Reduce allocations in EventRoute (traceroutedevent-allocations)

- **Assigned synthetic PR number:** 90010 (invalid — cross-fork branch, not a dotnet/wpf PR)
- **Author:** faithlife
- **Source fork:** `Faithlife/wpf`
- **Branch:** `faithlife/traceroutedevent-allocations`
- **Commit SHA:** `e8100efdad32f1ef0e223a649375ce32f96e6c24`
- **URL:** https://github.com/Faithlife/wpf/tree/faithlife/traceroutedevent-allocations
- **Tier:** A
- **Summary:** Reduces allocations when tracing routed events by changing EventRoute's
  `List<Handler>` backing store. Estimated +40 / -10 lines, 3 files changed.
- **Why not in auto-pipeline:** Lives in `Faithlife/wpf`, not `dotnet/wpf`. No upstream PR
  number exists; the diff cannot be fetched by the `pr-review.yml` pipeline.
- **Suggested handling:** Operator manually cherry-picks the branch tip commit from the
  Faithlife fork and invokes the `cherry-pick.md` prompt directly. Add a ledger entry
  using `ledger-event.py` with `event=cherry_picked` and `pr_number` set to the real
  upstream dotnet/wpf PR number if Faithlife ever submits one, or use a locally-assigned
  tracking number in the `20000-29999` range reserved for cross-fork patches.

---

## Entry 3 — Faithlife: Eliminate allocation in StreamAsIStream.Read (improve-read-interop)

- **Assigned synthetic PR number:** 90011 (invalid — cross-fork branch, not a dotnet/wpf PR)
- **Author:** faithlife
- **Source fork:** `Faithlife/wpf`
- **Branch:** `faithlife/improve-read-interop`
- **Commit SHA:** `bc27d57dba85761203ada3211c406de1e85c9ed8`
- **URL:** https://github.com/Faithlife/wpf/tree/faithlife/improve-read-interop
- **Tier:** S
- **Summary:** Eliminates an allocation in `StreamAsIStream.Read` interop path.
  Estimated +15 / -5 lines, 1 file changed.
- **Why not in auto-pipeline:** Lives in `Faithlife/wpf`. No upstream PR number.
- **Suggested handling:** Same as Entry 2 — manual cherry-pick from the Faithlife fork
  branch tip, direct `cherry-pick.md` invocation by operator.

---

## Entry 4 — Faithlife: Close Stream when creating ImageSource from Uri (close-image-stream)

- **Assigned synthetic PR number:** 90012 (invalid — cross-fork branch, not a dotnet/wpf PR)
- **Author:** faithlife
- **Source fork:** `Faithlife/wpf`
- **Branch:** `faithlife/close-image-stream`
- **Commit SHA:** `607f1875648d018b4449a0c5adc48b1c1ef51a82`
- **URL:** https://github.com/Faithlife/wpf/tree/faithlife/close-image-stream
- **Tier:** S
- **Summary:** Fixes a stream leak — closes the `Stream` when creating an `ImageSource`
  from a `Uri`. Fixes Faithlife issue #6842. Estimated +8 / -2 lines, 1 file changed.
- **Why not in auto-pipeline:** Lives in `Faithlife/wpf`. No upstream PR number. This is
  also a correctness/leak fix (not just a perf patch), so operator review before
  cherry-pick is especially recommended.
- **Suggested handling:** Manual cherry-pick from Faithlife fork branch tip. Because this
  is a correctness fix, operator should verify no conflict with upstream changes to the
  image-loading path before applying.

---

## Entry 5 — dotnet-campus: Thread-safety lock in WeakEventTable (t/lindexi/WeakEventTable)

- **Assigned synthetic PR number:** 90013 (invalid — cross-fork branch, not a dotnet/wpf PR)
- **Author:** lindexi (via dotnet-campus fork)
- **Source fork:** `dotnet-campus/wpf`
- **Branch:** `t/lindexi/WeakEventTable`
- **Commit SHA:** `651b5b27101cead64b77c132257c0cba040373d4`
- **URL:** https://github.com/dotnet-campus/wpf/tree/t/lindexi/WeakEventTable
- **Tier:** S
- **Summary:** Adds a thread-safety lock in `WeakEventTable`. Estimated +25 / -5 lines,
  1 file changed. Note: lindexi also has a real `dotnet/wpf` PR (#11139) already in the
  ledger; this is a separate patch from the dotnet-campus fork and distinct from that PR.
- **Why not in auto-pipeline:** Lives in `dotnet-campus/wpf`, not `dotnet/wpf`. No upstream
  PR number. Also note this patch touches concurrency-sensitive code (`WeakEventTable`) —
  extra care required.
- **Suggested handling:** Manual cherry-pick from dotnet-campus fork branch tip. Operator
  should run the regression test suite (`test/InitialForce.WpfSmoke/`) after applying,
  with particular attention to weak-event-related scenarios. Direct `cherry-pick.md`
  invocation by operator once GitHub setup is live.

---

## Processing instructions for operator

1. Ensure Phase 0/1 GitHub setup is complete (see `docs/BOOTSTRAP_STATUS.md`).
2. For each entry:
   a. Clone or fetch the source fork.
   b. Inspect the branch/commit diff manually.
   c. If acceptable, invoke the `.if-fork/prompts/cherry-pick.md` prompt with the
      commit SHA and a locally-assigned PR number from the cross-fork tracking range.
   d. After successful cherry-pick and smoke-pass, add a `cherry_picked` ledger event
      using `tools/ledger-event.py`.
3. Do not attempt to process these through `pr-review.yml` — the pipeline will fail
   to fetch diffs for these entries.
