# Build the WPF product assemblies whose source we may edit per the
# autoresearch path allowlist. PresentationCore is the entry point; building it
# transitively rebuilds WindowsBase + System.Xaml via project refs. All three
# locally-built DLLs land at artifacts/bin/<Name>/x64/Release/net10.0/<Name>.dll
# and are then staged + swapped into microbench's publish dir by microbench.py
# so a Tier-B differential A/B sees the locally-built code on both sides.
#
# PresentationFramework is INTENTIONALLY NOT BUILT here. It requires the same
# DWF-skip flags as PresentationCore but its DirectWriteForwarder project ref
# isn't bypassed by `SkipDirectWriteForwarderProjectRef=true` alone — the
# .vcxproj still gets processed somewhere in PF's transitive ref graph and
# fails on the missing $(VCTargetsPath)\Microsoft.Cpp.Default.props import.
# Until that's resolved, *WindowLifecycle* (the only currently-targeted PF
# filter) is skip-listed in program.md, so the harness gap doesn't surface.
#
# ApiCompat and ref-asm checks are disabled (we don't lay out reference
# assemblies). DirectWriteForwarder.vcxproj is bypassed by referencing the
# DirectWriteForwarder.dll from the installed WindowsDesktop runtime instead.
$ErrorActionPreference = 'Stop'

$pcCs = Join-Path $PSScriptRoot 'src\Microsoft.DotNet.Wpf\src\PresentationCore\PresentationCore.csproj'
$dwf = 'C:\Program Files\dotnet\shared\Microsoft.WindowsDesktop.App\10.0.7\DirectWriteForwarder.dll'
if (-not (Test-Path $dwf)) { throw "missing DWF: $dwf" }

Write-Host "[build-pc-perf] PresentationCore.csproj (transitively builds WindowsBase + System.Xaml)"
& dotnet build $pcCs `
    -c Release `
    -p:Platform=x64 `
    -p:RunNetFrameworkApiCompat=false `
    -p:RunRefApiCompat=false `
    -p:RunNetCoreApiCompat=false `
    -p:DisableApiCompat=true `
    -p:PerlCommand='C:\Strawberry\perl\bin\perl.exe' `
    -p:TreatWarningsAsErrors=false `
    -p:RunAnalyzers=false `
    -p:SkipDirectWriteForwarderProjectRef=true `
    -p:DirectWriteForwarderBinaryPath=$dwf `
    -nologo /clp:ErrorsOnly
exit $LASTEXITCODE
