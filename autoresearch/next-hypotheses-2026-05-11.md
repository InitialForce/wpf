# Next-Optimization Hypotheses — post-big-wins baseline (2026-05-11)

**Baseline source**: `autoresearch/profile-output/{startup,take-open}/analysis.json`
+ `autoresearch/profile-output/playback-post-bigwins/analysis.json` (32 Hz session).
**Reference**: `compare-bigwins.py` apples-to-apples — combined alloc 10.42 GB -> 1.17 GB (-88.8%).

## Sufficiency verdict

**Yes — sufficient to design the next round.** What we have:
- Per-scenario `totalAllocBytes`, `topAllocators[1..30]` (bytes, no callstacks).
- WPF render-pass counts and frame P95/P99 ms.
- Apples-to-apples stock-vs-candidate baselines from the same env (compare-bigwins).
- Three real MC scenarios (startup, take-open, playback) so signals can be
  cross-checked: a target that lights up in only one scenario is a different
  kind of fish than one that bleeds in all three.

What we DON'T have — caveats for hypothesis confidence:
- **No callstack attribution per type.** Top-allocator entries report only
  `allocBytes` and an empty `allocCount`. For Hashtable-style aggregate hits
  (DictionaryEntry / DictionaryEntry[]) the WPF subsystem responsible isn't
  named. **Mitigation**: re-run one scenario with `dotnet-trace` AllocationTick
  in callstack mode (or PerfView `/GCCollectOnly`) for the top three suspect
  types. Cost: one extra trace per target. Worth doing before committing
  major engineering on T4/T5.
- **No per-method allocCount.** Can't compute per-call cost or distinguish a
  few large arrays from many small ones from the JSON alone.
- **One sample per scenario.** Confirming any of these hypotheses needs an
  apples-to-apples re-measurement, not just one post-fix capture.

## Candidate top-allocator landscape (post-big-wins)

| Rank | Type | startup | take-open | playback | Combined |
|---|---|---|---|---|---|
| T4 | `System.Collections.DictionaryEntry[]` | — | 178.5 MB | 104.5 MB | **283 MB** |
| T4 | `System.Collections.DictionaryEntry` | — | 140.3 MB | 85.4 MB | **226 MB** |
| (env) | `System.String` | 29.0 MB | 30.9 MB | 15.9 MB | 76 MB |
| T5 | `FourElementAsyncLocalValueMap` | — | 11.4 MB | 5.65 MB | **17.0 MB** |
| (mc) | `ForceSample` / domain types | — | ~33 MB | — | mc-side |
| T6 | `System.Windows.Threading.DispatcherOperation` | — | 7.46 MB | 4.90 MB | **12.4 MB** |
| T7 | `ActivityInfo` | — | 6.93 MB | 4.05 MB | **11.0 MB** |
| T8 | `System.Threading.ExecutionContext` | — | 4.90 MB | 3.84 MB | **8.7 MB** |
| (mc) | EF/Reflection types | 30+ MB | — | — | startup |

DictionaryEntry+DictionaryEntry[] dominates the residual WPF wedge —
**44% of take-open allocs, 77% of playback allocs**. Everything else is at
least an order of magnitude smaller.

---

## T4: `Hashtable` churn (DictionaryEntry + DictionaryEntry[])

**Hypothesis**: A small number of WPF-internal `Hashtable` instances grow,
get enumerated, or are recreated per-frame, producing both the bucket-array
growth (`DictionaryEntry[]`) and per-entry boxing during enumeration
(`DictionaryEntry`). The 1:1.27 ratio of entries-vs-array bytes is consistent
with `foreach (DictionaryEntry e in hashtable)` over a Hashtable that is
itself periodically resized.

**Evidence**:
- Both types track each other across scenarios (178:140 take-open, 104:85 playback)
  with the array consistently ~30% larger than the entry count — typical of an
  enumerated Hashtable kept warm and resized intermittently.
- Drops out completely on `startup` (no rendering loop) → tied to the render /
  property-change path, not to type-system init.
- ~507 MB combined → likely a single hot path, not diffuse.

**Candidate culprits (need callstack to confirm)**:
1. **`ResourceDictionary._baseDictionary`** — Hashtable; enumerated on lookup
   walks for inherited keys. If MC mutates resource dicts during animation
   (theme switch, dynamic resource binding) this would explode.
2. **`DependencyObjectType` / `DependencyProperty` static tables** — Hashtables
   keyed by Type for property metadata; usually one-shot, but DP registration
   from app code on hot paths could leak.
