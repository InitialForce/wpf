# Publishes the BenchmarkDotNet harness self-contained against a packed InitialForce.WPF
# version and runs it, writing BenchmarkDotNet JSON reports under <artifacts-dir>.
#
# The harness is self-contained (like the smoke app), so it cannot start from build
# output with `dotnet run`: hostpolicy.dll is only laid down by publish. The package
# sources (local packed feed and nuget.org) must already be in NuGet.config.
#
# Usage: pwsh tools/run-perf-harness.ps1 <InitialForceWpfVersion> <artifacts-dir>
param(
  [Parameter(Mandatory)] [string] $Version,
  [Parameter(Mandatory)] [string] $ArtifactsDir
)
$ErrorActionPreference = 'Stop'

$proj = "test/InitialForce.WpfSmoke/Perf/InitialForce.WpfPerf.csproj"
$publish = Join-Path $env:RUNNER_TEMP "perf-publish"

dotnet restore $proj -p:InitialForceWpfVersion=$Version
if ($LASTEXITCODE -ne 0) { throw "restore failed" }
dotnet publish $proj -c Release --no-restore -p:InitialForceWpfVersion=$Version -o $publish
if ($LASTEXITCODE -ne 0) { throw "publish failed" }
& (Join-Path $PSScriptRoot "register-publish-assemblies.ps1") $publish "InitialForce.WpfPerf"

& (Join-Path $publish "InitialForce.WpfPerf.exe") --filter '*' --exporters json --artifacts $ArtifactsDir
if ($LASTEXITCODE -ne 0) { throw "BenchmarkDotNet run failed (exit $LASTEXITCODE)" }
$reports = @(Get-ChildItem $ArtifactsDir -Recurse -Filter '*-report-full*.json' -ErrorAction SilentlyContinue)
if ($reports.Count -eq 0) { throw "BenchmarkDotNet wrote no JSON report under $ArtifactsDir" }
Write-Host "Perf harness ran: $($reports.Count) JSON report(s)"
