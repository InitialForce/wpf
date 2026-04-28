# Known Limitations — InitialForce WPF Fork v1

**Date:** 2026-04-27
**Scope:** v1 (Phase 1–3). Items marked "v2" are deferred, not forgotten.

This document is the authoritative list of things we do not support, do not test, or have explicitly chosen not to fix. It exists so consumers (SC) and operators do not discover these at runtime.

---

## Target Frameworks and Platforms

### net10.0-windows only

The fork targets `net10.0-windows` exclusively. No `net48`, no `netstandard2.0`, no `net9.0-windows`. Swing Catalyst targets `net10.0-windows`; maintaining additional TFMs doubles build time and conflict surface for no current consumer benefit.

- No backport to `net9.0-windows` or `release/9.0` unless SC ships a 9.0 build (not on roadmap).
- The `net10.0-windows` TFM includes the `-windows` OS qualifier required for WPF APIs.

### x64 and arm64 only

Build matrix: `x64` and `arm64` (Windows). No `x86`. Upstream CI still builds x86; we do not. SC does not target x86. If x86 support is needed, enable it in `build.yml` and verify the packaging targets — the `RuntimePackAsset` swap may need platform-conditional logic.

---

## Native Renderer

### `PresentationNative_cor3.dll` consumed unchanged from Microsoft runtime pack

We ship **managed assemblies only**. The native renderer (`PresentationNative_cor3.dll`) is consumed from Microsoft's runtime pack on the consumer machine, not from our NuGet packages.

Implications:
- We cannot patch native rendering bugs in v1.
- A native bug in Microsoft's runtime pack will affect SC regardless of our fork version.
- Native renderer drift between WPF versions is not covered by our smoke harness (pixel-diff tests run against the Microsoft native renderer, not a patched one).
- `InitialForce.WpfGfx.Native` (patching the open-source `wpfgfx_cor3.dll`) is deferred to v2.

### `bilinearspan.lib` (DncEng package) access not verified on clean runners

The upstream WPF build requires `Microsoft.DotNet.Wpf.DncEng` from an Azure DevOps artifact feed. Whether this package is accessible from a clean `windows-latest` GitHub Actions runner without Microsoft credentials has not been confirmed at the time of this writing. Our build may require vendoring this binary directly if the feed is not publicly accessible. See PLAN.md §12 open question 1.

---

## Signing

### No Authenticode in v1

We do not Authenticode-sign the NuGet assemblies in v1. Justification: `AssemblyLoadContext` on .NET Core does not validate strong-name signatures in any deployment mode. The "no signing" policy is safe as long as all of the following hold:

- SC publishes self-contained (`<SelfContained>true</SelfContained>`)
- The only consumer of our packages is SC (single internal consumer)
- `packageSourceMapping` is in place in SC's `NuGet.config` (closes dependency-confusion)

**Signing trigger — turn on Authenticode and strong-name when any of these become true:**
1. SC switches from self-contained to framework-dependent publish.
2. A second internal project beyond SC consumes our packages.
3. A CVE is reported where the attack vector requires a binary without signature validation.
4. InitialForce grows to > 5 engineers.

### No strong-name signing in v1

Same rationale as Authenticode. Strong-name validation is disabled by default on .NET Core. If a consumer ever requires strong-named assemblies (e.g., a plugin host that validates strong names), signing must be enabled before that integration.

---

## NuGet Feed and Distribution

### GitHub Packages (private) — consumers must configure `packageSourceMapping`

Our NuGet packages are published to `nuget.pkg.github.com/initialforce` (private GitHub Packages feed). Consumers must:

1. Add the feed to their `NuGet.config`.
2. Add `packageSourceMapping` so that `InitialForce.*` packages are only resolved from the InitialForce feed, never from public nuget.org:
   ```xml
   <packageSourceMapping>
     <packageSource key="initialforce-github">
       <package pattern="InitialForce.*" />
     </packageSource>
     <packageSource key="nuget.org">
       <package pattern="*" />
     </packageSource>
   </packageSourceMapping>
   ```
   Without this mapping, a dependency-confusion attack is possible (malicious actor publishes `InitialForce.WPF` to nuget.org at a higher version number).

3. Provide a GitHub PAT with `read:packages` scope (or use GitHub Actions GITHUB_TOKEN) for CI restore.

### GitHub App token cannot sign NuGet packages

