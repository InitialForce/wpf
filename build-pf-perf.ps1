# Build PresentationFramework.dll (Release, x64) for the autoresearch harness.
#
# PresentationFramework depends on PresentationCore which transitively rebuilds
# WindowsBase + System.Xaml. All locally-built DLLs land at:
#   artifacts/bin/<Name>/x64/Release/net10.0/<Name>.dll
#
# The DirectWriteForwarder.vcxproj C++ build is bypassed by passing
# SkipDirectWriteForwarderProjectRef=true + DirectWriteForwarderBinaryPath pointing
# at the installed WindowsDesktop runtime copy (same technique as build-pc-perf.ps1).
#
# ReachFramework.csproj and System.Printing-ref.csproj are additional project refs
# in PF that do not require a C++ toolchain; they build cleanly under dotnet build.
# PresentationUI-PresentationFramework-impl-cycle.csproj is a cycle-breaker shim
# that also builds cleanly.
#
# ApiCompat and ref-asm checks are disabled (we don't lay out reference assemblies).
$ErrorActionPreference = 'Stop'

$pfCsProj = Join-Path $PSScriptRoot 'src\Microsoft.DotNet.Wpf\src\PresentationFramework\PresentationFramework.csproj'
$dwf = 'C:\Program Files\dotnet\shared\Microsoft.WindowsDesktop.App\10.0.7\DirectWriteForwarder.dll'
if (-not (Test-Path $dwf)) { throw "missing DWF: $dwf" }

Write-Host "[build-pf-perf] PresentationFramework.csproj (transitively builds PresentationCore + WindowsBase + System.Xaml)"
& dotnet build $pfCsProj `
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
