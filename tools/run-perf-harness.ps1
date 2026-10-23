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

# The WPF Arcade SDK leaves NuGet package assemblies (BenchmarkDotNet and its
# dependencies) out of the publish output even with CopyLocalLockFileAssemblies, as it
# does for NUnit in the smoke job. Copy every win-x64 runtime asset from the restore
# assets file that is not already there. The patched WPF DLLs are already in place and
# are skipped because the file exists.
$assetsFile = (dotnet msbuild $proj -getProperty:ProjectAssetsFile -p:InitialForceWpfVersion=$Version).Trim()
if (-not (Test-Path $assetsFile)) { throw "restore assets file not found: '$assetsFile'" }
$assets = Get-Content $assetsFile -Raw | ConvertFrom-Json
$pkgRoot = @($assets.packageFolders.PSObject.Properties.Name)[0]
$target = $assets.targets.PSObject.Properties | Where-Object { $_.Name -like '*/win-x64' } | Select-Object -First 1
if (-not $target) { throw "project.assets.json has no win-x64 target" }
$copied = 0
foreach ($lib in $target.Value.PSObject.Properties) {
  if ($lib.Value.type -ne 'package' -or $null -eq $lib.Value.runtime) { continue }
  $libPath = $assets.libraries.($lib.Name).path
  foreach ($asset in $lib.Value.runtime.PSObject.Properties.Name) {
    if ($asset.EndsWith('_._')) { continue }
    $dest = Join-Path $publish ([System.IO.Path]::GetFileName($asset))
    if (Test-Path $dest) { continue }
    Copy-Item (Join-Path (Join-Path $pkgRoot $libPath) $asset) $dest
    $copied++
  }
}
Write-Host "Copied $copied package assemblies into $publish"
& (Join-Path $PSScriptRoot "register-publish-assemblies.ps1") $publish "InitialForce.WpfPerf"

& (Join-Path $publish "InitialForce.WpfPerf.exe") --filter '*' --exporters json --artifacts $ArtifactsDir
if ($LASTEXITCODE -ne 0) { throw "BenchmarkDotNet run failed (exit $LASTEXITCODE)" }
$reports = @(Get-ChildItem $ArtifactsDir -Recurse -Filter '*-report-full*.json' -ErrorAction SilentlyContinue)
if ($reports.Count -eq 0) { throw "BenchmarkDotNet wrote no JSON report under $ArtifactsDir" }
Write-Host "Perf harness ran: $($reports.Count) JSON report(s)"
