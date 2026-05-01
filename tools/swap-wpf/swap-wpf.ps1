<#
.SYNOPSIS
    swap-wpf — swap patched WPF DLLs into MotionCatalyst's build output and restore originals.

.DESCRIPTION
    Manages swapping of 4 WPF DLLs (PresentationCore, PresentationFramework, System.Xaml,
    WindowsBase) plus their PDBs from a fork build output into MC's BUILD/x64_Release
    directory. Backs up originals with md5 verification before and after each operation.

.PARAMETER Command
    The action to perform: swap | restore | status

.EXAMPLE
    swap-wpf.ps1 swap -ForkOutputDir C:\wpf-fork\artifacts\bin\PresentationCore\Release -McBuildDir C:\MC\BUILD\x64_Release

.EXAMPLE
    swap-wpf.ps1 restore -McBuildDir C:\MC\BUILD\x64_Release

.EXAMPLE
    swap-wpf.ps1 status -McBuildDir C:\MC\BUILD\x64_Release

.EXAMPLE
    swap-wpf.ps1 status -McBuildDir C:\MC\BUILD\x64_Release -Json
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('swap', 'restore', 'status')]
    [string]$Command,

    # swap params
    [string]$ForkOutputDir,
    [switch]$Force,

    # restore params
    [switch]$CleanBackup,

    # shared
    [string]$McBuildDir,

    # output mode
    [switch]$Json,

    # help
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

$BACKUP_DIR_NAME    = 'swap-wpf-backup'
$APPLIED_JSON_NAME  = 'swap-wpf-applied.json'
$MANIFEST_JSON_NAME = 'manifest.json'

# Core DLLs that must always be present (relative names, case-insensitive on Windows)
$REQUIRED_DLLS = @(
    'PresentationCore.dll',
    'PresentationFramework.dll',
    'System.Xaml.dll',
    'WindowsBase.dll'
)

# PDB counterparts (optional — copied if present)
$OPTIONAL_PDBS = @(
    'PresentationCore.pdb',
    'PresentationFramework.pdb',
    'System.Xaml.pdb',
    'WindowsBase.pdb'
)

# Native DLL patterns (optional — copied if matching files found)
$NATIVE_PATTERNS = @('PresentationNative_*.dll', 'PresentationNative_*.pdb')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Info  ([string]$msg) { if (-not $Json) { Write-Host "  $msg" } }
function Write-Step  ([string]$msg) { if (-not $Json) { Write-Host "[>] $msg" } }
function Write-Ok    ([string]$msg) { if (-not $Json) { Write-Host "[+] $msg" -ForegroundColor Green } }
function Write-Warn  ([string]$msg) { if (-not $Json) { Write-Host "[!] $msg" -ForegroundColor Yellow } }
function Write-Fail  ([string]$msg) { if (-not $Json) { Write-Host "[x] $msg" -ForegroundColor Red } }

function ConvertFrom-WslPath ([string]$path) {
    # Convert /c/work/... or /mnt/c/work/... to Windows path
    if ($path -match '^/mnt/([a-zA-Z])/(.*)$') {
        return "$($Matches[1].ToUpper()):\$($Matches[2] -replace '/', '\')"
    }
    if ($path -match '^/([a-zA-Z])/(.*)$') {
        return "$($Matches[1].ToUpper()):\$($Matches[2] -replace '/', '\')"
    }
    return $path
}

function Get-Md5 ([string]$filePath) {
    $hash = Get-FileHash -Path $filePath -Algorithm MD5
    return $hash.Hash.ToUpperInvariant()
}

function Assert-PathExists ([string]$path, [string]$label) {
    if (-not (Test-Path $path)) {
        throw "Required path does not exist ($label): $path"
    }
}