The `initial-force-wpf-bot` GitHub App token does not have code-signing capability. NuGet package signing (if ever enabled) requires a separate X.509 code-signing certificate held by a human. The bot can push packages to GitHub Packages but cannot attach a NuGet signature to them.

### PAT rotation cadence

The `NUGET_FEED_PAT` used by SC's CI to restore our packages expires on the PAT's configured expiry. Personal PATs expire at 90 days max; fine-grained PATs at up to 1 year. When the PAT expires, SC CI breaks silently at restore time. Rotation is a manual human operation. See operator-runbook.md monthly checklist.

---

## Runtime Pack Asset Override

### msquic-pattern override depends on MSBuild target ordering

The `RuntimePackAsset` override target (the mechanism that substitutes our managed DLLs for Microsoft's at publish time) depends on the `AfterTargets="Build;CopyFilesToOutputDirectory"` hook firing correctly under net10.0. The exact hook sequencing changed between .NET 8 and .NET 10 as part of the `PublishRelatedFilesToPublishDirectory` refactor.

Known gap: if a consumer project uses a custom SDK or overrides `ResolveRuntimePackAssets` themselves, the target ordering may break silently — Microsoft's DLL wins, ours is ignored, and the fork's fixes are not present in the output. The consumer would not see an error; they would simply be running unpatched WPF.

Verification step: each release should include a spot-check that the published output contains `InitialForce.WPF` DLLs (verified by file version or informational version suffix `if.YYYYMMDD`), not the stock Microsoft DLLs.

### XAML designer (Visual Studio) resolves from ref pack, not bin

The XAML designer in Visual Studio resolves WPF types at design time from the `.NET Windows Desktop App` reference pack, not from `bin/`. If our patched DLL's IL differs from the ref pack metadata in a way that affects public types, the designer may show spurious IntelliSense errors or fail to render XAML previews at design time even though the runtime output is correct.

This is not expected to affect SC in practice (our patches target internal implementation, not public API) but is a known limitation for any consumer who relies on XAML designer accuracy.

---

## Testing

### Smoke harness only — no Swing Catalyst integration test

The fork ships with a 22-scenario `test/InitialForce.WpfSmoke/` harness (NUnit + BenchmarkDotNet + pixel-diff goldens at 0.1% tolerance, WARP renderer). SC validation is manual at each release cut — there is no automated SC canary in v1.

Coverage gaps (known):
- Printer and print preview codepaths (IME input, accessibility, print dialogs)
- IME (Input Method Editor) for CJK text input
- WPF 3D (`Viewport3D`, etc.) — not in SC's use case
- WPF WebBrowser control
- Clipboard codepaths beyond basic text/image
- Concurrent WPF rendering scenarios (thread-safety bugs may not surface in deterministic 22-scenario harness)
- Regression in behaviors only observable after 10,000+ event registrations (long-running production scenarios)

Any smoke test gap should be documented here when discovered during incident postmortem.

### Roslyn analyzer (deterministic third gate) is v1.1, not v1

The `RoslynForkPolicy` analyzer (`tools/analyzers/`) that provides a deterministic third gate (catching `Assembly.Load*`, `BinaryFormatter`, `Process.Start` without LLM involvement) is deferred to v1.1. In v1, the 2× Opus review gate is the only automated policy check for these patterns. See KNOWN_RISKS.md RISK-010.

### Perf baseline drift

The perf gate (BenchmarkDotNet, `perf/series.jsonl`) uses a 5% regression threshold. The baseline is locked at the first clean build of `if/release/10.0` with zero patches. If the runner hardware changes (GitHub upgrades `windows-latest` agent specs), the baseline becomes invalid and may produce spurious regression alerts or miss real regressions. Baseline must be re-locked when the runner environment changes.

---

## Governance

### 24-hour auto-merge timer is wall-clock

The 24-hour auto-merge window for approved `claude/*` PRs is wall-clock time, not business hours. A patch approved at 11 PM on a Friday will auto-merge by 11 PM on Saturday. There is no business-hours filter. Operators who want to ensure human awareness before a weekend merge should set `IF_AUTOMERGE_FROZEN=true` before the weekend and clear it on Monday.

### Single key holder for catastrophic ops

Catastrophic operations (NuGet unlist, branch force-push, key rotation) require two human approvers per the reversibility hierarchy. In v1, the designated second approver has not been formally named. This is a known single-point-of-failure — see KNOWN_RISKS.md RISK-012. Revisit when a second engineer joins the platform team.