3. **`MS.Internal.WeakHashtable` / `WeakObjectHashtable`** — `PresentationFramework`
   has three of these wrappers. They store DO-keyed event handler lists. If
   a render-frequency object is registered/unregistered repeatedly, the inner
   Hashtable rehashes.
4. **`Dispatcher._reserved0` / `_reservedHooksHashtable`** — diagnostics hook map.
5. **`SystemResources` / theme dictionary** — every `FindResource` walk.

**Verification step (cheap)**:
```
dotnet-trace collect --providers Microsoft-DotNetCore-SampleProfiler,System.GC.Tracing \
  --providers-events GCAllocationTick_V4 --duration 30 -- MotionCatalyst-cli.exe
```
Then re-run `nettrace-probe` with `--alloc-stacks` (add flag if missing) to
attribute the DictionaryEntry types to their construction call site.

**Expected upside**: 250-500 MB combined across take-open+playback. Bigger
than any of T1-A/T2-A/T1-#1/T1-#2 individually.

**Fix shape (presumptive — depends on which Hashtable)**:
- Convert hot Hashtable to `Dictionary<TKey,TValue>` (no DictionaryEntry boxing).
- Or replace `foreach (DictionaryEntry e in ht)` with index-based access via the
  `Keys` collection where the dictionary is read-only.
- For WeakHashtable specifically: cache the enumerator results when consumers
  walk the same collection at frame rate.

---

## T5: `FourElementAsyncLocalValueMap` (17 MB combined)

**Hypothesis**: `AsyncLocal<T>.Value` writes inside the WPF dispatcher /
activity path force `ExecutionContext` rebuilds with each scheduled
DispatcherOperation. `FourElementAsyncLocalValueMap` is the immutable map
backing `ExecutionContext._localValues` when 4 distinct AsyncLocals are set
(it copies the whole map on any change).

**Evidence**:
- Co-allocates with `ExecutionContext` (4.9 MB take-open, 3.84 MB playback)
  and `ActivityInfo` (6.9 / 4.05 MB) — all three are the System.Diagnostics
  Activity / AsyncLocal capture trio.
- Specific to the 4-element map size suggests Activity tracing + ~3 other
  AsyncLocals are live on the dispatcher.

**Fix shape**:
- Skip Activity creation in `DispatcherOperation` (or use a single shared
  ambient Activity for the render loop).
- Coalesce ExecutionContext suppression around the WPF render frame.
- Either save ~12 MB take-open + ~9 MB playback, OR concede this is the cost
  of `ActivitySource` and turn it off when not actively tracing.

**Confidence**: Medium. The mechanism is well-understood; what isn't certain
is whether the Activity is created by WPF itself or by an MC subscriber.

---

## T6: `DispatcherOperation` residual (12.4 MB combined)

**Hypothesis**: After ralph's earlier work on `DispatcherOperation` pooling,
this is residual churn from operations that escape the pool (e.g. those that
hold async continuations, those with non-default priority, or `Task`-backed
ones).

**Evidence**: `Task`1[System.Object]` (2.23 MB playback) and
`DispatcherOperationTaskMapping` (0.85 MB) show up alongside.

**Fix shape**: Profile DispatcherOperation construction sites to see which
overload bypasses pooling. Likely a 50-70% reduction is possible by routing
the `Task`-backed overloads through the same pool.

**Confidence**: Lower priority than T4 — already small wedge.

---

## T7: `ActivityInfo` (11 MB combined)

See T5 — same root cause cluster. Killing the dispatcher Activity also kills
ActivityInfo.

---

## T8: `EffectiveValueEntry[]` is gone — confirmation

`EffectiveValueEntry[]` does NOT appear in the candidate's top-30 allocators
for take-open or playback (was 548 MB / 308 MB in stock). T1-#1 hit-test pool
landed cleanly. No follow-up needed.

---

## Prioritization

1. **T4 (Hashtable)** — biggest single signal, presumptive ~500 MB. Needs
   one callstack-attribution capture before commit-effort decision. **Do this
   first.**
2. **T5 (AsyncLocal/Activity)** — clean structural target, ~17 MB. Worth ~1
   day of investigation regardless of T4 outcome.
3. **T6/T7** — wait until T4+T5 land; re-baseline and re-rank.

## Suggested next actions

1. Add `--alloc-stacks` (or equivalent) to `nettrace-probe`, OR run one
   PerfView trace of the take-open scenario to attribute DictionaryEntry[].
2. Resume ralph with `cooldown.json` updates reflecting which T1/T2/T3
   targets are fully landed (no need to re-explore those wedges).
3. After T4 stack data lands, draft the implementation as a single bead and
   land it before relaunching ralph against the full residual.
