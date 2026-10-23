# Registers every DLL in a self-contained publish directory that the app's deps.json
# does not list yet.
#
# A self-contained app resolves assemblies strictly from its deps.json TPA and does
# not probe the app base directory for files absent from it. The WPF Arcade SDK strips
# NuGet package assemblies, and the InitialForce.WPF targets strip the patched WPF
# DLLs, from the generated deps.json (the WPF DLLs are still copied into the app
# directory by the package targets, just not registered). Registering them lets the
# host resolve them from the app base directory.
#
# Usage: pwsh tools/register-publish-assemblies.ps1 <publish-dir> <app-name>
param(
  [Parameter(Mandatory)] [string] $PublishDir,
  [Parameter(Mandatory)] [string] $AppName
)
$ErrorActionPreference = 'Stop'

$depsPath = Join-Path $PublishDir "$AppName.deps.json"
$deps = Get-Content $depsPath -Raw | ConvertFrom-Json
$rt = $deps.runtimeTarget.name
$existing = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($lib in $deps.targets.$rt.PSObject.Properties) {
  $runtime = $lib.Value.runtime
  if ($null -ne $runtime) {
    foreach ($r in $runtime.PSObject.Properties) { [void]$existing.Add([System.IO.Path]::GetFileName($r.Name)) }
  }
}
$appProp = $deps.targets.$rt.PSObject.Properties |
           Where-Object { $_.Name -like "$AppName/*" } | Select-Object -First 1
if (-not $appProp) { throw "deps.json has no '$AppName/*' entry under $rt" }
$runtimeObj = $deps.targets.$rt.($appProp.Name).runtime
$added = 0
foreach ($f in Get-ChildItem $PublishDir -Filter '*.dll') {
  if (-not $existing.Contains($f.Name)) {
    $runtimeObj | Add-Member -NotePropertyName $f.Name -NotePropertyValue ([pscustomobject]@{}) -Force
    [void]$existing.Add($f.Name); $added++
    Write-Host "  deps.json += $($f.Name)"
  }
}
Write-Host "Registered $added assemblies in deps.json"
($deps | ConvertTo-Json -Depth 100) | Set-Content $depsPath -Encoding utf8
