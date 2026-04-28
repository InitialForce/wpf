# Decision Log — InitialForce WPF Fork

**Format:** Each entry records a non-trivial architectural or operational decision.
Claude appends automatically when it changes `.if-fork/config.yaml`; humans append for governance decisions.
Every entry must include: decision, rationale, alternatives considered, and sign-off.

See `operator-runbook.md §DECISION_LOG.md Format` for the schema.

---

## DEC-001: Track `release/10.0` only — no 9.0 stack

**Date:** 2026-04-27

**Decision:** The fork tracks `upstream/release/10.0` exclusively. No `if/release/9.0` branch will be created or maintained.

**Context/why:** Swing Catalyst targets `net10.0-windows`. Maintaining two release branches doubles rebase work and CI cost with no consumer benefit until SC ships a 9.0 build, which is not on the roadmap. The upstream `release/9.0` branch is in maintenance-only mode with minimal new patches.

**Alternatives considered:**
- Maintain `if/release/9.0` in parallel: rejected — doubles conflict surface and GHA cost, zero current consumer demand.
- Track upstream `main` (preview): rejected — `main` is an unstable target unsuitable for production; previews break API compatibility frequently.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.1

---

## DEC-002: No signing (Authenticode or strong-name) in v1

**Date:** 2026-04-27

**Decision:** Ship unsigned managed assemblies in v1. Neither Authenticode code-signing nor strong-name signing is applied to the NuGet packages.

**Context/why:** `AssemblyLoadContext` on .NET Core does not validate strong-name signatures in any deployment mode. The "no signing" policy is safe for self-contained SC builds with `packageSourceMapping` enforced. Signing requires MicroBuild infrastructure (internal to Microsoft) — replacing it adds multi-day bootstrap work for zero runtime security benefit in the current threat model.

**Alternatives considered:**
- Authenticode via a purchased code-signing certificate: deferred — SC is the sole consumer; the marginal security value does not justify the certificate cost and process overhead in v1.
- Strong-name signing via a generated key: rejected — SN validation is disabled on .NET Core; the key would add operational overhead with no enforcement.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.6, known-limitations.md §Signing, KNOWN_RISKS.md RISK-003

---

## DEC-003: 2× independent Opus review per patch as the merge gate

**Date:** 2026-04-27

**Decision:** Every patch must receive `safe` verdicts from two independent `claude-opus-4-7` reviewer instances before auto-merge is permitted. Disagreement or any `escalate-to-human` verdict opens a blocking `review-disagreement` issue.

**Context/why:** A single LLM reviewer can be fooled by prompt injection, correlated hallucination, or a sophisticated code-comment injection. Two reviewers running in parallel with different checklist emphases (review-1: security patterns; review-2: behavioral correctness and supply-chain integrity) reduce correlated failure probability. The 2× design catches transient hallucination and prompt injection that fools one but not the other.

**Alternatives considered:**
- Single LLM reviewer + Roslyn analyzer (deterministic third gate): preferred for v1.1; Roslyn analyzer is deferred to v1.1 per R4-7.
- Three-way LLM review: too expensive at Opus 4.7 pricing during bootstrap ($230 one-time); marginal benefit over 2× is low for the realistic threat model (accidental regression, not sophisticated backdoor).
- Human review only: incompatible with the autonomous pipeline goal; the 228-patch bootstrap backlog requires automation.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.9, KNOWN_RISKS.md RISK-001, round-4-critique/01-2x-review-skeptic.md

---

## DEC-004: Repository name `InitialForce/wpf`

**Date:** 2026-04-27

**Decision:** The GitHub repository is named `InitialForce/wpf` (not `InitialForce/dotnet-wpf`, `InitialForce/wpf-fork`, or similar).

**Context/why:** Short, unambiguous, matches the NuGet package namespace prefix `InitialForce.WPF`. Consumers referencing the repo in NOTICE.md, CODEOWNERS, and SC's dependency docs should have a stable, minimal name.

**Alternatives considered:**
- `InitialForce/dotnet-wpf`: longer, redundant with the org name prefix.
- `InitialForce/wpf-fork`: implies unofficial status; the fork is a first-class internal dependency.

**Owner:** Øystein Krog

**Related:** PLAN.md §3 decision 4

---

