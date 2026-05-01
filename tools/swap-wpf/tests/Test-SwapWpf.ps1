# Smoke tests for swap-wpf.ps1
# Exercises swap -> status -> restore round-trip with fake DLL files.
# Does NOT require a real WPF fork or MC build.
#
# Creates two temporary directories (FakeForge and FakeMC) populated with
# dummy DLL/PDB files, then drives swap-wpf.ps1 through the full lifecycle.
# Verifies md5 checks, backup creation, applied-marker creation, and restore.
#
# Run from a PowerShell prompt (no elevation required):
#   pwsh -ExecutionPolicy Bypass .\tests\Test-SwapWpf.ps1
#
# Exit code 0 = all tests passed. Non-zero = at least one failure.
# Uses a lightweight inline test runner (no Pester dependency required).

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── locate the script under test ───────────────────────────────────────────
$ScriptDir     = Split-Path -Parent $PSScriptRoot
$ScriptUnderTest = Join-Path $ScriptDir 'swap-wpf.ps1'

if (-not (Test-Path $ScriptUnderTest)) {
    throw "swap-wpf.ps1 not found at: $ScriptUnderTest"
}

# ── minimal inline test runner ─────────────────────────────────────────────
$script:PassCount = 0
$script:FailCount = 0
$script:Results   = [System.Collections.Generic.List[hashtable]]::new()

function It {
    param([string]$Name, [scriptblock]$Test)
    try {
        & $Test
        $script:PassCount++
        Write-Host "  [pass] $Name" -ForegroundColor Green
        $script:Results.Add(@{ name = $Name; status = 'pass' })
    } catch {
        $script:FailCount++
        Write-Host "  [FAIL] $Name" -ForegroundColor Red
        Write-Host "         $_" -ForegroundColor DarkRed
        $script:Results.Add(@{ name = $Name; status = 'fail'; error = "$_" })
    }
}

function Assert-True  ([bool]$val, [string]$msg = 'Expected true') { if (-not $val) { throw $msg } }
function Assert-False ([bool]$val, [string]$msg = 'Expected false') { if ($val)     { throw $msg } }
function Assert-Equal ([object]$a, [object]$b, [string]$msg = "Expected '$b' == '$a'") {
    if ($a -ne $b) { throw "$msg  (got '$a')" }
}
function Assert-Contains ([string]$haystack, [string]$needle) {
    if ($haystack -notmatch [regex]::Escape($needle)) {
        throw "Expected '$haystack' to contain '$needle'"
    }
}

