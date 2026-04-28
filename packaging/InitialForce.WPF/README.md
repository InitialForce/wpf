# InitialForce.WPF

Patched WPF managed assemblies for Swing Catalyst / Motion Catalyst.

Based on [dotnet/wpf](https://github.com/dotnet/wpf) (MIT). Third-party fork — not endorsed by Microsoft.

## What this package does

`InitialForce.WPF` ships four patched WPF managed assemblies:

| Assembly | Description |
|---|---|
| `PresentationCore.dll` | Core WPF types: visual tree, input, imaging |
| `PresentationFramework.dll` | Controls, data binding, layout, styles |
| `WindowsBase.dll` | DependencyObject, threading primitives |
| `System.Xaml.dll` | XAML parsing and type conversion |

An MSBuild targets file (`InitialForce.WPF.targets`) is auto-imported into every project
that references this package. It:

1. Removes the four Microsoft-supplied DLLs from MSBuild's copy lists so the runtime
   pack versions are never copied to your output directory.
2. Copies the patched DLLs to your output directory on every build (F5 inner-loop) and
   on publish — using the same msquic precedent from `InitialForce.App.csproj`.

## Installation

```xml
<PackageReference Include="InitialForce.WPF" Version="10.0.x-if.*" />
```

No other changes are required. The MSBuild targets file fires automatically.

## Requirements

- .NET 10 (`net10.0-windows`)
- `RuntimeIdentifier` must be `win-x64` (ARM64 support deferred to a future release)
- Self-contained deployment is supported and recommended (matches Swing Catalyst's deployment model)

## Verifying the override worked

After building your project, confirm that:

```
bin/Debug/net10.0-windows/win-x64/PresentationFramework.dll
```

does **not** come from `%DOTNET_ROOT%\packs\Microsoft.WindowsDesktop.App.Ref\...`. You can
check with:

```powershell
(Get-Item "$env:OutDir\PresentationFramework.dll").VersionInfo.FileVersion
```

It should match the version reported by this package, not the version of the .NET 10 SDK
you have installed.

## Fallback package

If the runtime override does not fire (unusual project types, future SDK changes), use
`InitialForce.WPF.RuntimeOverride` instead. It adds explicit `<Reference>` items with
`HintPath` so the patched DLLs win at both compile time and runtime.

## License

MIT. See [LICENSE.TXT](../../LICENSE.TXT) and [NOTICE.md](../../NOTICE.md) for attribution.

## Known limitations

- Only `win-x64` is supported in v1. `x86` and `arm64` consumers fall through silently to
  Microsoft's runtime pack.
- Framework-dependent publish is untested. Use self-contained deployment to ensure
  correct DLL resolution.
- See `docs/known-limitations.md` for the full list.

## Not endorsed by Microsoft

This is an unofficial fork. Performance patches are submitted upstream to dotnet/wpf; once
accepted upstream they are removed from this package on the next release.