function Copy-FileVerified {
    <#
    .SYNOPSIS Copy $src → $dst and verify md5 matches. Returns the md5 string.
    #>
    param(
        [string]$src,
        [string]$dst
    )
    $srcHash = Get-Md5 $src
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $dstHash = Get-Md5 $dst
    if ($srcHash -ne $dstHash) {
        throw "MD5 mismatch after copy!`n  src $src => $srcHash`n  dst $dst => $dstHash"
    }
    return $srcHash
}

# ---------------------------------------------------------------------------
# Usage / Help
# ---------------------------------------------------------------------------

function Show-Help {
    @"
swap-wpf.ps1 — swap patched WPF DLLs into MotionCatalyst build output

COMMANDS
  swap     Copy patched DLLs from fork output into MC build dir (backs up originals)
  restore  Revert to original backed-up DLLs
  status   Show what is currently installed

USAGE
  swap-wpf.ps1 swap -ForkOutputDir <path> -McBuildDir <path> [-Force] [-Json]
  swap-wpf.ps1 restore -McBuildDir <path> [-CleanBackup] [-Json]
  swap-wpf.ps1 status -McBuildDir <path> [-Json]

PARAMETERS (swap)
  -ForkOutputDir <path>   Directory containing the fork-built DLLs and PDBs.
                          Accepts Windows paths or WSL /c/... paths.
  -McBuildDir <path>      MotionCatalyst BUILD/x64_Release directory.
  -Force                  Overwrite an already-applied swap without restoring first.
                          Refreshes the backup with the DLLs currently in place.

PARAMETERS (restore)
  -McBuildDir <path>      MotionCatalyst BUILD/x64_Release directory.
  -CleanBackup            Delete the backup directory after successful restore.

PARAMETERS (shared)
  -Json                   Emit machine-readable JSON to stdout instead of human output.
  -Help                   Show this help text.

EXAMPLES
  # Swap in fork build
  swap-wpf.ps1 swap `
    -ForkOutputDir C:\wpf-fork\artifacts\Release `
    -McBuildDir    C:\MC\BUILD\x64_Release

  # Check status
  swap-wpf.ps1 status -McBuildDir C:\MC\BUILD\x64_Release

  # Restore originals
  swap-wpf.ps1 restore -McBuildDir C:\MC\BUILD\x64_Release

  # Run from WSL
  cmd.exe /c "powershell -ExecutionPolicy Bypass -File swap-wpf.ps1 status -McBuildDir /c/work/..."

TRACKED FILES
  PresentationCore.dll/.pdb, PresentationFramework.dll/.pdb,
  System.Xaml.dll/.pdb, WindowsBase.dll/.pdb
  PresentationNative_*.dll/.pdb  (if present in fork output)

SWAP STATE FILES
  <McBuildDir>\swap-wpf-backup\manifest.json   Original DLL md5s
  <McBuildDir>\swap-wpf-applied.json           Active swap record
"@
}

# ---------------------------------------------------------------------------
# Discover DLLs in a directory
# ---------------------------------------------------------------------------

function Get-CandidateFiles ([string]$dir) {
    <#
    Returns an ordered list of file names (relative, no path) that swap-wpf manages,
    based on what is present in $dir. Required DLLs must all exist; optional PDBs
    and native DLLs are included only if found.
    #>
    $files = [System.Collections.Generic.List[string]]::new()

    foreach ($dll in $REQUIRED_DLLS) {
        $p = Join-Path $dir $dll
        if (-not (Test-Path $p)) {
            throw "Required DLL not found in '$dir': $dll"
        }
        $files.Add($dll)
    }

    foreach ($pdb in $OPTIONAL_PDBS) {
        $p = Join-Path $dir $pdb
        if (Test-Path $p) { $files.Add($pdb) }
    }

    foreach ($pat in $NATIVE_PATTERNS) {
        Get-ChildItem -Path $dir -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object {
            $files.Add($_.Name)
        }
    }

    return $files
}

# ---------------------------------------------------------------------------
# Command: swap
# ---------------------------------------------------------------------------