# ── helper: invoke swap-wpf.ps1 ────────────────────────────────────────────
function Invoke-SwapWpf {
    param([string[]]$ScriptArgs)
    $out = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
        -File $ScriptUnderTest @ScriptArgs 2>&1
    return @{
        Output   = ($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
        Errors   = ($out | Where-Object { $_ -is  [System.Management.Automation.ErrorRecord] }) -join "`n"
        ExitCode = $LASTEXITCODE
    }
}

# ── fixture helpers ─────────────────────────────────────────────────────────
$REQUIRED_DLL_NAMES = @(
    'PresentationCore.dll',
    'PresentationFramework.dll',
    'System.Xaml.dll',
    'WindowsBase.dll'
)
$OPTIONAL_PDB_NAMES = @(
    'PresentationCore.pdb',
    'PresentationFramework.pdb',
    'System.Xaml.pdb',
    'WindowsBase.pdb'
)
$ALL_NAMES = $REQUIRED_DLL_NAMES + $OPTIONAL_PDB_NAMES

function New-FakeDir {
    param([string]$Prefix, [string[]]$FileNames, [string]$Content = 'FAKE_DLL_CONTENT')
    $dir = Join-Path ([System.IO.Path]::GetTempPath()) "${Prefix}_$([System.IO.Path]::GetRandomFileName())"
    New-Item -ItemType Directory -Path $dir | Out-Null
    foreach ($name in $FileNames) {
        Set-Content -Path (Join-Path $dir $name) -Value "$Content $name" -Encoding UTF8 -NoNewline
    }
    return $dir
}

# ── test suite ──────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "swap-wpf smoke tests" -ForegroundColor Cyan
Write-Host ("=" * 50)
Write-Host ""

# ── T01: help / no-command ──────────────────────────────────────────────────
Write-Host "Group: help / invocation" -ForegroundColor Yellow
It 'T01 - shows help when no command given' {
    $r = Invoke-SwapWpf @()
    Assert-Equal $r.ExitCode 0 'exit code'
    Assert-Contains $r.Output 'COMMANDS'
    Assert-Contains $r.Output 'swap'
    Assert-Contains $r.Output 'restore'
}

It 'T02 - -Help flag shows usage' {
    $r = Invoke-SwapWpf @('-Help')
    Assert-Equal $r.ExitCode 0 'exit code'
    Assert-Contains $r.Output 'COMMANDS'
}

# ── T10: swap ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Group: swap" -ForegroundColor Yellow

$forkDir = $null
$mcDir   = $null

It 'T10 - swap succeeds with all DLLs present' {
    $script:forkDir = New-FakeDir 'fake_fork' $ALL_NAMES 'FORK_CONTENT'
    $script:mcDir   = New-FakeDir 'fake_mc'   $ALL_NAMES 'ORIGINAL_CONTENT'

    $r = Invoke-SwapWpf @('swap', '-ForkOutputDir', $script:forkDir, '-McBuildDir', $script:mcDir)
    Assert-Equal $r.ExitCode 0 "exit code (output: $($r.Output))"
    # applied marker must exist
    Assert-True (Test-Path (Join-Path $script:mcDir 'swap-wpf-applied.json')) 'applied marker created'
    # backup must exist
    Assert-True (Test-Path (Join-Path $script:mcDir 'swap-wpf-backup\manifest.json')) 'manifest created'
}

It 'T11 - after swap, target files contain fork content' {
    foreach ($name in $REQUIRED_DLL_NAMES) {
        $content = Get-Content (Join-Path $script:mcDir $name) -Raw
        Assert-Contains $content 'FORK_CONTENT'
    }
}

It 'T12 - backup files contain original content' {
    foreach ($name in $REQUIRED_DLL_NAMES) {
        $backupPath = Join-Path $script:mcDir "swap-wpf-backup\$name"
        Assert-True (Test-Path $backupPath) "backup exists: $name"
        $content = Get-Content $backupPath -Raw
        Assert-Contains $content 'ORIGINAL_CONTENT'
    }
}

It 'T13 - swap refuses second swap without -Force' {
    $r = Invoke-SwapWpf @('swap', '-ForkOutputDir', $script:forkDir, '-McBuildDir', $script:mcDir)
    Assert-Equal $r.ExitCode 1 'should exit non-zero'
    Assert-Contains ($r.Output + $r.Errors) 'already active'
}

It 'T14 - swap with -Force overwrites the active swap' {
    $fork2 = New-FakeDir 'fake_fork2' $ALL_NAMES 'FORK2_CONTENT'
    $r = Invoke-SwapWpf @('swap', '-ForkOutputDir', $fork2, '-McBuildDir', $script:mcDir, '-Force')
    Assert-Equal $r.ExitCode 0 "exit code (output: $($r.Output))"
    foreach ($name in $REQUIRED_DLL_NAMES) {
        $content = Get-Content (Join-Path $script:mcDir $name) -Raw
        Assert-Contains $content 'FORK2_CONTENT'
    }
    Remove-Item $fork2 -Recurse -Force
}

It 'T15 - swap fails gracefully when a required DLL is missing from fork dir' {
    $incompleteDir = New-FakeDir 'fake_incomplete' @('PresentationCore.dll') 'PARTIAL'
    $freshMcDir    = New-FakeDir 'fake_mc_clean'   $ALL_NAMES 'ORIGINAL'
    $r = Invoke-SwapWpf @('swap', '-ForkOutputDir', $incompleteDir, '-McBuildDir', $freshMcDir)
    Assert-Equal $r.ExitCode 1 'should fail when DLL missing'
    Remove-Item $incompleteDir -Recurse -Force
    Remove-Item $freshMcDir -Recurse -Force
}

# ── T20: status ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Group: status" -ForegroundColor Yellow

It 'T20 - status while swapped shows SWAPPED' {
    $r = Invoke-SwapWpf @('status', '-McBuildDir', $script:mcDir)
    Assert-Equal $r.ExitCode 0 'exit code'
    Assert-Contains $r.Output 'SWAPPED'
}

It 'T21 - status -Json returns valid JSON while swapped' {
    $r = Invoke-SwapWpf @('status', '-McBuildDir', $script:mcDir, '-Json')
    Assert-Equal $r.ExitCode 0 'exit code'
    $json = $r.Output | ConvertFrom-Json
    Assert-Equal $json.state 'swapped' 'json.state'
}

# ── T30: restore ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Group: restore" -ForegroundColor Yellow

It 'T30 - restore succeeds' {
    $r = Invoke-SwapWpf @('restore', '-McBuildDir', $script:mcDir)
    Assert-Equal $r.ExitCode 0 "exit code (output: $($r.Output))"
    # applied marker must be gone
    Assert-False (Test-Path (Join-Path $script:mcDir 'swap-wpf-applied.json')) 'applied marker removed'
}

It 'T31 - after restore, target files contain backed-up content (not the last-swapped FORK2)' {
    # T14 forced a swap with FORK2_CONTENT and refreshed backup from FORK_CONTENT.
    # Restore should return FORK_CONTENT (pre-T14 state), NOT FORK2_CONTENT.
    foreach ($name in $REQUIRED_DLL_NAMES) {
        $content = Get-Content (Join-Path $script:mcDir $name) -Raw
        # Must NOT still have the FORK2 content (confirms restore happened)
        if ($content -match 'FORK2_CONTENT') {
            throw "Expected restore to revert from FORK2_CONTENT but $name still has FORK2"
        }
        # Should have FORK_CONTENT (what was in MC when T14's -Force backup was taken)
        Assert-Contains $content 'FORK_CONTENT'
    }
}

It 'T32 - status after restore shows ORIGINAL' {
    $r = Invoke-SwapWpf @('status', '-McBuildDir', $script:mcDir)
    Assert-Equal $r.ExitCode 0 'exit code'
    Assert-Contains $r.Output 'ORIGINAL'
}

It 'T33 - status -Json returns valid JSON in original state' {
    $r = Invoke-SwapWpf @('status', '-McBuildDir', $script:mcDir, '-Json')
    Assert-Equal $r.ExitCode 0 'exit code'
    $json = $r.Output | ConvertFrom-Json
    Assert-Equal $json.state 'original' 'json.state'
}

It 'T34 - restore with -CleanBackup removes backup dir' {
    # swap again so we have an applied state to restore from
    $r1 = Invoke-SwapWpf @('swap', '-ForkOutputDir', $script:forkDir, '-McBuildDir', $script:mcDir)
    Assert-Equal $r1.ExitCode 0 're-swap exit code'
    $r2 = Invoke-SwapWpf @('restore', '-McBuildDir', $script:mcDir, '-CleanBackup')
    Assert-Equal $r2.ExitCode 0 'restore exit code'
    Assert-False (Test-Path (Join-Path $script:mcDir 'swap-wpf-backup')) 'backup dir removed'
}

It 'T35 - restore fails when backup missing' {
    # mcDir has no backup now (was cleaned)
    $r = Invoke-SwapWpf @('restore', '-McBuildDir', $script:mcDir)
    Assert-Equal $r.ExitCode 1 'should fail'
}

# ── T40: native DLL passthrough ─────────────────────────────────────────────
Write-Host ""
Write-Host "Group: native DLL" -ForegroundColor Yellow

It 'T40 - native PresentationNative_*.dll is included when present' {
    $nativeMcDir   = New-FakeDir 'fake_mc_native'   ($ALL_NAMES + @('PresentationNative_v0400.dll')) 'ORIG'
    $nativeForkDir = New-FakeDir 'fake_fork_native'  ($ALL_NAMES + @('PresentationNative_v0400.dll')) 'FORK'
    $r = Invoke-SwapWpf @('swap', '-ForkOutputDir', $nativeForkDir, '-McBuildDir', $nativeMcDir)
    Assert-Equal $r.ExitCode 0 "exit code (output: $($r.Output))"
    $nativeContent = Get-Content (Join-Path $nativeMcDir 'PresentationNative_v0400.dll') -Raw
    Assert-Contains $nativeContent 'FORK'
    Remove-Item $nativeMcDir   -Recurse -Force
    Remove-Item $nativeForkDir -Recurse -Force
}

# ── cleanup ──────────────────────────────────────────────────────────────────
if ($forkDir -and (Test-Path $forkDir)) { Remove-Item $forkDir -Recurse -Force }
if ($mcDir   -and (Test-Path $mcDir))   { Remove-Item $mcDir   -Recurse -Force }

# ── summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host ("=" * 50)
Write-Host "Results: $script:PassCount passed, $script:FailCount failed" -ForegroundColor $(if ($script:FailCount -eq 0) { 'Green' } else { 'Red' })
Write-Host ""

exit $(if ($script:FailCount -eq 0) { 0 } else { 1 })
