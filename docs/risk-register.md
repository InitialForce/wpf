# Risk Register — InitialForce WPF Fork

**Date:** 2026-04-27
**Owner:** Øystein Krog
**Purpose:** On-call cheat sheet mapping each known risk to its detection signal, response runbook, and current residual severity. Full risk detail is in KNOWN_RISKS.md.

---

## Register

| ID | Risk summary | Residual severity | Detection signal | Response runbook | Last reviewed |
|---|---|---|---|---|---|
| R1 | Correlated LLM reviewer failure (2× review) | high | `review-disagreement` issue opened; disagreement rate < 2% in ledger over 30+ PRs | operator-runbook.md §Escalate a review-disagreement issue; tune prompts per exec-docs/20-claude-prompts.md | 2026-04-27 |
| R2 | Prompt injection via upstream PR content | med | `prompt-injection-suspected` label on a reviewed PR; unexpected verdicts on benign-looking patches | operator-runbook.md §Escalate a review-disagreement issue; inspect both reviewer rationales for injected framing | 2026-04-27 |
| R3 | No Authenticode signing — dependency confusion | low (mitigated) | SC restore begins pulling `InitialForce.*` from nuget.org (verify via `dotnet restore -v diag`); new package appears on nuget.org for `InitialForce.WPF` | Enable `packageSourceMapping` in SC NuGet.config immediately; reserve namespace on nuget.org; operator-runbook.md I-1 | 2026-04-27 |
| R4 | GitHub App `contents:write` — compromised runner | high | Unexpected commits on `claude/*` branches not matching ledger events; bot account activity outside normal workflow triggers; GitHub org audit log alert | operator-runbook.md I-8 (key compromise); pause autonomy; audit `audit/` branch; rotate all secrets | 2026-04-27 |
| R5 | Ledger tampering | high | `tools/ledger-validate.py` CI failure ("non-trailing line modified"); `patch-state.json` diff against regenerated state; Rekor entry mismatch | operator-runbook.md I-10; rotate all secrets if external actor suspected | 2026-04-27 |
| R6 | Allowlisted account compromise | high | Patch from allowlisted author with unusual file patterns, new `[DllImport]`, or post a PR after 30+ day silence; 2× review escalation | operator-runbook.md I-9; audit all recent patches from the author; revert if suspicious | 2026-04-27 |
| R7 | Cherry-pick SHA smuggling | low (mitigated) | `pr-ingestion.yml` fails with "PR HEAD changed since triage" assertion error; escalation issue opens automatically | Re-triage the PR; verify new HEAD is clean before re-approving | 2026-04-27 |
| R8 | Native renderer drift / native CVE | med | Monthly CVE scan finds advisory for `dotnet/wpf` native component; SC reports rendering artifact not reproducible in our smoke harness | Monthly checklist CVE scan; coordinate SC SDK update; operator-runbook.md §CVE scan | 2026-04-27 |
| R9 | RuntimePackAsset swap silently fails | med | SC build output DLL has Microsoft informational version (no `if.YYYYMMDD` suffix); `dotnet publish /bl` MSBuild log shows no IF target firing | Re-run Phase 1 P1-3 verification; check `AfterTargets` hook ordering in packaging project; known-limitations.md §RuntimePackAsset | 2026-04-27 |
| R10 | Roslyn analyzer deferred (v1.1 gap) | med | LLM reviewer passes a patch containing `Assembly.Load*`, `BinaryFormatter`, or `Process.Start` in non-test code | Manual code review of flagged patterns; operator adds `Process.Start`/`Assembly.Load*` patterns to hard-fail list if not already present; plan v1.1 Roslyn work | 2026-04-27 |
| R11 | Rebase stack growth / conflict rate | med | Weekly rebase PR has `dropped-hunk` or `escalation` warning; conflict count exceeds 30% threshold; operator spends > 60 min on rebase weekly | Freeze new ingestion; graduate patches; quarterly human review of patches > 6 months old; operator-runbook.md weekly checklist | 2026-04-27 |
| R12 | Single key holder for catastrophic ops | med | Oystein unavailable during a production NuGet incident; no named second approver | Identify a temporary second approver before any absence > 5 days; operator-runbook.md return-from-vacation checklist | 2026-04-27 |
| R13 | GitHub Packages outage | low | SC CI fails at `dotnet restore` with "Unable to resolve 'InitialForce.WPF'"; `githubstatus.com` shows GitHub Packages incident | Wait (typical < 1h); if > 4h, pin SC to cached version; operator-runbook.md I-6 | 2026-04-27 |
| R14 | Audit log tampering | low (mitigated) | `audit/` branch force-push attempt blocked by branch protection; unexpected commits on audit branch by non-bot actor; Rekor entry missing for a known run | Verify branch protection settings; check Rekor log; operator-runbook.md I-10 | 2026-04-27 |
| R15 | DncEng package inaccessible on clean runners | high (open) | Phase 1 P1-2 build fails at `dotnet restore` with authentication or package-not-found error for `Microsoft.DotNet.Wpf.DncEng` | Vendor `PresentationNative_cor3.dll` directly in fork; bypass DncEng dependency for managed-only build; known-limitations.md §DncEng | 2026-04-27 |

---

## Residual Severity Definitions

- **critical** — failure likely causes data loss, security breach, or unrecoverable production incident.
- **high** — failure causes significant operational disruption; requires urgent operator response within hours.
- **med** — failure causes degraded service or increased manual workload; can be addressed within days.
- **low** — failure causes minor inconvenience or is already well-mitigated; addressed in next scheduled review.

---

## Review Schedule

| Cadence | Scope |
|---|---|
| Monthly | Re-read all `high` and `critical` rows; verify detection signals are still valid; update `Last reviewed` date. |
| Quarterly | Full register review; add new risks surfaced by incidents or architecture changes; retire closed risks. |
| Post-incident | Add or update the row for any risk that materialized; capture new detection signals or runbook gaps. |

---

## How to Add a New Risk

1. Add an entry to KNOWN_RISKS.md following the `## RISK-NNN` format.
2. Add a row to this register referencing the KNOWN_RISKS.md ID.
3. Add an entry to DECISION_LOG.md if the risk motivated a governance decision.
4. Reference the risk register row in any related incident postmortem.
