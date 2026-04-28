# Known Risks — InitialForce WPF Fork

**Date:** 2026-04-27
**Owner:** Øystein Krog
**Sources:** PLAN.md §11, round-3-critique (pessimist, security, operations), round-4-critique (2× review skeptic)

Each entry: severity (critical/high/med/low), mitigation status, owner, related runbook/register ID.

---

## RISK-001: Correlated LLM reviewer failure modes (2× review)

**Severity:** high
**Status:** monitoring

Both review-1 and review-2 use the same base model (`claude-opus-4-7`), read the same diff, and apply the same hard-fail pattern list. Cognitive science research on expert review documents that same-framework same-stimulus reviewers have correlated error modes. A prompt injection vector that fools one instance may fool both.

**Mitigation:**
- review-2 runs at temperature 0.7 vs review-1 at 0.0 — different probability mass sampled.
- review-2 receives the diff plus surrounding source file context; review-1 receives the diff only. Forces different reasoning anchors.
- Hard-fail patterns are applied to raw diff bytes before LLM reasoning begins (deterministic pre-filter).
- `Process.Start`, `AppContext.TryGetSwitch`, `Assembly.Load*`, `BinaryFormatter` added to hard-fail list per round-4 finding #2.
- Roslyn analyzer (deterministic third gate) deferred to v1.1 — will catch patterns LLM reviewers evaluate probabilistically.

**Owner:** Øystein Krog

**Related:** DECISION_LOG.md DEC-003, round-4-critique/01-2x-review-skeptic.md finding #1, risk-register.md R1

---

## RISK-002: Prompt injection via upstream PR content

**Severity:** high
**Status:** mitigated

An attacker who can open a PR on `dotnet/wpf` can craft a PR body or code comment that attempts to override reviewer instructions (imperative injection: `IGNORE PRIOR INSTRUCTIONS`), assert false social-proof facts (plausible-looking security team annotations with embedded confidence scores), or embed base64-encoded payloads.

**Mitigation:**
- All PR body and diff content is wrapped in `<untrusted_input>...</untrusted_input>` delimiters in every reviewer prompt.
- Hard-fail patterns include: `IGNORE`, `SYSTEM:`, `[INST]`, new `[DllImport]`, `unsafe`, `extern`, `Marshal.`, `SecurityCritical` removal, `LinkDemand` removal, `Assert(...)` removal, P/Invoke signature changes, base64-encoded strings, suspicious URLs, `Process.Start`, `AppContext.TryGetSwitch`, `Assembly.Load*`, `BinaryFormatter`.
- Any hard-fail pattern match → automatic `unsafe` verdict + `prompt-injection-suspected` label, bypassing LLM confidence scoring.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.9, round-3-critique/04-security.md finding #3

---

## RISK-003: No Authenticode signing — dependency confusion attack surface

**Severity:** high
**Status:** mitigated (primary mitigation in place; signing deferred to trigger conditions)

An attacker can register `InitialForce.WPF` on public nuget.org. An unsigned package from the private GitHub Packages feed is indistinguishable at the file level from an unsigned malicious package from nuget.org. Without `packageSourceMapping`, NuGet restore may resolve the malicious package.

**Mitigation:**
- `packageSourceMapping` in SC's `NuGet.config` maps `InitialForce.*` exclusively to the GitHub Packages feed. This is the primary mitigation (DECISION_LOG.md DEC-009).
- `InitialForce.*` namespace reserved on public nuget.org (placeholder packages, immediately unlisted after publish).
- Authenticode signing enabled when signing triggers are met (see known-limitations.md §Signing).

**Owner:** Øystein Krog

**Related:** DECISION_LOG.md DEC-002, DEC-009, known-limitations.md §Signing, round-3-critique/04-security.md finding #1

---

## RISK-004: GitHub App `contents:write` — compromised runner push

**Severity:** high
**Status:** mitigated

The `initial-force-wpf-bot` GitHub App has `contents:write` at the installation level. A compromised GitHub Actions runner that obtains the App's installation token (valid 1 hour) can push to any non-protected branch. `claude/*` branches auto-merge after 24h if CI passes, so a fast-moving attacker could craft a passing PR with a subtle payload.

**Mitigation:**
- Branch protection on `claude/*` prefix: require status checks, signed commits, no force-push from non-bot accounts.
- `GH_APP_PRIVATE_KEY` stored in the `bot-credentials` Environment secret, not a repo secret — prevents PR workflows from accessing it.
- 90-day rotation cadence for the App private key (monthly per operator runbook, stricter than the 90-day minimum).
- CODEOWNERS: `.github/workflows/ @InitialForce/platform-team` — the bot's token cannot edit its own CI.
- App installation scoped to `InitialForce/wpf` only — confirmed at App settings level, not just documented.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.7, round-3-critique/04-security.md finding #2

---

## RISK-005: Ledger tampering

**Severity:** high
**Status:** mitigated

The `patch-ledger.jsonl` is the source of truth for which patches have been applied, reviewed, and published. The bot has `contents:write` and could theoretically overwrite ledger history. An external actor with repo write access could also tamper.

