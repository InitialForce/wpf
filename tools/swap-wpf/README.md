# swap-wpf

Swap patched WPF DLLs from a fork build into MotionCatalyst's `BUILD/x64_Release`
directory, with md5 verification and full backup/restore support.

## Managed files

| DLL | PDB | Notes |
|-----|-----|-------|
| `PresentationCore.dll` | `PresentationCore.pdb` | required |
| `PresentationFramework.dll` | `PresentationFramework.pdb` | required |
| `System.Xaml.dll` | `System.Xaml.pdb` | required |
| `WindowsBase.dll` | `WindowsBase.pdb` | required |
| `PresentationNative_*.dll` | `PresentationNative_*.pdb` | optional — included if present in fork dir |

Only these files are touched. Nothing else in the MC build dir is modified.

## Commands

### `swap` — install fork DLLs

```powershell
swap-wpf.ps1 swap -ForkOutputDir <path> -McBuildDir <path> [-Force] [-Json]
```

1. Validates all 4 required DLLs exist in `ForkOutputDir`.
2. Backs up the current DLLs in `McBuildDir` to `<McBuildDir>\swap-wpf-backup\`
   (skipped if a backup already exists, unless `-Force`).
3. Copies fork DLLs (and PDBs, and native DLLs if present) into `McBuildDir`.
4. Verifies MD5 of each file post-copy matches the source.
5. Writes `<McBuildDir>\swap-wpf-applied.json` as the "swap active" marker.

Without `-Force`: refuses if a swap is already active — run `restore` first.

With `-Force`: refreshes backup with files currently in place, then overwrites.

### `restore` — revert to originals

```powershell
swap-wpf.ps1 restore -McBuildDir <path> [-CleanBackup] [-Json]
```

1. Reads `<McBuildDir>\swap-wpf-backup\manifest.json`.
2. Copies each backed-up file back to `McBuildDir`.
3. Verifies MD5 matches the manifest.
4. Removes `swap-wpf-applied.json`.
5. With `-CleanBackup`: also removes the `swap-wpf-backup\` directory.

### `status` — show current state

```powershell
swap-wpf.ps1 status -McBuildDir <path> [-Json]
```

Prints whether a swap is active, when it was applied, and which fork dir it came from.
`-Json` emits machine-readable JSON.

## State files

| File | Purpose |
|------|---------|
| `<McBuildDir>\swap-wpf-backup\manifest.json` | Original DLL MD5s + timestamp |
| `<McBuildDir>\swap-wpf-applied.json` | Active swap record (present = swap is on) |

## Examples

### Typical A/B perf run

```powershell
# 1. Build patched WPF in the fork (done separately)
#    Output lands in: C:\wpf-fork\artifacts\bin\windows\Release\

# 2. Swap in the fork DLLs
.\swap-wpf.ps1 swap `
    -ForkOutputDir "C:\wpf-fork\artifacts\bin\windows\Release" `
    -McBuildDir    "C:\MC\BUILD\x64_Release"

# 3. Run profiler / perf scenario (separate tooling)

# 4. Check what's installed
.\swap-wpf.ps1 status -McBuildDir "C:\MC\BUILD\x64_Release"

# 5. Restore originals
.\swap-wpf.ps1 restore -McBuildDir "C:\MC\BUILD\x64_Release"
```

### From WSL

```bash
# Windows paths or WSL /c/... paths both work
cmd.exe /c "powershell -ExecutionPolicy Bypass -File \
  /c/work/wpf-perf/tools/swap-wpf/swap-wpf.ps1 \
  swap \
  -ForkOutputDir /c/wpf-fork/artifacts/bin/windows/Release \
  -McBuildDir /c/work/desktop/wpf-test/src/motioncatalyst/BUILD/x64_Release"
```

Or via the `.cmd` wrapper (shorter):

```bash
cmd.exe /c "C:\work\wpf-perf\tools\swap-wpf\swap-wpf.cmd swap \
  -ForkOutputDir C:\wpf-fork\artifacts\bin\windows\Release \
  -McBuildDir C:\MC\BUILD\x64_Release"
```

### JSON output (for scripting)

```bash
# Check status as JSON
cmd.exe /c "powershell -ExecutionPolicy Bypass -File swap-wpf.ps1 \
  status -McBuildDir C:\MC\BUILD\x64_Release -Json"
```

Output when swapped:
```json
{
  "state": "swapped",
  "timestamp": "2026-05-01T12:34:56.0000000+02:00",
  "fork_dir": "C:\\wpf-fork\\artifacts\\bin\\windows\\Release",
  "mc_dir": "C:\\MC\\BUILD\\x64_Release",
  "files": ["PresentationCore.dll", "..."],
  "details": { ... }
}
```

Output when original:
```json
{
  "state": "original",
  "mc_dir": "C:\\MC\\BUILD\\x64_Release",
  "backup_dir": null
}
```

## Running tests

```powershell
# From a PowerShell prompt — no Pester required
pwsh -ExecutionPolicy Bypass .\tests\Test-SwapWpf.ps1

# Or from WSL
cmd.exe /c "powershell -ExecutionPolicy Bypass \
  -File /c/work/wpf-perf/tools/swap-wpf/tests/Test-SwapWpf.ps1"
```

Exit code 0 = all tests passed.

## Design notes

- **Paranoid copy**: every file copy is followed by MD5 verification. A hash mismatch
  is a hard error.
- **Atomic-ish swap**: if any copy fails during the swap phase, the script attempts to
  roll back by restoring the backup files before reporting the error. The backup is
  written before any MC files are touched.
- **Idempotent backup**: the backup is only written once per `swap` call. Re-swapping
  (with `-Force`) refreshes the backup with whatever is currently in `McBuildDir`.
- **No elevation required**: operates entirely on user-accessible file paths in the
  MC build output directory.
- **WSL-friendly**: accepts `/c/work/...` or `/mnt/c/work/...` paths and converts
  them to Windows form before use.
- **Machine-readable**: `-Json` on any command emits structured JSON for use in
  automated runners and CI scripts.
