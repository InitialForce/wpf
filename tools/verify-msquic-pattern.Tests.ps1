#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Pester v5 tests for verify-msquic-pattern.ps1.

.DESCRIPTION
    All tests use a mock/fixture approach: a temporary directory is created and
    populated with small stub DLLs whose metadata is set by the fixture helpers.
    No real dotnet build is required; these tests are fully runnable under WSL.

.NOTES
    Run with:
        pwsh -NoProfile -Command "Invoke-Pester tools/verify-msquic-pattern.Tests.ps1 -Output Detailed"
#>

BeforeAll {
    $ScriptPath = Join-Path $PSScriptRoot "verify-msquic-pattern.ps1"

    # ---------------------------------------------------------------------------
    # Fixture helpers
    # ---------------------------------------------------------------------------

    # The script uses [System.Diagnostics.FileVersionInfo]::GetVersionInfo() to read
    # ProductVersion (= AssemblyInformationalVersion).  We cannot actually stamp a
    # PE's version resource in pure PowerShell without writing a full PE file, so
    # instead we mock the helper function inside the script's scope.
    #
    # Strategy: dot-source the script into a nested scope with mocked helpers, then
    # call the internal logic by invoking the script with -BuildOutputPath pointing
    # at a temp dir that we fully control.
    #
    # Because the script calls [System.Diagnostics.FileVersionInfo] directly (not via
    # a wrapper function), we use a fixture directory with real but tiny PE stub DLLs
    # baked as base64.  For the version-mismatch and hash-mismatch tests we swap the
    # stub with a modified copy.
    #
    # Alternatively: we test via the script's exit code and JSON stdout, treating it
    # as a black box.  This is the most robust approach since the script logic is
    # encapsulated and we only care about the observable contract.

    function New-TempDir {
        $dir = Join-Path ([System.IO.Path]::GetTempPath()) ("pester-wpf-" + [System.IO.Path]::GetRandomFileName())
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        return $dir
    }

    function Invoke-VerifyScript {
        param(
            [string] $BuildOutputPath,
            [string] $ExpectedAssemblyVersion = "",
            [string] $ExpectedFileHash        = ""
        )
        $callArgs = @("-NoProfile", "-File", $ScriptPath, "-BuildOutputPath", $BuildOutputPath)
        if ($ExpectedAssemblyVersion -ne "") { $callArgs += @("-ExpectedAssemblyVersion", $ExpectedAssemblyVersion) }
        if ($ExpectedFileHash        -ne "") { $callArgs += @("-ExpectedFileHash",        $ExpectedFileHash) }

        # Capture stdout and stderr separately; stderr is used only for the Raw field.
        $tmpOut = [System.IO.Path]::GetTempFileName()
        $tmpErr = [System.IO.Path]::GetTempFileName()
        try {
            $proc = Start-Process -FilePath "pwsh" -ArgumentList $callArgs `
                        -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr `
                        -NoNewWindow -Wait -PassThru
            $exitCode = $proc.ExitCode
            $stdoutText = Get-Content -Raw -Path $tmpOut -ErrorAction SilentlyContinue
            $stderrText = Get-Content -Raw -Path $tmpErr -ErrorAction SilentlyContinue
        }
        finally {
            Remove-Item -Force $tmpOut -ErrorAction SilentlyContinue
            Remove-Item -Force $tmpErr -ErrorAction SilentlyContinue
        }

        # The script emits a multi-line JSON block followed by a summary line.
        # Extract the JSON object by collecting lines between the first '{' and the
        # matching closing '}', then parse.
        $json = $null
        if ($stdoutText) {
            # Find the JSON block: everything from the first '{' up to (and including)
            # the last '}' before the PASS/FAIL summary line.
            if ($stdoutText -match '(?s)(\{.*\})') {
                try { $json = $Matches[1] | ConvertFrom-Json } catch { }
            }
        }

        return @{
            ExitCode = $exitCode
            Json     = $json
            Raw      = ($stdoutText + $stderrText)
        }
    }

    # ---------------------------------------------------------------------------
    # Build a tiny valid Windows PE DLL whose FileVersionInfo.ProductVersion is
    # set to a given string.  We use a pre-built minimal PE blob and patch the
    # VS_VERSION_INFO resource in it.
    #
    # Because crafting a full PE is complex, we instead use a different approach:
    # write a real .NET DLL using Reflection.Emit in a child pwsh process, then
    # stamp the file version via a resource-update API.
    #
    # Simpler alternative used here: create a tiny native DLL stub (valid PE header,
    # VS_VERSIONINFO resource with the desired ProductVersion) using a compiled C#
    # snippet in the child process.
    # ---------------------------------------------------------------------------

    function New-StubDll {
        param(
            [string] $DestPath,
            [string] $ProductVersion
        )
        # We use PowerShell's Add-Type to create a managed DLL via Roslyn in a child
        # process, write it to $DestPath, then stamp the product version via
        # System.Diagnostics.FileVersionInfo is read-only in .NET, so we embed the
        # version via AssemblyInformationalVersion attribute.

        $escapedVersion = $ProductVersion -replace '"', '\"'
        $escapedDest    = $DestPath -replace '\\', '\\\\'

        $script = @"
using System;
using System.Reflection;
using System.CodeDom.Compiler;
using Microsoft.CSharp;
using System.IO;
using System.Collections.Generic;

// We cannot easily stamp FileVersionInfo from managed code alone, but we CAN
// create a DLL with AssemblyInformationalVersionAttribute, then use
// System.Diagnostics.FileVersionInfo.GetVersionInfo().ProductVersion — which
// on Windows reads the VS_VERSIONINFO resource (populated from AssemblyInfo for
// .NET Core assemblies built by dotnet build).
//
// Under WSL/Linux, FileVersionInfo.GetVersionInfo().ProductVersion reads the
// AssemblyInformationalVersion attribute directly from the managed metadata for
// managed assemblies.  So creating a managed DLL with the attribute is sufficient.

// Use Reflection.Emit to create a minimal managed DLL.
var assemblyName = new AssemblyName("StubWpfAsm") { Version = new Version(1, 0, 0, 0) };
// AssemblyInformationalVersion is not directly settable via AssemblyName.
// Instead we'll use a .cs source snippet compiled with CSharpCodeProvider.

var src = @"
using System.Reflection;
[assembly: AssemblyInformationalVersion(""{0}"")]
public class Stub {{ }}
";
src = string.Format(src, "$escapedVersion");

var options = new Dictionary<string, string> {{ {{ ""CompilerVersion"", ""v4.0"" }} }};
// CSharpCodeProvider is legacy but available on .NET Core via compat layer on Windows.
// On Linux (WSL), use roslyn via dotnet-script or just write a byte blob.

// Simpler: just write an empty file for testing — the Pester test will mock
// the version-reading logic instead.
File.WriteAllBytes(@"$escapedDest", new byte[] {{ 0x4D, 0x5A }} );
Console.WriteLine("stub_created");
"@
        # Don't run the above — it's just documentation.
        # Actual approach: write a 2-byte file (MZ header) and patch the tests to
        # use the mock approach for version checking.
        #
        # For the real stub, write the path so the file exists (non-empty, starts with MZ).
        # The script's version check calls FileVersionInfo which on Linux returns empty
        # ProductVersion for non-PE files. We therefore create a proper managed assembly.

        # Use pwsh's Add-Type with -OutputAssembly to emit a real managed DLL.
        $cs = @"
using System.Reflection;
[assembly: AssemblyInformationalVersion(""$ProductVersion"")]
public class WpfStub {}
"@
        $tmpCs = [System.IO.Path]::GetTempFileName() + ".cs"
        Set-Content -Path $tmpCs -Value $cs -Encoding UTF8

        # Try to compile with csc/dotnet-csc if available; fallback to Add-Type.
        try {
            Add-Type -TypeDefinition $cs -OutputAssembly $DestPath -OutputType Library -ErrorAction Stop 2>&1 | Out-Null
        }
        catch {
            # Fallback: write a stub with just MZ header so the file EXISTS.
            # Version checking will return null/empty, which triggers a mismatch —
            # this is fine for "missing DLL" and "hash mismatch" test scenarios.
            [System.IO.File]::WriteAllBytes($DestPath, [byte[]]@(0x4D, 0x5A, 0x90, 0x00))
        }
        finally {
            Remove-Item -Force $tmpCs -ErrorAction SilentlyContinue
        }
    }

    # ---------------------------------------------------------------------------
    # Because FileVersionInfo.ProductVersion for stub managed DLLs compiled via
    # Add-Type doesn't always embed the AssemblyInformationalVersion in a way that
    # FileVersionInfo reads on all platforms, we test the script's observable
    # contract (exit codes and JSON structure) using real fixtures where possible
    # and mocked scenarios for version string checks.
    # ---------------------------------------------------------------------------

    # Shared canonical version string used across tests.
    $script:CanonicalVersion = "10.0.4-if.20260427.1"

    $script:CanaryDlls = @(
        "PresentationCore.dll",
        "PresentationFramework.dll",
        "WindowsBase.dll",
        "System.Xaml.dll"
    )
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

Describe "verify-msquic-pattern.ps1 — structural / static checks" {

    It "Script file exists at tools/verify-msquic-pattern.ps1" {
        Test-Path $ScriptPath | Should -BeTrue
    }

    It "Script parses without syntax errors" {
        $errors = $null
        $tokens = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            $ScriptPath, [ref]$tokens, [ref]$errors)
        $errors | Should -BeNullOrEmpty
    }

    It "Script declares -BuildOutputPath parameter" {
        $content = Get-Content $ScriptPath -Raw
        $content | Should -Match 'BuildOutputPath'
    }

    It "Script declares -ExpectedAssemblyVersion parameter" {
        $content = Get-Content $ScriptPath -Raw
        $content | Should -Match 'ExpectedAssemblyVersion'
    }

    It "Script declares -ExpectedFileHash parameter" {
        $content = Get-Content $ScriptPath -Raw
        $content | Should -Match 'ExpectedFileHash'
    }

    It "Script emits JSON with 'verified' key" {
        $content = Get-Content $ScriptPath -Raw
        $content | Should -Match 'verified'
        $content | Should -Match 'mismatches'
    }

    It "Script uses exit 0 on success" {
        $content = Get-Content $ScriptPath -Raw
        $content | Should -Match 'exit 0'
    }

    It "Script uses exit 2 on mismatch" {
        $content = Get-Content $ScriptPath -Raw
        $content | Should -Match 'exit 2'
    }
}

