# InitialForce.WpfHelloWorld — msquic-pattern verification test app

A minimal WPF application that references `InitialForce.WPF` and is used to
verify the msquic-pattern DLL swap fires correctly at build time and publish
time.

## What is the msquic pattern?

The msquic pattern is a two-target MSBuild technique used by `InitialForce.WPF`
to replace the four Microsoft-supplied WPF managed assemblies
(`PresentationCore.dll`, `PresentationFramework.dll`, `WindowsBase.dll`,
`System.Xaml.dll`) with our patched copies at every build and publish step.

**Target 1 — `RemoveRuntimeWpfAssets`:** fires after
`ResolveRuntimePackAssets` and `ResolveLockFileCopyLocalFiles`, removing the
Microsoft copies from all MSBuild item groups that cause them to be copied to
the output folder.

**Target 2 — `InjectIfWpfAssemblies`:** fires after `Build` and
`CopyFilesToOutputDirectory`, copying our patched DLLs from the NuGet package
cache into the output folder. This fires on every inner-loop F5 build, not only
at publish.

The pattern is adapted verbatim from the `msquic.dll` replacement in Swing
Catalyst's `InitialForce.App.csproj`, where the same two-target structure is
used to prefer the OpenSSL-linked msquic over the default Schannel one.

## Project layout

```
test/InitialForce.WpfHelloWorld/
├── InitialForce.WpfHelloWorld.csproj   ← references InitialForce.WPF
├── App.xaml / App.xaml.cs              ← WPF Application class
├── MainWindow.xaml / MainWindow.xaml.cs← simple window showing PF version + location
├── Program.cs                          ← [STAThread] entry point
└── README.md                           ← this file
```

## Running msquic-pattern verification locally

> **WSL note:** `dotnet build` for WPF requires Windows. All static gate checks
> (script syntax, Pester tests) run under WSL. The dotnet build + DLL
> verification step requires Windows CI or a Windows workstation.

### Step 1 — Build the hello-world app (Windows only)

```powershell
# From the repo root on Windows:
dotnet build test/InitialForce.WpfHelloWorld/InitialForce.WpfHelloWorld.csproj `
    -p:InitialForceWpfVersion=10.0.4-if.20260427.1 `
    --source artifacts/nuget/
```

The build output lands at:
`test/InitialForce.WpfHelloWorld/bin/Debug/net10.0-windows/win-x64/`

### Step 2 — Run the verification script

```powershell
pwsh -NoProfile tools/verify-msquic-pattern.ps1 `
    -BuildOutputPath "test/InitialForce.WpfHelloWorld/bin/Debug/net10.0-windows/win-x64/" `
    -ExpectedAssemblyVersion "10.0.4-if.20260427.1"
```

**Expected output (pass):**

```json
{
  "verified": true,
  "buildOutput": "...",
  "checked": [...],
  "mismatches": []
}
```

Exit code `0` means all four patched DLLs are present with the correct
`AssemblyInformationalVersion`.

**If you see exit code `2`:** the MSBuild targets did not fire. Check that:
- The `InitialForce.WPF` package was restored from the local feed.
- The `.targets` file is present in the NuGet package cache under
  `~/.nuget/packages/initialforce.wpf/<version>/build/`.
- MSBuild output includes `InjectIfWpfAssemblies: copied InitialForce.WPF
  patched DLLs to ...`.

### Step 3 — Optional: verify publish output

```powershell
dotnet publish test/InitialForce.WpfHelloWorld/InitialForce.WpfHelloWorld.csproj `
    -p:InitialForceWpfVersion=10.0.4-if.20260427.1 `
    --source artifacts/nuget/ `
    -o publish/HelloWorld

pwsh -NoProfile tools/verify-msquic-pattern.ps1 `
    -BuildOutputPath "publish/HelloWorld/" `
    -ExpectedAssemblyVersion "10.0.4-if.20260427.1"
```

### Step 4 — Optional: canary hash check

To also verify the exact binary hash of `PresentationCore.dll` (detecting any
silent DLL substitution at the file level):

```powershell
# Compute the expected hash from the package source.
$hash = (Get-FileHash `
    packaging/InitialForce.WPF/runtimes/win-x64/lib/net10.0-windows/PresentationCore.dll `
    -Algorithm SHA256).Hash.ToLowerInvariant()

pwsh -NoProfile tools/verify-msquic-pattern.ps1 `
    -BuildOutputPath "test/InitialForce.WpfHelloWorld/bin/Debug/net10.0-windows/win-x64/" `
    -ExpectedAssemblyVersion "10.0.4-if.20260427.1" `
    -ExpectedFileHash $hash
```

## Running Pester static/unit tests (WSL-compatible)

The `verify-msquic-pattern.Tests.ps1` file exercises the script's contract
using fixture directories — no `dotnet build` required.

```bash
# From the repo root:
pwsh -NoProfile -Command "Invoke-Pester tools/verify-msquic-pattern.Tests.ps1 -Output Detailed"
```

All Pester tests should pass on WSL without a Windows DLL build.

## CI integration

The CI workflow `verify-swap.yml` (defined in `.github/workflows/`) runs on
every PR that touches `packaging/InitialForce.WPF/`:

1. Builds `InitialForce.WPF` NuGet package into `artifacts/nuget/`.
2. Runs `dotnet build` of this hello-world app referencing the local package.
3. Calls `tools/verify-msquic-pattern.ps1` on the build output.
4. Runs `dotnet publish` (self-contained) and repeats the verification.

> **Deferred:** The CI workflow itself (`verify-swap.yml`) is authored in bead
> `wpf-1j6`. This hello-world app + verification script are the static
> deliverables for bead `wpf-w3v`.

## Interpreting failures

| Exit code | Meaning |
|-----------|---------|
| `0` | All four patched DLLs are present and carry the `if.` version tag. |
| `1` | Script error — `BuildOutputPath` not found or invalid arguments. |
| `2` | One or more canary DLLs are missing, carry the wrong version, or fail the hash check. The `mismatches` array in the JSON output lists each failure with a `reason` field (`file_missing`, `not_initialforce_version`, `version_mismatch`, or `hash_mismatch`). |

The most common failure is `not_initialforce_version`, meaning the MSBuild
targets did not fire and the Microsoft runtime-pack DLLs were used instead.