**Mitigation:**
- Ledger is append-only by design; `tools/ledger-validate.py` in CI fails if any non-trailing line is modified or removed.
- `regenerate-state.py` re-derives `patch-state.json` from the JSONL on every PR; a diff mismatch fails CI.
- Audit branch (`audit/`) has branch protection (no force-push, no deletion, signed commits required) — provides an independent append-only record.
- Cosign keyless signing of audit JSON artifacts, pushed to Sigstore Rekor (external append-only transparency log).
- Detection via daily CI validation; operator spot-checks monthly per operator-runbook.md.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.10, operator-runbook.md I-10, round-3-critique/04-security.md finding #7

---

## RISK-006: Allowlisted contributor account compromise

**Severity:** high
**Status:** monitoring

`h3xds1nz` and other allowlisted contributors are known, targetable GitHub identities. An attacker who compromises an allowlisted account can open a PR that passes all Tier-S predicates and gets fast-path routed to 2× Opus review — the only automated gate. A sufficiently legitimate-looking 3-file perf patch could receive `safe` verdicts and auto-merge within 24 hours.

**Mitigation:**
- 2× independent Opus review is the primary gate; both reviewers must agree on `safe`.
- Velocity check: if an allowlisted author has zero PRs in the last 30 days, the first new PR lowers automatic confidence by 0.15 — enough to escalate to human review.
- `patch-ledger.jsonl` records author per event; incident response can quickly audit all patches from a compromised author (operator-runbook.md I-9).
- Allowlist changes require a human-reviewed PR to `.if-fork/config.yaml` with CODEOWNERS approval.

**Owner:** Øystein Krog

**Related:** PLAN.md §3.9, round-3-critique/04-security.md finding #4, operator-runbook.md I-9

---

## RISK-007: Cherry-pick SHA smuggling (force-push between triage and execution)

**Severity:** high
**Status:** mitigated

Between triage (when a PR is evaluated) and ingestion (when it is cherry-picked), a contributor can force-push a new commit to their fork branch. GitHub returns the current HEAD on `gh pr view`, not the SHA at triage time. If ingestion fetches the new HEAD, it applies a commit that was never reviewed.

**Mitigation:**
- `head_sha` is captured in the `discovered` event of `patch-ledger.jsonl` at triage time.
- `pr-ingestion.yml` asserts that `gh pr view --json headRefOid` matches the ledger's `head_sha` before proceeding. Mismatch → abort + escalation issue `"PR HEAD changed since triage — re-triage required"`.

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

## RISK-010: Roslyn analyzer (deterministic third gate) deferred to v1.1

**Severity:** med
**Status:** accepted

`Assembly.Load*`, `BinaryFormatter`, `Process.Start`, and `Process.GetCurrentProcess()` are not in the LLM hard-fail pattern list and are evaluated probabilistically by the LLM reviewers. A carefully crafted patch could contain conditional dead code using these primitives that both reviewers pass with moderate confidence. A deterministic Roslyn analyzer would catch these with zero false negatives.

**Mitigation:**
- `Process.Start`, `AppContext.TryGetSwitch` added to hard-fail list per round-4 finding #2.
- `Assembly.Load*`, `BinaryFormatter` partially mitigated by reviewer step-5 checklists.
- Roslyn analyzer (`tools/analyzers/RoslynForkPolicy/`) is planned for v1.1 with rules IFP001–IFP005.

**Owner:** Øystein Krog

**Related:** known-limitations.md §Roslyn analyzer, round-4-critique/01-2x-review-skeptic.md finding #5

---

## RISK-011: Rebase stack growth — conflict rate grows super-linearly

**Severity:** med
**Status:** monitoring

As the carry patch stack grows (starting at ~34 patches, adding ~2/month), the probability that any upstream commit conflicts with a carried patch increases. At 100 patches, conflicts become near-certain during weekly rebasing. Manual conflict resolution time scales with stack depth.

**Mitigation:**
- Graduation detection (`tools/check-graduated.py`, content-based hunk comparison) automatically drops patches absorbed by upstream.
- Stack-size cap: if carry stack exceeds 75 patches, freeze new ingestion and dedicate a sprint to upstreaming or retiring patches.
- Quarterly human review gate: patches older than 6 months get an explicit keep/retire/upstream decision.
- `rerere.enabled` and `rerere.autoupdate` reduce repeated conflict resolution time.

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

## RISK-013: GitHub Packages outage — SC restore breaks

**Severity:** med
**Status:** accepted

GitHub Packages has had multi-hour outages. SC cannot build if GitHub Packages is down and the NuGet cache is cold.

**Mitigation:**
- SC CI caches the NuGet package directory (`actions/cache`) — a warm cache survives a < 4h outage without breaking SC builds.
- Incident runbook I-6 covers the response.
- Secondary NuGet mirror (Azure Artifacts) is a v2 improvement if outages exceed once per quarter.

**Owner:** Øystein Krog

**Related:** operator-runbook.md I-6, round-3-critique/01-pessimist.md finding #9

---

## RISK-014: Audit log tamper resistance limited by bot write access

**Severity:** med
**Status:** mitigated

The bot has `contents:write` on `InitialForce/wpf` and could technically overwrite or rewrite the `audit/` orphan branch.

**Mitigation:**
- Branch protection on `audit/`: no force-push, no deletion, signed commits required. The bot can only append.
- Cosign keyless bundles pushed to Sigstore Rekor (external append-only log) — these cannot be deleted.
- GitHub Issues fallback: one issue per workflow run summary provides an additional append-only record.

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