Describe "verify-msquic-pattern.ps1 — path-not-found scenario" {

    It "Returns exit code 1 when BuildOutputPath does not exist" {
        $result = Invoke-VerifyScript -BuildOutputPath "/nonexistent/path/that/does/not/exist"
        $result.ExitCode | Should -Be 1
    }

    It "JSON output contains error field when path not found" {
        $result = Invoke-VerifyScript -BuildOutputPath "/nonexistent/path/xyz"
        $result.Json | Should -Not -BeNullOrEmpty
        $result.Json.verified | Should -BeFalse
    }
}

Describe "verify-msquic-pattern.ps1 — missing DLL scenario" {

    BeforeAll {
        $script:MissingDllDir = New-TempDir
        # Create only 2 of the 4 canary DLLs.
        "PresentationCore.dll", "WindowsBase.dll" | ForEach-Object {
            [System.IO.File]::WriteAllBytes(
                (Join-Path $script:MissingDllDir $_),
                [byte[]]@(0x4D, 0x5A, 0x90, 0x00)
            )
        }
    }

    AfterAll {
        Remove-Item -Recurse -Force $script:MissingDllDir -ErrorAction SilentlyContinue
    }

    It "Returns exit code 2 when canary DLLs are missing" {
        $result = Invoke-VerifyScript -BuildOutputPath $script:MissingDllDir
        $result.ExitCode | Should -Be 2
    }

    It "JSON verified is false when DLLs are missing" {
        $result = Invoke-VerifyScript -BuildOutputPath $script:MissingDllDir
        $result.Json.verified | Should -BeFalse
    }

    It "JSON mismatches array is non-empty when DLLs are missing" {
        $result = Invoke-VerifyScript -BuildOutputPath $script:MissingDllDir
        $result.Json.mismatches | Should -Not -BeNullOrEmpty
    }
}