function Invoke-Swap {
    param(
        [string]$forkDir,
        [string]$mcDir,
        [bool]$force
    )

    Write-Step "swap: fork='$forkDir'  mc='$mcDir'"

    Assert-PathExists $forkDir 'ForkOutputDir'
    Assert-PathExists $mcDir   'McBuildDir'

    $appliedJson = Join-Path $mcDir $APPLIED_JSON_NAME
    $backupDir   = Join-Path $mcDir $BACKUP_DIR_NAME

    # Guard: refuse if a swap is already applied and -Force not set
    if ((Test-Path $appliedJson) -and -not $force) {
        throw "A swap is already active ($appliedJson). Run 'restore' first, or pass -Force to overwrite."
    }

    # Discover what files to transfer
    Write-Step "Discovering DLLs in fork output dir..."
    $fileNames = Get-CandidateFiles $forkDir
    Write-Info "Found $($fileNames.Count) files: $($fileNames -join ', ')"

    # Compute source hashes
    $sourceHashes = @{}
    foreach ($name in $fileNames) {
        $src = Join-Path $forkDir $name
        $sourceHashes[$name] = Get-Md5 $src
        Write-Info "  src $name  $($sourceHashes[$name])"
    }

    # ------------------------------------------------------------------
    # Backup phase — back up the CURRENT files in mc dir (only once, unless -Force)
    # ------------------------------------------------------------------
    $needsBackup = $true
    if ((Test-Path $backupDir) -and (Test-Path (Join-Path $backupDir $MANIFEST_JSON_NAME))) {
        if ($force) {
            Write-Warn "-Force: refreshing backup with files currently in place..."
        } else {
            Write-Info "Backup already exists — reusing existing backup."
            $needsBackup = $false
        }
    }

    if ($needsBackup) {
        Write-Step "Backing up current DLLs to '$backupDir'..."
        if (-not (Test-Path $backupDir)) {
            New-Item -ItemType Directory -Path $backupDir | Out-Null
        }

        $manifestEntries = @{}
        foreach ($name in $fileNames) {
            $src = Join-Path $mcDir $name
            if (Test-Path $src) {
                $dst = Join-Path $backupDir $name
                $hash = Copy-FileVerified $src $dst
                $manifestEntries[$name] = $hash
                Write-Info "  backed up $name  $hash"
            } else {
                Write-Warn "  $name not found in McBuildDir — will not be in backup (new file)"
            }
        }

        $manifest = @{
            created   = (Get-Date -Format 'o')
            mc_dir    = $mcDir
            files     = $manifestEntries
        }
        $manifest | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $backupDir $MANIFEST_JSON_NAME) -Encoding UTF8
        Write-Ok "Backup complete ($($manifestEntries.Count) files)."
    }

    # ------------------------------------------------------------------
    # Copy phase
    # ------------------------------------------------------------------
    Write-Step "Copying fork DLLs into '$mcDir'..."
    $appliedEntries = @{}
    $failed = @()

    foreach ($name in $fileNames) {
        $src = Join-Path $forkDir $name
        $dst = Join-Path $mcDir $name
        try {
            $hash = Copy-FileVerified $src $dst
            $appliedEntries[$name] = @{ src_md5 = $sourceHashes[$name]; dst_md5 = $hash }
            Write-Info "  copied $name  $hash"
        } catch {
            $failed += "  FAILED $name : $_"
        }
    }

    if ($failed.Count -gt 0) {
        # Attempt rollback — restore backup
        Write-Fail "Copy phase had $($failed.Count) failure(s). Attempting rollback..."
        foreach ($name in $fileNames) {
            $bak = Join-Path $backupDir $name
            $dst = Join-Path $mcDir $name
            if (Test-Path $bak) { Copy-Item -LiteralPath $bak -Destination $dst -Force -ErrorAction SilentlyContinue }
        }
        throw "Swap failed and was rolled back.`n$($failed -join "`n")"
    }

    # ------------------------------------------------------------------
    # Write applied marker
    # ------------------------------------------------------------------
    $applied = @{
        timestamp    = (Get-Date -Format 'o')
        fork_dir     = $forkDir
        mc_dir       = $mcDir
        file_count   = $fileNames.Count
        files        = $appliedEntries
    }
    $applied | ConvertTo-Json -Depth 5 | Set-Content -Path $appliedJson -Encoding UTF8

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    $result = @{
        status    = 'swapped'
        timestamp = $applied.timestamp
        fork_dir  = $forkDir
        mc_dir    = $mcDir
        files     = $fileNames
        hashes    = $appliedEntries
    }

    if ($Json) {
        $result | ConvertTo-Json -Depth 5
    } else {
        Write-Ok "Swap complete — $($fileNames.Count) files replaced."
        Write-Info ""
        Write-Info "File                               Source MD5"
        Write-Info ("-" * 70)
        foreach ($name in $fileNames) {
            $h = $appliedEntries[$name].src_md5
            Write-Info ("  {0,-38} {1}" -f $name, $h)
        }
        Write-Info ""
        Write-Info "Restore: swap-wpf.ps1 restore -McBuildDir '$mcDir'"
    }
}

# ---------------------------------------------------------------------------
# Command: restore
# ---------------------------------------------------------------------------

function Invoke-Restore {
    param(
        [string]$mcDir,
        [bool]$cleanBackup
    )

    Write-Step "restore: mc='$mcDir'"

    Assert-PathExists $mcDir 'McBuildDir'

    $backupDir   = Join-Path $mcDir $BACKUP_DIR_NAME
    $manifestPath = Join-Path $backupDir $MANIFEST_JSON_NAME
    $appliedJson  = Join-Path $mcDir $APPLIED_JSON_NAME

    Assert-PathExists $backupDir    'BackupDir'
    Assert-PathExists $manifestPath 'manifest.json'

    # Load manifest
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $fileNames = @($manifest.files.PSObject.Properties.Name)

    Write-Info "Restoring $($fileNames.Count) files from backup..."

    $failed = @()
    $restoredHashes = @{}

    foreach ($name in $fileNames) {
        $src = Join-Path $backupDir $name
        $dst = Join-Path $mcDir $name
        $expectedHash = $manifest.files.$name

        if (-not (Test-Path $src)) {
            $failed += "Backup file missing: $src"
            continue
        }

        try {
            $actualHash = Copy-FileVerified $src $dst
            $restoredHashes[$name] = $actualHash

            # Verify against manifest
            if ($actualHash -ne $expectedHash.ToUpperInvariant()) {
                $failed += "MD5 mismatch after restore: $name`n  expected=$expectedHash`n  actual=$actualHash"
            } else {
                Write-Info "  restored $name  $actualHash"
            }
        } catch {
            $failed += "  FAILED $name : $_"
        }
    }

    if ($failed.Count -gt 0) {
        throw "Restore had $($failed.Count) failure(s):`n$($failed -join "`n")"
    }

    # Remove applied marker
    if (Test-Path $appliedJson) {
        Remove-Item $appliedJson -Force
        Write-Info "Removed $APPLIED_JSON_NAME"
    }

    # Optionally remove backup dir
    if ($cleanBackup) {
        Remove-Item $backupDir -Recurse -Force
        Write-Info "Removed backup directory."
    }

    $result = @{
        status    = 'restored'
        timestamp = (Get-Date -Format 'o')
        mc_dir    = $mcDir
        files     = $fileNames
        hashes    = $restoredHashes
    }

    if ($Json) {
        $result | ConvertTo-Json -Depth 5
    } else {
        Write-Ok "Restore complete — $($fileNames.Count) files reverted to originals."
        Write-Info ""
        Write-Info "File                               Restored MD5"
        Write-Info ("-" * 70)
        foreach ($name in $fileNames) {
            Write-Info ("  {0,-38} {1}" -f $name, $restoredHashes[$name])
        }
    }
}

