#requires -Version 5
<#
.SYNOPSIS
    Runs crossgen2 (ReadyToRun ahead-of-time compilation) on the staged
    InitialForce.WPF DLLs so the published nupkg contains native code,
    matching stock dotnet/wpf behavior.

.DESCRIPTION
    Stock WPF DLLs in Microsoft.WindowsDesktop.App ship with R2R native code
    baked in by the dotnet/runtime build infra during runtime-pack assembly.
    Our fork builds the libraries via build.cmd but does not run the
    runtime-pack step, so the DLLs in artifacts/bin are JIT-only.

    JIT'd frames are slightly fatter than R2R'd frames. WPF code paths that
    are already deep on the stack (notably the dispatcher unhandled-exception
    handler loading MessageDialog.xaml -> BAML -> WPFLocalizeExtension's
    800-culture iteration) overflow the 1 MB thread stack when frames are
    fatter than upstream consumers expect.

    This script crossgen2-compiles the 4 staged DLLs in place. Verified with
    upstream crossgen2 10.0.7.

.PARAMETER StagingDir
    Directory containing the 4 patched DLLs (PresentationCore,
    PresentationFramework, WindowsBase, System.Xaml). The DLLs are replaced
    in place with their R2R-compiled equivalents.

.PARAMETER NetCorePack
    Path to the Microsoft.NETCore.App shared runtime pack (full of *.dll).

.PARAMETER DesktopPack
    Path to the Microsoft.WindowsDesktop.App shared runtime pack.

.PARAMETER Crossgen2Version
    Version of Microsoft.NETCore.App.Crossgen2.win-x64 to fetch from nuget.org.
    Should match the runtime pack's major.minor.

.PARAMETER ToolsCache
    Directory used to cache the downloaded crossgen2 NuGet package.
    Defaults to <repo>/.tools-cache.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $StagingDir,
    [Parameter(Mandatory)] [string] $NetCorePack,
    [Parameter(Mandatory)] [string] $DesktopPack,
    [ValidateSet('x64', 'arm64')] [string] $TargetArch = 'x64',
    [string] $Crossgen2Version = '10.0.7',
    [string] $ToolsCache = (Join-Path $PSScriptRoot '..\.tools-cache')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-Crossgen2Exe {
    param([string] $Version, [string] $Cache)

    $pkgDir = Join-Path $Cache "crossgen2-$Version"
    $exe = Join-Path $pkgDir 'tools\crossgen2.exe'
    if (Test-Path $exe) {
        Write-Host "==> crossgen2 cached at $exe"
        return $exe
    }

    New-Item -ItemType Directory -Path $pkgDir -Force | Out-Null
    $nupkg = Join-Path $pkgDir "crossgen2.$Version.nupkg"
    $url = "https://api.nuget.org/v3-flatcontainer/microsoft.netcore.app.crossgen2.win-x64/$Version/microsoft.netcore.app.crossgen2.win-x64.$Version.nupkg"

    Write-Host "==> Downloading crossgen2 $Version from $url"
    Invoke-WebRequest -Uri $url -OutFile $nupkg -UseBasicParsing

    Write-Host "==> Extracting to $pkgDir"
    # .nupkg is a zip; Expand-Archive insists on a .zip extension, so copy then expand.
    $zip = Join-Path $pkgDir "crossgen2.$Version.zip"
    Copy-Item -Path $nupkg -Destination $zip -Force
    Expand-Archive -Path $zip -DestinationPath $pkgDir -Force
    Remove-Item -Path $zip -Force

    if (-not (Test-Path $exe)) {
        throw "crossgen2.exe not found after extraction: $exe"
    }
    return $exe
}

if (-not (Test-Path $StagingDir))    { throw "StagingDir not found: $StagingDir" }
if (-not (Test-Path $NetCorePack))   { throw "NetCorePack not found: $NetCorePack" }
if (-not (Test-Path $DesktopPack))   { throw "DesktopPack not found: $DesktopPack" }

$crossgen = Get-Crossgen2Exe -Version $Crossgen2Version -Cache $ToolsCache
& $crossgen --version
if ($LASTEXITCODE -ne 0) { throw "crossgen2 --version failed" }

$dlls = @('PresentationCore.dll', 'PresentationFramework.dll', 'WindowsBase.dll', 'System.Xaml.dll')
$tmpOut = Join-Path $StagingDir '.r2r-out'
New-Item -ItemType Directory -Path $tmpOut -Force | Out-Null

foreach ($name in $dlls) {
    $input = Join-Path $StagingDir $name
    if (-not (Test-Path $input)) { throw "Missing staged DLL: $input" }
    $output = Join-Path $tmpOut $name

    Write-Host ""
    Write-Host "==> R2R-compiling $name"
    $inSize = (Get-Item $input).Length

    # crossgen2.exe is the win-x64 binary (only one shipped); use --targetarch
    # to cross-compile to arm64. Reference assemblies are read for managed
    # metadata only, so the host arch's runtime pack works as references for
    # arm64 cross-compilation too.
    & $crossgen `
        --targetos=windows `
        --targetarch=$TargetArch `
        -r "$NetCorePack\*.dll" `
        -r "$DesktopPack\*.dll" `
        -r "$StagingDir\*.dll" `
        -o $output `
        $input

    if ($LASTEXITCODE -ne 0) { throw "crossgen2 failed for $name (exit $LASTEXITCODE)" }

    # Sanity check: output must contain the R2R magic 'RTR\0' near the start.
    $bytes = [System.IO.File]::ReadAllBytes($output)
    $found = $false
    for ($i = 0; $i -lt [Math]::Min($bytes.Length - 4, 65536); $i++) {
        if ($bytes[$i] -eq 0x52 -and $bytes[$i+1] -eq 0x54 -and $bytes[$i+2] -eq 0x52 -and $bytes[$i+3] -eq 0x00) {
            $found = $true
            break
        }
    }
    if (-not $found) { throw "R2R magic 'RTR\0' not found in $name within first 64 KB; crossgen2 produced a non-R2R image" }

    $outSize = $bytes.Length
    $growth = if ($inSize -gt 0) { ($outSize / $inSize - 1) * 100 } else { 0 }
    Write-Host ("    {0,-30} in={1} out={2} growth={3:N1}%" -f $name, $inSize, $outSize, $growth)
}

# Move R2R outputs back over staged inputs.
foreach ($name in $dlls) {
    $src = Join-Path $tmpOut $name
    $dst = Join-Path $StagingDir $name
    Move-Item -Path $src -Destination $dst -Force
}
Remove-Item -Path $tmpOut -Recurse -Force

Write-Host ""
Write-Host "==> Done. $StagingDir now contains R2R-compiled DLLs."
Get-ChildItem $StagingDir -Filter '*.dll' | Format-Table Name, Length
