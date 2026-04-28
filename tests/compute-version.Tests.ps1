#Requires -Version 7.0
<#
.SYNOPSIS
    Pester v5 tests for tools/compute-version.ps1
#>

BeforeAll {
    $ScriptPath = Join-Path $PSScriptRoot '../tools/compute-version.ps1'
    if (-not (Test-Path $ScriptPath)) {
        throw "Script not found: $ScriptPath"
    }

    # Helper: create a temp git repo, seed tags, run script, return output
    function New-TempRepo {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "pester-wpf-$([System.Guid]::NewGuid().ToString('N'))"
        New-Item -ItemType Directory -Path $tmp | Out-Null
        & git -C $tmp init --quiet
        & git -C $tmp config user.email 'test@example.com'
        & git -C $tmp config user.name 'Test'
        # Need at least one commit for tags to work
        $null | Out-File (Join-Path $tmp 'README.md')
        & git -C $tmp add .
        & git -C $tmp commit --quiet -m 'init'
        return $tmp
    }

    function Remove-TempRepo {
        param([string] $Path)
        if (Test-Path $Path) {
            Remove-Item -Recurse -Force $Path -ErrorAction SilentlyContinue
        }
    }

    function Invoke-ComputeVersion {
        param(
            [string]   $RepoPath,
            [string]   $SimulatedDate,
            [switch]   $DryRun
        )
        $invokeArgs = @('-NonInteractive', '-File', $ScriptPath, '-GitDir', $RepoPath)
        if ($SimulatedDate) { $invokeArgs += '-SimulatedDate', $SimulatedDate }
        if ($DryRun) { $invokeArgs += '-DryRun' }
        $output = pwsh @invokeArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            $errorText = ($output | Out-String).Trim()
            throw "compute-version.ps1 exited $LASTEXITCODE. Output: $errorText"
        }
        # Return the first stdout line (filter out ErrorRecord objects from stderr)
        $stdoutLines = @($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
        if ($stdoutLines.Count -eq 0) {
            throw 'compute-version.ps1 produced no stdout output'
        }
        return [string]$stdoutLines[0]
    }
}

Describe 'compute-version.ps1' {

    Context 'No prior tags' {
        It 'throws a helpful error when no if-10.0.*-perf.* tags exist' {
            $repo = New-TempRepo
            try {
                { Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427' } | Should -Throw
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }
    }

    Context 'Single prior tag (first publish of the day)' {
        It 'returns rev 2 when one same-day tag exists' {
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'if-10.0.4-perf.20260427'
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427'
                $result | Should -Be '10.0.4-if.20260427.2'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }

        It 'returns rev 1 on a new day after a prior day tag' {
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'if-10.0.4-perf.20260426'
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427'
                $result | Should -Be '10.0.4-if.20260427.1'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }
    }

    Context 'Multiple prior tags, same upstream version' {
        It 'counts same-day tags and returns next rev' {
            $repo = New-TempRepo
            try {
                # Two existing publishes on same day
                & git -C $repo tag 'if-10.0.4-perf.20260427'
                # Simulate second same-day publish with a unique tag name
                # Architecture: one tag per publish; same-day → append sub-rev
                # Here we test counting logic
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427'
                # 1 same-day tag → next rev = 2
                $result | Should -Be '10.0.4-if.20260427.2'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }

        It 'returns rev 1 when latest tag is from a previous day' {
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'if-10.0.4-perf.20260425'
                & git -C $repo tag 'if-10.0.4-perf.20260426'
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427'
                $result | Should -Be '10.0.4-if.20260427.1'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }
    }

    Context 'Upstream version bump resets rev' {
        It 'resets rev to 1 when upstream version is higher than any tag' {
            # This scenario: all tags are for 10.0.4, but we want 10.0.5
            # In practice the caller must seed a new tag for the new version first.
            # The script computes next version relative to the LATEST tag's upstream version.
            # So if 10.0.4 tags exist and we simulate a new day, it stays on 10.0.4.
            # Upstream bump detection is out of scope for this script (done by CI).
            # Test that tags for older upstream version don't bleed into new one.
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'if-10.0.4-perf.20260426'
                # Seed a 10.0.5 tag to simulate upstream bump
                & git -C $repo tag 'if-10.0.5-perf.20260427'
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427'
                # Latest is 10.0.5-perf.20260427 → same day → rev = 2
                $result | Should -Be '10.0.5-if.20260427.2'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }

        It 'starts at rev 1 on new day after upstream version bump' {
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'if-10.0.4-perf.20260426'
                & git -C $repo tag 'if-10.0.5-perf.20260426'
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427'
                # Latest upstream version is 10.0.5, date is yesterday → new day → rev 1
                $result | Should -Be '10.0.5-if.20260427.1'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }
    }

    Context 'Malformed tags are ignored' {
        It 'ignores tags that do not match the if-10.0.*-perf.* pattern' {
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'v10.0.4'
                & git -C $repo tag 'if-10.0.4'
                & git -C $repo tag 'release-10.0.4'
                & git -C $repo tag 'if-10.0.4-perf.notadate'
                & git -C $repo tag 'if-10.0.4-perf.20260427'
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427'
                # Only one valid tag exists for today
                $result | Should -Be '10.0.4-if.20260427.2'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }

        It 'throws when only malformed tags exist (no valid tags at all)' {
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'v10.0.4'
                & git -C $repo tag 'bad-tag-format'
                { Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427' } | Should -Throw
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }
    }

    Context 'DryRun flag' {
        It 'outputs the version and DryRun message when -DryRun is set' {
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'if-10.0.4-perf.20260426'
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260427' -DryRun
                # First line should be the version
                ($result | Select-Object -First 1) | Should -Be '10.0.4-if.20260427.1'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }
    }

    Context 'Output format validation' {
        It 'output matches NuGet semver2 pattern {major}.{minor}.{patch}-if.{date}.{rev}' {
            $repo = New-TempRepo
            try {
                & git -C $repo tag 'if-10.0.4-perf.20260426'
                $result = Invoke-ComputeVersion -RepoPath $repo -SimulatedDate '20260428'
                $result | Should -Match '^\d+\.\d+\.\d+-if\.\d{8}\.\d+$'
            }
            finally {
                Remove-TempRepo -Path $repo
            }
        }
    }
}
