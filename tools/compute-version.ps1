<#
.SYNOPSIS
    Derive the next NuGet version from the latest if-10.0.*-perf.* git tag.

.DESCRIPTION
    Reads git tags matching 'if-10.0.*-perf.*', finds the most recent one,
    and computes the next NuGet version string using the scheme:
        {upstream-version}-if.{yyyymmdd}.{rev}
    e.g. tag 'if-10.0.4-perf.20260427' on its first publish day -> '10.0.4-if.20260427.2'
    A same-day re-publish uses one tag per round; rev = count of same-day tags + 1.

.PARAMETER DryRun
    When set, prints the computed version but does NOT create a new git tag.

.PARAMETER SimulatedDate
    Override today's date for testing. Accepts 'yyyy-MM-dd' or 'yyyyMMdd'.

.PARAMETER GitDir
    Path to the git repository root. Defaults to the repo containing this script.

.OUTPUTS
    A single line to stdout: the next NuGet version string.

.EXAMPLE
    pwsh -File tools/compute-version.ps1 -DryRun -SimulatedDate 2026-04-27

.EXAMPLE
    pwsh -File tools/compute-version.ps1
#>

[CmdletBinding()]
param(
    [switch] $DryRun,

    [ValidatePattern('^\d{4}-?\d{2}-?\d{2}$')]
    [string] $SimulatedDate,

    [string] $GitDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Get-TodayString {
    param([string] $Simulated)
    if ($Simulated) {
        # Normalise: strip hyphens; accepts yyyy-mm-dd or yyyymmdd
        return $Simulated -replace '-', ''
    }
    return (Get-Date -Format 'yyyyMMdd')
}

function Invoke-GitCommand {
    param(
        [string[]] $GitArgs,
        [string]   $WorkDir
    )
    $fullArgs = [System.Collections.Generic.List[string]]::new()
    if ($WorkDir) {
        $fullArgs.Add('-C')
        $fullArgs.Add($WorkDir)
    }
    foreach ($a in $GitArgs) { $fullArgs.Add($a) }

    $output = & git @fullArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE): $output"
    }
    return $output
}

function Get-MatchingTag {
    param([string] $WorkDir)
    $allTags = Invoke-GitCommand -GitArgs @('tag', '--list') -WorkDir $WorkDir
    if (-not $allTags) {
        return @()
    }
    # Pattern: if-<upstream-version>-perf.<yyyymmdd>
    # e.g. if-10.0.4-perf.20260427
    $pattern = '^if-\d+\.\d+\.\d+-perf\.\d{8}$'
    $matched = @($allTags | Where-Object { $_ -match $pattern })
    return $matched
}

function ConvertTo-ParsedTag {
    param([string] $Tag)
    $pattern = '^if-(\d+\.\d+\.\d+)-perf\.(\d{8})$'
    if ($Tag -match $pattern) {
        return [PSCustomObject]@{
            UpstreamVersion = $Matches[1]
            DateString      = $Matches[2]
            Raw             = $Tag
        }
    }
    return $null
}

function Get-LatestParsedTag {
    param([string[]] $Tags)
    $parsed = @($Tags | ForEach-Object { ConvertTo-ParsedTag -Tag $_ } | Where-Object { $_ })
    if (-not $parsed -or $parsed.Count -eq 0) {
        return $null
    }
    # Sort: date desc, then upstream version desc (zero-pad components for lexical sort)
    $sorted = $parsed | Sort-Object {
        $ver = $_.UpstreamVersion -split '\.'
        [string]::Format('{0}|{1:D6}.{2:D6}.{3:D6}', $_.DateString,
            [int]$ver[0], [int]$ver[1], [int]$ver[2])
    } -Descending
    return $sorted[0]
}

function Get-NextNuGetVersion {
    param(
        [string] $TodayString,
        [string] $WorkDir
    )

    $tags   = Get-MatchingTag -WorkDir $WorkDir
    $latest = Get-LatestParsedTag -Tags $tags

    if (-not $latest) {
        throw ('No if-10.0.*-perf.* tags found in the repository. ' +
               'Create an initial tag first, e.g.: ' +
               "git tag if-10.0.0-perf.$TodayString")
    }

    $upstreamVersion = $latest.UpstreamVersion
    $latestDate      = $latest.DateString

    if ($latestDate -eq $TodayString) {
        # Same day: count all same-day tags for this upstream version; rev = count + 1
        $todayCount = @($tags | Where-Object {
            $p = ConvertTo-ParsedTag -Tag $_
            $p -and ($p.UpstreamVersion -eq $upstreamVersion) -and ($p.DateString -eq $TodayString)
        }).Count
        $rev = $todayCount + 1
    }
    else {
        # New day (or future simulated date): start at rev 1
        $rev = 1
    }

    return "$upstreamVersion-if.$TodayString.$rev"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if (-not $GitDir) {
    # Resolve git root from the script's directory
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    try {
        $gitRoot = (Invoke-GitCommand -GitArgs @('rev-parse', '--show-toplevel') -WorkDir $scriptDir) -join ''
        $GitDir  = $gitRoot.Trim()
    }
    catch {
        $GitDir = $scriptDir
    }
}

$today   = Get-TodayString -Simulated $SimulatedDate
$version = Get-NextNuGetVersion -TodayString $today -WorkDir $GitDir

Write-Output $version

if ($DryRun) {
    Write-Information "[DryRun] Next NuGet version: $version (no tag created)" -InformationAction Continue
}
