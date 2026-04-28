# NOTICE

`InitialForce/wpf` is a fork of `dotnet/wpf`
(https://github.com/dotnet/wpf), which is copyright (c) .NET Foundation
and Contributors and licensed under the MIT License. Initial Force AS is
not affiliated with Microsoft Corporation. The trademarks "WPF" and
"Windows Presentation Foundation" remain the property of Microsoft
Corporation.

Last reviewed: 2026-04-27

---

## Upstream

This fork tracks the `release/10.0` branch of `dotnet/wpf`
(https://github.com/dotnet/wpf/tree/release/10.0). The repository is
rebased onto upstream release tags (e.g. `v10.0.X`) and carries a linear
patch stack on top.

The upstream MIT License and third-party attributions apply unmodified.
See `LICENSE.TXT` and `THIRD-PARTY-NOTICES.TXT` (both originally from
`dotnet/wpf`) in this repository for the full copyright notices, which
are preserved verbatim.

---

## Primary patch source — h3xds1nz

The majority of carry-patches in this fork originated as pull requests to
`dotnet/wpf` authored by community contributor **h3xds1nz**
(https://github.com/h3xds1nz). These include performance improvements,
allocation reductions, and correctness fixes across `PresentationCore`,
`PresentationFramework`, `WindowsBase`, and `System.Xaml`. At the time of
fork bootstrap (2026-04-27), approximately 214 patches from h3xds1nz were
candidates for ingestion (98 open PRs + 116 merged-to-upstream-main PRs
not yet backported to `release/10.0`).

All carry-patches from h3xds1nz are MIT-licensed (as derivatives of
`dotnet/wpf`). Each applied commit carries a `Cherry-picked-from:` trailer
linking the upstream PR and a `Co-authored-by:` trailer crediting the
original author.

---

## Cross-fork patches

The following patches originate from forks of `dotnet/wpf` other than the
upstream repository. All are MIT-licensed as derivatives of `dotnet/wpf`.

### Faithlife/wpf

https://github.com/Faithlife/wpf (archived July 2024)

- "Eliminate allocation in StreamAsIStream.Read"
- "Close Stream when creating ImageSource from Uri"
- "Reduce allocations when tracing routed events"

### dotnet-campus/wpf

https://github.com/dotnet-campus/dotnet-campus.WPF

- "WeakEventTable thread-safety lock"

---

## Other community contributors

Additional Tier-S and Tier-A patches were sourced from `dotnet/wpf` pull
requests authored by various community contributors, including but not
limited to: `hexawyz`, `IAmTheCShark`, `lindexi`, `smolchanovsky`,
`etvorun`, `ThomasGoulet73`, and `akshatsinha0`. All such patches are
MIT-licensed. Each applied commit carries `Cherry-picked-from:` and
`Co-authored-by:` trailers with the original author and PR link.

---

## Modifications by Initial Force AS

Initial Force AS (https://github.com/InitialForce) maintains this fork.
The modifications made by Initial Force AS are:

1. **Cherry-pick aggregation.** Automated tooling (Claude Code GitHub
   Actions workflows) discovers, reviews (2x independent Opus review gate),
   and applies upstream and cross-fork patches to the `if/release/10.0`
   branch. Every applied patch is recorded in the patch ledger.

2. **msquic-pattern packaging.** Two NuGet packages are produced from
   the patched managed assemblies, using the same `RuntimePackAsset` swap
   pattern as `InitialForce.App.csproj`:
   - `InitialForce.WPF` — patched managed DLLs
     (`PresentationCore`, `PresentationFramework`, `WindowsBase`,
     `System.Xaml`) and PDBs; for standard consumers.
   - `InitialForce.WPF.RuntimeOverride` — same DLLs with an additional
     targets file for consumers that cannot use the standard
     `RuntimePackAsset` swap.

3. **Patch ledger.** `.if-fork/patch-ledger.jsonl` is an append-only
   audit log of every patch ever considered by this fork (discovered,
   reviewed, applied, graduated, or rejected). It is the authoritative
   per-patch attribution record. For the full list of applied patches
   with their upstream PR links and author credits, see:
   `.if-fork/patch-ledger.jsonl`

All modifications by Initial Force AS are released under the MIT License.

---

## Native renderer note

`PresentationNative.dll` (also referenced as `PresentationNative_cor3.dll`
in the upstream build infrastructure) is a **closed-source** component
owned by Microsoft Corporation. This fork does **not** modify
`PresentationNative.dll`. It is consumed unchanged from Microsoft's
`Microsoft.WindowsDesktop.App.Runtime.win-x64` runtime pack and is
delivered to end-users by the .NET runtime framework installation.
Microsoft's license terms for that runtime pack apply to
`PresentationNative.dll` without modification by this fork.

---

## License

All managed source code and patches in this repository are MIT-licensed.
`dotnet/wpf` is MIT-licensed; all carry-patches are derivatives of that
codebase or independently MIT-licensed from their respective source forks.
The full MIT License text is in `LICENSE.TXT`.

Copyright holders include: .NET Foundation and Contributors; Initial Force
AS (modifications and packaging); h3xds1nz (community patches); Faithlife
Corporation (cross-fork patches, archived 2024); dotnet-campus contributors
(cross-fork patches); various community contributors via dotnet/wpf pull
requests.