## DEC-005: Managed-only NuGet packages in v1 (no native patching)

**Date:** 2026-04-27

**Decision:** `InitialForce.WPF` ships patched versions of the four managed assemblies only: `PresentationCore`, `PresentationFramework`, `WindowsBase`, `System.Xaml`. Native binaries (`PresentationNative_cor3.dll`, `wpfgfx_cor3.dll`) flow from Microsoft's runtime pack.

**Context/why:** The native renderer source (`wpfgfx_cor3.dll`) is open source (PR #2553), but patching it requires native toolchain setup (C++, Arcade SDK, DncEng package access) that adds significant bootstrap complexity. The current patch set (h3xds1nz + cross-fork) targets managed code exclusively. `InitialForce.WpfGfx.Native` is deferred to v2 if a specific native bug warrants it.

**Alternatives considered:**
- Ship patched native renderer: deferred — adds bootstrap risk (DncEng access unconfirmed), complex toolchain, no current consumer need.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.3, known-limitations.md §Native Renderer

---

## DEC-006: All h3xds1nz PRs are candidates subject to 2× Opus review

**Date:** 2026-04-27

**Decision:** All 98 open and 116 merged-not-backported h3xds1nz PRs are treated as ingestion candidates. The allowlist is a fast-path prior, not automatic approval. Every patch goes through 2× Opus review.

**Context/why:** h3xds1nz is a prolific WPF contributor (207 commits ahead of upstream `release/9.0`). The quality of their patches is generally high. However, an allowlisted account compromise (see KNOWN_RISKS.md RISK-006) could smuggle a malicious patch without the 2× review gate. The allowlist only affects discovery routing, not the merge gate.

**Alternatives considered:**
- Auto-approve all h3xds1nz patches without review: rejected — account compromise risk; 2× review is cheap at Haiku/Sonnet discovery tier.
- Manual human cherry-pick for all 228 candidates: would take 2–3 weeks of full-time work; automation is the point.

**Owner:** Øystein Krog

**Related:** PLAN.md §3 decision 6, §6, KNOWN_RISKS.md RISK-006

---

## DEC-007: Hard-fail on public API changes in our patches

**Date:** 2026-04-27

**Decision:** Any patch that adds, removes, or modifies public or protected API members on non-internal types is automatically rejected by the ingestion pipeline (`PUBLIC_API_CHANGE` hard-fail predicate). Such PRs route to a `review-disagreement` issue requiring explicit human approval.

**Context/why:** SC consumes a specific compiled version of WPF. A public API change in our fork that is not in the upstream ref pack creates a mismatch between the runtime assembly and the design-time ref pack, potentially breaking SC's build or the XAML designer. Public API changes also require SC to update its source code, which couples SC release timing to fork release timing.

**Alternatives considered:**
- Allow additive-only public API changes (new members, no removals): too complex to validate automatically; deferred to v2 with explicit tooling.

**Owner:** Øystein Krog

**Related:** PLAN.md §3 decision 7, KNOWN_RISKS.md RISK-009

---

## DEC-008: Two NuGet packages, not six

**Date:** 2026-04-27

**Decision:** Ship exactly two NuGet packages: `InitialForce.WPF` (all four managed DLLs) and `InitialForce.WPF.RuntimeOverride` (same DLLs + `RuntimePackAsset` swap target). No per-assembly packages.

**Context/why:** The four managed assemblies form an inseparable dependency group: `DependencyObject` (WindowsBase) → `Visual` (PresentationCore) → `FrameworkElement` (PresentationFramework). Partial adoption throws `TypeLoadException`. Per-assembly packages would be bookkeeping overhead with no real consumer use case. The architecture critic (round-3 #2) confirmed this pivot.

**Alternatives considered:**
- Six packages (one per assembly + two umbrella): rejected — no consumer would ever take a subset; maintenance overhead with no benefit.
- Single umbrella package: considered; two packages retained because the `RuntimeOverride` variant serves a specific edge case (consumers who cannot add per-DLL `<Reference>`).

**Owner:** Øystein Krog

**Related:** PLAN.md §3.4

---

## DEC-009: `packageSourceMapping` mandatory in SC's NuGet.config

**Date:** 2026-04-27

**Decision:** SC must configure `packageSourceMapping` in its `NuGet.config` mapping `InitialForce.*` exclusively to the `nuget.pkg.github.com/initialforce` feed. This is a required configuration, not optional.

**Context/why:** Without source mapping, a dependency-confusion attacker who publishes `InitialForce.WPF` on public nuget.org at a higher version number would have their package resolved by SC's CI. An unsigned package from a private feed is indistinguishable from an unsigned package from a public feed at the file level. `packageSourceMapping` is the primary mitigation (security critic finding #1).

**Alternatives considered:**
- Rely on feed priority ordering in NuGet.config: insufficient — NuGet's resolution order is version-based, not feed-priority-based for version-satisfying packages.
- Authenticode signing: deferred to v1.1 as a defense-in-depth measure; `packageSourceMapping` closes the gap without signing complexity.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.5, KNOWN_RISKS.md RISK-004

---

## DEC-010: Kill switch (`IF_AUTONOMY_ENABLED`) and manual freeze (`IF_AUTOMERGE_FROZEN`) are first-class repo variables

**Date:** 2026-04-27

**Decision:** Two GitHub Actions repository variables gate all autonomous behavior. `IF_AUTONOMY_ENABLED=false` stops all Claude-invoking workflows immediately. `IF_AUTOMERGE_FROZEN=true` halts auto-merges while keeping discovery and review running. Both are operator-controlled; `IF_AUTOMERGE_FROZEN` has no automatic trigger in v1.

**Context/why:** The operations skeptic critique (round-3 #3) identified that without an explicit pause mechanism, a detected regression has no safe off-ramp — the pipeline would continue merging while the operator investigates. The kill-switch design ensures a single command (`gh variable set IF_AUTONOMY_ENABLED -b false`) can stop all autonomous activity within one workflow-check cycle.

**Alternatives considered:**
- Commit-file-based kill switch (`.if-fork/config.yaml` `autonomy_enabled: false`): requires a commit + CI cycle to take effect; too slow for an emergency stop. Repo variables take effect on the next workflow run.
- Workflow-level disable in GitHub UI: does not produce a ledger event; hard to audit.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.11, operator-runbook.md §Kill-Switch Operations

---

## DEC-011: No SC canary in v1 — manual SC validation at each release cut

**Date:** 2026-04-27

**Decision:** SC's existing test suite is not wired as an automated canary gate for the WPF fork. SC validation is manual: Oystein rebuilds SC against each new fork version before a release is considered stable.

**Context/why:** SC's `Test.Unit` and `Test.Integration` are application-level business logic tests that do not exercise WPF's internal allocation behavior, rendering codepaths, or image loading pipeline (pessimist critique finding #5). The marginal signal is poor for the cross-repo coordination cost. SC is the only consumer and rollback is a one-line `.csproj` edit. "Find regressions when Oystein rebuilds SC" is the accepted post-publish safety net in v1.

**Alternatives considered:**
- Automated SC canary (block release if SC CI fails): deferred — requires cross-repo GitHub App access, adds coordination cost, provides poor signal for the patch types we ingest.
- Promote SC `Test.UI` (FlaUI) to blocking: does not cover WPF internal allocation or rendering regressions; same gap.

**Owner:** Øystein Krog (per 2026-04-27 directive)

**Related:** PLAN.md §3.8 decision, PLAN.md §3 decision 11

---

## DEC-012: Single key holder for catastrophic ops is a known risk, accepted in v1

**Date:** 2026-04-27

**Decision:** Catastrophic operations (NuGet unlist, branch force-push, key rotation) formally require two human approvers, but the second named approver is not yet designated. Oystein is the only current key holder. This is accepted as a known single-point-of-failure for the duration of v1 (single-engineer platform team).

**Context/why:** Designating a second approver requires another engineer who has been briefed on the fork's security model and operational runbooks. That person does not exist at time of writing. The risk is documented and accepted with a revisit trigger: when a second engineer joins Initial Force's platform team.

**Alternatives considered:**
- Expand catastrophic ops access to a non-platform engineer: rejected — the second approver must understand the system's security model; a nominally-named approver who cannot evaluate the action provides no real safety.

**Owner:** Øystein Krog

**Related:** PLAN.md §3 decision 12, KNOWN_RISKS.md RISK-012
