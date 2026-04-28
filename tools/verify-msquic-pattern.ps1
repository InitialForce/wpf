#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Verifies that the build output of a project referencing InitialForce.WPF
    contains our patched WPF DLLs and NOT the upstream Microsoft runtime-pack copies.

.DESCRIPTION
    Implements the msquic-pattern verification: after dotnet build, the bin folder
    must contain InitialForce-patched assemblies (identified by AssemblyInformationalVersion
    containing "if." prefix, or by matching a supplied file hash).

    Exit codes:
      0 — all checks passed; output is verified
      2 — one or more mismatches found; build output is NOT verified
      1 — script error (bad arguments, path not found, etc.)

.PARAMETER BuildOutputPath
    Path to the build output folder (e.g. bin/Debug/net10.0-windows/).
    Must contain PresentationFramework.dll, PresentationCore.dll, etc.

.PARAMETER ExpectedAssemblyVersion
    Expected AssemblyInformationalVersion string (e.g. "10.0.4-if.20260427.1").
    All four WPF assemblies must report this exact version.
    If omitted, version checking is skipped and only the "not from runtime pack" heuristic is used.

.PARAMETER ExpectedFileHash
    Optional SHA-256 hex string for the canary DLL (PresentationCore.dll).
    If provided, the hash of the output DLL is compared against this value.

.PARAMETER JsonOutput
    If set, always emit a JSON result object. Otherwise JSON is only emitted if
    -Quiet is not set. Defaults to true (always emit JSON).

.EXAMPLE
    pwsh -NoProfile tools/verify-msquic-pattern.ps1 `
        -BuildOutputPath test/InitialForce.WpfHelloWorld/bin/Debug/net10.0-windows/ `
        -ExpectedAssemblyVersion "10.0.4-if.20260427.1"

.EXAMPLE
    pwsh -NoProfile tools/verify-msquic-pattern.ps1 `
        -BuildOutputPath publish/win-x64/ `
        -ExpectedAssemblyVersion "10.0.4-if.20260427.1" `
        -ExpectedFileHash "a1b2c3d4..."
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $BuildOutputPath,

    [Parameter(Mandatory = $false)]
    [string] $ExpectedAssemblyVersion = "",

    [Parameter(Mandatory = $false)]
    [string] $ExpectedFileHash = "",

    [Parameter(Mandatory = $false)]
    [switch] $JsonOutput = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Get-AssemblyInformationalVersion {
    param([string] $DllPath)
    try {
        $fvi = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($DllPath)
        return $fvi.ProductVersion   # ProductVersion maps to AssemblyInformationalVersion
    }
    catch {
        return $null
    }
}

function Get-FileSha256 {
    param([string] $FilePath)
    $bytes = [System.IO.File]::ReadAllBytes($FilePath)
    $sha   = [System.Security.Cryptography.SHA256]::Create()
    $hash  = $sha.ComputeHash($bytes)
    return ([System.BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
}

function Test-IsFromRuntimePack {
    param([string] $DllPath)
    # A DLL that came from the Microsoft runtime pack rather than from our package
    # will live under the NuGet/dotnet packs cache. If the DLL is IN the build
    # output folder we are verifying, this is only relevant if the DLL's origin
    # is identifiable by its informational version (no "if." suffix).
    # This helper is used as a secondary heuristic when no expected version is given.
    $ver = Get-AssemblyInformationalVersion -DllPath $DllPath
    if ($null -eq $ver) { return $true }   # can't tell — assume bad
    # Microsoft upstream versions look like "10.0.0.0+abc123" with no "if." component.
    return ($ver -notmatch 'if\.')
}

# ---------------------------------------------------------------------------
# Canary assembly list — the four WPF managed DLLs patched by this fork.
# ---------------------------------------------------------------------------
$CanaryAssemblies = @(
    "PresentationCore.dll",
    "PresentationFramework.dll",
    "WindowsBase.dll",
    "System.Xaml.dll"
)

# ---------------------------------------------------------------------------
# Validate inputs
# ---------------------------------------------------------------------------
$resolvedOutputPath = Resolve-Path -Path $BuildOutputPath -ErrorAction SilentlyContinue
if (-not $resolvedOutputPath) {
    $result = @{
        verified   = $false
        mismatches = @("BuildOutputPath not found: $BuildOutputPath")
        error      = "path_not_found"
    }
    Write-Output ($result | ConvertTo-Json -Depth 4)
    exit 1
}
$BuildOutputPath = $resolvedOutputPath.Path

# ---------------------------------------------------------------------------
# Run checks
# ---------------------------------------------------------------------------
$mismatches = [System.Collections.Generic.List[object]]::new()
$checked    = [System.Collections.Generic.List[object]]::new()

foreach ($dllName in $CanaryAssemblies) {
    $dllPath = Join-Path $BuildOutputPath $dllName

    if (-not (Test-Path $dllPath)) {
        $mismatches.Add(@{
            assembly = $dllName
            reason   = "file_missing"
            detail   = "Expected DLL not found in build output: $dllPath"
        })
        continue
    }

    $actualVersion = Get-AssemblyInformationalVersion -DllPath $dllPath
    $entry = @{
        assembly       = $dllName
        path           = $dllPath
        actualVersion  = $actualVersion
    }

    # Check 1: version string must contain "if." (InitialForce tag)
    if ($actualVersion -notmatch 'if\.') {
        $mismatches.Add(@{
            assembly        = $dllName
            reason          = "not_initialforce_version"
            detail          = "Assembly informational version '$actualVersion' does not contain 'if.' — looks like an upstream Microsoft DLL"
            actualVersion   = $actualVersion
            expectedPattern = "*if.*"
        })
    }

    # Check 2: if a specific expected version was provided, require exact match
    if ($ExpectedAssemblyVersion -ne "" -and $actualVersion -ne $ExpectedAssemblyVersion) {
        $mismatches.Add(@{
            assembly         = $dllName
            reason           = "version_mismatch"
            detail           = "Version mismatch"
            actualVersion    = $actualVersion
            expectedVersion  = $ExpectedAssemblyVersion
        })
    }

    $checked.Add($entry)
}

# Check 3: optional file-hash check on PresentationCore.dll (canary)
if ($ExpectedFileHash -ne "") {
    $coredll = Join-Path $BuildOutputPath "PresentationCore.dll"
    if (Test-Path $coredll) {
        $actualHash = Get-FileSha256 -FilePath $coredll
        if ($actualHash -ne $ExpectedFileHash.ToLowerInvariant()) {
            $mismatches.Add(@{
                assembly      = "PresentationCore.dll"
                reason        = "hash_mismatch"
                detail        = "SHA-256 hash of PresentationCore.dll does not match expected canary hash"
                actualHash    = $actualHash
                expectedHash  = $ExpectedFileHash.ToLowerInvariant()
            })
        }
    }
}

# ---------------------------------------------------------------------------
# Emit result
# ---------------------------------------------------------------------------
$verified = ($mismatches.Count -eq 0)

$result = [ordered]@{
    verified      = $verified
    buildOutput   = $BuildOutputPath
    checked       = @($checked)
    mismatches    = @($mismatches)
}

$json = $result | ConvertTo-Json -Depth 6
Write-Output $json

if ($verified) {
    Write-Host "verify-msquic-pattern: PASS — all $($CanaryAssemblies.Count) WPF assemblies are InitialForce-patched." -ForegroundColor Green
    exit 0
}
else {
    Write-Host "verify-msquic-pattern: FAIL — $($mismatches.Count) mismatch(es) found." -ForegroundColor Red
    exit 2
}
