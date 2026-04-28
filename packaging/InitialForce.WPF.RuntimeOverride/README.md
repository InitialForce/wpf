# InitialForce.WPF.RuntimeOverride

Surgical per-assembly WPF override package. Use this when you need to replace
only specific WPF DLLs with InitialForce-patched versions, or when the full
`InitialForce.WPF` package cannot be used.

## When to use `InitialForce.WPF.RuntimeOverride` vs `InitialForce.WPF`

### Use `InitialForce.WPF` (the primary package) when:

- You want all four patched WPF assemblies (`PresentationCore`, `PresentationFramework`,
  `WindowsBase`, `System.Xaml`) replaced in one step.
- Your project is a standard self-contained WPF application targeting `win-x64`.
- You use the same MSBuild pipeline as Swing Catalyst / Motion Catalyst.
- You are adopting the InitialForce WPF fork for the first time.

This is the right choice for **the vast majority of consumers**.

### Use `InitialForce.WPF.RuntimeOverride` when:

- You need to replace **only a subset** of WPF assemblies. For example, a bug fix
  ships only in `PresentationCore` and you do not want to touch the other three.
- Your project type (`MAUI` embedding, `WinUI` interop, unusual SDK setups) resolves
  WPF DLLs through a code path not covered by `InitialForce.WPF`'s targets.
- A future .NET SDK change alters when `ResolveRuntimePackAssets` runs relative to
  `CopyFilesToOutputDirectory`, breaking the primary package's injection sequence.
- You are in a **crisis situation** and need a targeted hot fix with minimal blast
  radius — switch one DLL without disturbing the others.

In v1, almost no consumer needs this package. Start with `InitialForce.WPF`.
Treat `InitialForce.WPF.RuntimeOverride` as the documented fallback for edge cases.

## Usage

```xml
<PropertyGroup>
  <!-- List only the assemblies you want to replace (semicolon-separated). -->
  <!-- Supported values: PresentationCore, PresentationFramework, WindowsBase, System.Xaml -->
  <IF_OverrideAssemblies>PresentationCore;PresentationFramework</IF_OverrideAssemblies>
</PropertyGroup>

<ItemGroup>
  <PackageReference Include="InitialForce.WPF.RuntimeOverride" Version="10.0.*-if.*" />
</ItemGroup>
```

If `IF_OverrideAssemblies` is empty (the default), **no assemblies are replaced**.
This is intentional — the package is a targeted tool, not a bulk replacement.

## Supported assemblies

| Assembly name | Replaces |
|---|---|
| `PresentationCore` | `PresentationCore.dll` |
| `PresentationFramework` | `PresentationFramework.dll` |
| `WindowsBase` | `WindowsBase.dll` |
| `System.Xaml` | `System.Xaml.dll` |

## How it works

For each assembly listed in `IF_OverrideAssemblies`, two MSBuild targets fire:

1. **RemoveRuntimeWpfAsset_`<Name>`** — strips the Microsoft-supplied DLL from
   `RuntimePackAsset`, `ResolvedFileToPublish`, and `ReferenceCopyLocalPaths` so the
   SDK never copies the stock runtime-pack version.

2. **InjectIfWpfAssembly_`<Name>`** — copies the InitialForce-patched DLL to the
   output directory at `Build` and `CopyFilesToOutputDirectory` time, so patched DLLs
   are present on every F5 inner-loop build as well as at publish.

Both targets are conditioned on `$(RuntimeIdentifier)` starting with `win-`. The
`runtimes/win-x64/` payload is the only one shipped; x86 and ARM64 consumers fall
through silently to the Microsoft runtime pack (a v1 limitation — acceptable since
Swing Catalyst is x64-only).

The targets are placed under `buildTransitive/` so they propagate transitively through
project-to-project references, consistent with NuGet's transitive package behavior.

## Framework-dependent vs self-contained

`InitialForce.WPF.RuntimeOverride` is the recommended fallback for
**framework-dependent** publish scenarios where `InitialForce.WPF`'s injection may
not fire. See `docs/known-limitations.md` for details.

## License

MIT. Based on [dotnet/wpf](https://github.com/dotnet/wpf) (MIT).
Third-party fork; not affiliated with or endorsed by Microsoft.