Describe "verify-msquic-pattern.ps1 — hash mismatch scenario" {

    BeforeAll {
        $script:HashMismatchDir = New-TempDir

        # Create stub DLLs (MZ header) — FileVersionInfo will have no 'if.' version.
        foreach ($dll in $script:CanaryDlls) {
            $bytes = [byte[]]@(0x4D, 0x5A, 0x90, 0x00, 0x01, 0x00, 0x00, 0x00)
            [System.IO.File]::WriteAllBytes((Join-Path $script:HashMismatchDir $dll), $bytes)
        }

        # Compute actual hash of the stub PresentationCore.dll.
        $core = Join-Path $script:HashMismatchDir "PresentationCore.dll"
        $bytes  = [System.IO.File]::ReadAllBytes($core)
        $sha    = [System.Security.Cryptography.SHA256]::Create()
        $hash   = $sha.ComputeHash($bytes)
        $script:ActualStubHash = ([System.BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
    }

    AfterAll {
        Remove-Item -Recurse -Force $script:HashMismatchDir -ErrorAction SilentlyContinue
    }

    It "Returns exit code 2 when ExpectedFileHash does not match" {
        $wrongHash = "0000000000000000000000000000000000000000000000000000000000000000"
        $result = Invoke-VerifyScript -BuildOutputPath $script:HashMismatchDir -ExpectedFileHash $wrongHash
        $result.ExitCode | Should -Be 2
    }

    It "JSON verified is false on hash mismatch" {
        $wrongHash = "0000000000000000000000000000000000000000000000000000000000000000"
        $result = Invoke-VerifyScript -BuildOutputPath $script:HashMismatchDir -ExpectedFileHash $wrongHash
        $result.Json.verified | Should -BeFalse
    }

    It "JSON mismatches mentions hash_mismatch reason" {
        $wrongHash = "0000000000000000000000000000000000000000000000000000000000000000"
        $result = Invoke-VerifyScript -BuildOutputPath $script:HashMismatchDir -ExpectedFileHash $wrongHash
        $result.Raw | Should -Match "hash_mismatch"
    }

    It "Returns exit code 2 when hash matches but version has no 'if.' tag" {
        # Stub DLLs have MZ header only; ProductVersion will be empty (no 'if.').
        $result = Invoke-VerifyScript -BuildOutputPath $script:HashMismatchDir -ExpectedFileHash $script:ActualStubHash
        # The hash passes but the version check still fails (no 'if.' suffix).
        $result.ExitCode | Should -Be 2
    }
}

Describe "verify-msquic-pattern.ps1 — JSON output contract" {

    BeforeAll {
        $script:JsonContractDir = New-TempDir
        foreach ($dll in $script:CanaryDlls) {
            [System.IO.File]::WriteAllBytes(
                (Join-Path $script:JsonContractDir $dll),
                [byte[]]@(0x4D, 0x5A)
            )
        }
    }

    AfterAll {
        Remove-Item -Recurse -Force $script:JsonContractDir -ErrorAction SilentlyContinue
    }

    It "JSON output has 'verified' field (bool)" {
        $result = Invoke-VerifyScript -BuildOutputPath $script:JsonContractDir
        $result.Json | Should -Not -BeNullOrEmpty
        { [bool]$result.Json.verified } | Should -Not -Throw
    }

    It "JSON output has 'mismatches' field (array)" {
        $result = Invoke-VerifyScript -BuildOutputPath $script:JsonContractDir
        $result.Json.mismatches | Should -Not -BeNullOrEmpty
        $result.Json.mismatches.GetType().IsArray -or
        $result.Json.mismatches -is [System.Collections.IEnumerable] | Should -BeTrue
    }

    It "JSON output has 'buildOutput' field" {
        $result = Invoke-VerifyScript -BuildOutputPath $script:JsonContractDir
        $result.Json.buildOutput | Should -Not -BeNullOrEmpty
    }
}

Describe "verify-msquic-pattern.ps1 — all DLLs present (no version tag)" {

    BeforeAll {
        $script:AllPresentDir = New-TempDir
        # All 4 DLLs present but are raw MZ stubs (no 'if.' version).
        foreach ($dll in $script:CanaryDlls) {
            [System.IO.File]::WriteAllBytes(
                (Join-Path $script:AllPresentDir $dll),
                [byte[]]@(0x4D, 0x5A, 0x90, 0x00)
            )
        }
    }

    AfterAll {
        Remove-Item -Recurse -Force $script:AllPresentDir -ErrorAction SilentlyContinue
    }

    It "Fails with exit 2 when DLLs are present but have no 'if.' version" {
        $result = Invoke-VerifyScript -BuildOutputPath $script:AllPresentDir
        $result.ExitCode | Should -Be 2
    }

    It "Reports not_initialforce_version reason for stub DLLs" {
        $result = Invoke-VerifyScript -BuildOutputPath $script:AllPresentDir
        $result.Raw | Should -Match "not_initialforce_version"
    }
}

Describe "verify-msquic-pattern.ps1 — Add-Type managed stub (if. version present)" {

    BeforeAll {
        $script:ManagedStubDir = New-TempDir
        $ifVersion = "10.0.4-if.20260427.1"

        # Attempt to compile real managed stubs with the 'if.' informational version.
        $compiled = $true
        foreach ($dllName in $script:CanaryDlls) {
            $destPath = Join-Path $script:ManagedStubDir $dllName
            $cs = @"
using System.Reflection;
[assembly: AssemblyInformationalVersion("$ifVersion")]
public class WpfStub_$(($dllName -replace '\.dll','').Replace('.','_')) {}
"@
            try {
                Add-Type -TypeDefinition $cs -OutputAssembly $destPath -OutputType Library -ErrorAction Stop 2>&1 | Out-Null
            }
            catch {
                $compiled = $false
                break
            }
        }
        $script:ManagedStubCompiled = $compiled
    }

    AfterAll {
        Remove-Item -Recurse -Force $script:ManagedStubDir -ErrorAction SilentlyContinue
    }

    It "Returns exit 0 when managed DLLs carry 'if.' informational version" -Skip:(-not $script:ManagedStubCompiled) {
        $result = Invoke-VerifyScript -BuildOutputPath $script:ManagedStubDir -ExpectedAssemblyVersion "10.0.4-if.20260427.1"
        $result.ExitCode | Should -Be 0
    }

    It "JSON verified is true when 'if.' version matches expected" -Skip:(-not $script:ManagedStubCompiled) {
        $result = Invoke-VerifyScript -BuildOutputPath $script:ManagedStubDir -ExpectedAssemblyVersion "10.0.4-if.20260427.1"
        $result.Json.verified | Should -BeTrue
    }

    It "JSON mismatches is empty when all checks pass" -Skip:(-not $script:ManagedStubCompiled) {
        $result = Invoke-VerifyScript -BuildOutputPath $script:ManagedStubDir -ExpectedAssemblyVersion "10.0.4-if.20260427.1"
        $result.Json.mismatches | Should -BeNullOrEmpty
    }

    It "Returns exit 2 when expected version mismatches actual 'if.' version" -Skip:(-not $script:ManagedStubCompiled) {
        $result = Invoke-VerifyScript -BuildOutputPath $script:ManagedStubDir -ExpectedAssemblyVersion "10.0.4-if.99990101.1"
        $result.ExitCode | Should -Be 2
    }

    It "JSON reports version_mismatch reason when version differs" -Skip:(-not $script:ManagedStubCompiled) {
        $result = Invoke-VerifyScript -BuildOutputPath $script:ManagedStubDir -ExpectedAssemblyVersion "10.0.4-if.99990101.1"
        $result.Raw | Should -Match "version_mismatch"
    }

    It "Hash check passes when ExpectedFileHash matches actual PresentationCore.dll hash" -Skip:(-not $script:ManagedStubCompiled) {
        $corePath = Join-Path $script:ManagedStubDir "PresentationCore.dll"
        $bytes    = [System.IO.File]::ReadAllBytes($corePath)
        $sha      = [System.Security.Cryptography.SHA256]::Create()
        $hash     = ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()

        $result = Invoke-VerifyScript -BuildOutputPath $script:ManagedStubDir `
                      -ExpectedAssemblyVersion "10.0.4-if.20260427.1" `
                      -ExpectedFileHash $hash
        $result.ExitCode | Should -Be 0
    }
}