# ---------------------------------------------------------------------------
# Command: status
# ---------------------------------------------------------------------------

function Invoke-Status {
    param([string]$mcDir)

    Assert-PathExists $mcDir 'McBuildDir'

    $appliedJson = Join-Path $mcDir $APPLIED_JSON_NAME
    $backupDir   = Join-Path $mcDir $BACKUP_DIR_NAME
    $manifestPath = Join-Path $backupDir $MANIFEST_JSON_NAME

    if (Test-Path $appliedJson) {
        $applied = Get-Content $appliedJson -Raw | ConvertFrom-Json

        $result = @{
            state     = 'swapped'
            timestamp = $applied.timestamp
            fork_dir  = $applied.fork_dir
            mc_dir    = $mcDir
            files     = @($applied.files.PSObject.Properties.Name)
            details   = $applied
        }

        if ($Json) {
            $result | ConvertTo-Json -Depth 6
        } else {
            Write-Host ""
            Write-Host "State   : SWAPPED (patched fork DLLs are active)" -ForegroundColor Cyan
            Write-Host "Applied : $($applied.timestamp)"
            Write-Host "Fork    : $($applied.fork_dir)"
            Write-Host "MC dir  : $mcDir"
            Write-Host ""
            Write-Host "File                               Src MD5"
            Write-Host ("-" * 70)
            foreach ($name in $applied.files.PSObject.Properties.Name) {
                $h = $applied.files.$name.src_md5
                Write-Host ("  {0,-38} {1}" -f $name, $h)
            }
            Write-Host ""
            Write-Host "Restore: swap-wpf.ps1 restore -McBuildDir '$mcDir'"
            Write-Host ""
        }
    } else {
        $backupPresent = Test-Path $manifestPath

        $result = @{
            state      = 'original'
            mc_dir     = $mcDir
            backup_dir = if ($backupPresent) { $backupDir } else { $null }
        }

        if ($backupPresent) {
            $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
            $result.backup_created = $manifest.created
            $result.backup_files = @($manifest.files.PSObject.Properties.Name)
        }

        if ($Json) {
            $result | ConvertTo-Json -Depth 4
        } else {
            Write-Host ""
            Write-Host "State   : ORIGINAL (no active swap)" -ForegroundColor Green
            Write-Host "MC dir  : $mcDir"
            if ($backupPresent) {
                $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
                Write-Host "Backup  : $backupDir (from $($manifest.created))"
            } else {
                Write-Host "Backup  : none"
            }
            Write-Host ""
        }
    }
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if ($Help -or (-not $Command)) {
    Show-Help
    exit 0
}

# Normalize paths (WSL → Windows)
if ($McBuildDir)    { $McBuildDir    = ConvertFrom-WslPath $McBuildDir }
if ($ForkOutputDir) { $ForkOutputDir = ConvertFrom-WslPath $ForkOutputDir }

try {
    switch ($Command) {
        'swap' {
            if (-not $ForkOutputDir) { throw "-ForkOutputDir is required for 'swap'." }
            if (-not $McBuildDir)    { throw "-McBuildDir is required for 'swap'." }
            Invoke-Swap -forkDir $ForkOutputDir -mcDir $McBuildDir -force ([bool]$Force)
        }
        'restore' {
            if (-not $McBuildDir) { throw "-McBuildDir is required for 'restore'." }
            Invoke-Restore -mcDir $McBuildDir -cleanBackup ([bool]$CleanBackup)
        }
        'status' {
            if (-not $McBuildDir) { throw "-McBuildDir is required for 'status'." }
            Invoke-Status -mcDir $McBuildDir
        }
    }
} catch {
    if ($Json) {
        @{ error = $_.Exception.Message } | ConvertTo-Json
    } else {
        Write-Fail $_.Exception.Message
    }
    exit 1
}
