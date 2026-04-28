# InitialForce.WpfSmoke — WPF Smoke Test Harness

Post-publish safety net for the `InitialForce.WPF` NuGet package. Contains
22 NUnit scenarios covering the patches applied to the upstream `dotnet/wpf`
fork, plus a BenchmarkDotNet perf harness and pixel-diff golden infrastructure.

---

## Prerequisites

- Windows 10 or later (WPF is Windows-only)
- .NET 10 SDK (`winget install Microsoft.DotNet.SDK.10`)
- A built `InitialForce.WPF` NuGet package in a local feed (see below)

---

## Running tests locally on Windows

### 1. Build the package

```powershell
# From repo root — builds WPF and packs the NuGet package into artifacts/nuget/
.\tools\build-and-pack.ps1
```

Or manually:

```powershell
# Copy built DLLs into the package staging area (from a WPF build output):
$srcDir = "artifacts/packages/Release/Shipping/Microsoft.DotNet.Wpf.GitHub/lib/net10.0-windows"
$dstDir = "packaging/InitialForce.WPF/runtimes/win-x64/lib/net10.0-windows"
New-Item -ItemType Directory -Force $dstDir | Out-Null
foreach ($asm in "PresentationCore","PresentationFramework","WindowsBase","System.Xaml") {
    Copy-Item "$srcDir\$asm.dll" "$dstDir\"
    Copy-Item "$srcDir\$asm.pdb" "$dstDir\"
}
dotnet pack packaging/InitialForce.WPF/InitialForce.WPF.csproj `
    -p:PackageVersion=10.0.0-dev.local `
    -o artifacts/nuget/
```

### 2. Run all 22 smoke scenarios

```powershell
dotnet test test/InitialForce.WpfSmoke/InitialForce.WpfSmoke.csproj `
    -p:InitialForceWpfVersion=10.0.0-dev.local `
    --source artifacts/nuget/ `
    --logger "console;verbosity=normal"
```

### 3. Run a specific scenario

```powershell
dotnet test test/InitialForce.WpfSmoke/ `
    -p:InitialForceWpfVersion=10.0.0-dev.local `
    --source artifacts/nuget/ `
    --filter "FullyQualifiedName~SMOKE-004"
```

### 4. Regenerate pixel-diff goldens (after intentional rendering change)

```powershell
dotnet run --project test/InitialForce.WpfSmoke/ -c Release `
    -p:InitialForceWpfVersion=10.0.0-dev.local `
    --source artifacts/nuget/ `
    -- --update-goldens
```

Commit the updated PNGs and SHA-256 files in `goldens/`. PR title must contain
`[update-goldens]` for human review.

### 5. Run the BenchmarkDotNet perf harness

```powershell
# Standalone perf project (self-contained, Release config required for valid results):
dotnet run --project test/InitialForce.WpfSmoke/Perf/InitialForce.WpfPerf.csproj `
    -c Release `
    -p:InitialForceWpfVersion=10.0.0-dev.local `
    --source artifacts/nuget/ `
    -- --filter "*" --exporters json
```

Results appear under `BenchmarkDotNet.Artifacts/results/`. The JSON output is
post-processed by `perf/check-regression.py`:

```powershell
python perf/check-regression.py `
    --current-sha (git rev-parse --short HEAD) `
    --series perf/series.jsonl
```

---

## How CI runs the suite

CI workflow (`.github/workflows/smoke-tests.yml`) runs on every PR that
touches `src/Microsoft.DotNet.Wpf/src/PresentationFramework/**`:

```yaml
- name: Run smoke tests
  shell: pwsh
  run: |
    dotnet test test/InitialForce.WpfSmoke/ `
      -p:InitialForceWpfVersion=${{ steps.version.outputs.PACKAGE_VERSION }} `
      --source artifacts/nuget/ `
      --logger "trx;LogFileName=smoke-results.trx" `
      --results-directory artifacts/test-results/
```

The NUnit adapter produces a `.trx` file uploaded as a CI artifact. A separate
step runs `perf/check-regression.py` and fails the build on regressions.

---

## Golden image workflow

Golden images live in `goldens/SMOKE-0XX/` alongside a `.sha256` sidecar file:

```
goldens/
├── SMOKE-009/
│   ├── 96-default.png       ← rendered at 96 DPI, default theme (WARP)
│   └── 96-default.png.sha256
├── SMOKE-010/
│   └── ...
├── SMOKE-011/
│   └── ...
└── SMOKE-012/
    └── ...
```

Goldens are generated on the zero-patch baseline using `--update-goldens`. They
must be regenerated when an intentional rendering change is made. All golden
regenerations require a `[update-goldens]` PR for human sign-off.

---

## Scenario list

| ID | Test class | Method | Covers | Type |
|----|------------|--------|--------|------|
| SMOKE-001 | `GeometryParserTests` | `RoundTrip10kPaths` | PR #6272 span-slice | Test |
| SMOKE-002 | `GeometryParserBench` | `ReadNumberBench` | PR #6272 perf | Benchmark |
| SMOKE-003 | `ListCollectionViewTests` | `SortOf50kItems` | `ListCollectionView` sort | Test |
| SMOKE-004 | `ListCollectionViewTests` | `PrepareComparerZeroAllocs` | PR #6511 zero-alloc | Test |
| SMOKE-005 | `FrugalListTests` | `InsertRemoveRoundTrip` | PR #6280 correctness | Test |
| SMOKE-006 | `FrugalListTests` | `GenericIntNoBoxing` | PR #6280 generic path | Test |
| SMOKE-007 | `WeakReferenceListTests` | `EnumeratorNotBoxed` | PR #6502 boxing fix | Test |
| SMOKE-008 | `VirtualizingPanelTests` | `Only30ContainersRealized` | VSP container recycling | Test |
| SMOKE-009 | `PixelDiffTests` | `XamlSceneA` | Rendering correctness | Test |
| SMOKE-010 | `PixelDiffTests` | `DataGrid5Rows` | DataGrid rendering | Test |
| SMOKE-011 | `PixelDiffTests` | `FlowDocument` | FlowDocument layout | Test |
| SMOKE-012 | `PixelDiffTests` | `RtlText` | RTL text path | Test |
| SMOKE-013 | `HitTestingTests` | `ThreeRectanglesNinePoints` | `VisualTreeHelper.HitTest` | Test |
| SMOKE-014 | `ImageLoadingTests` | `DecodeAllFormats` | BitmapImage decode | Test |
| SMOKE-015 | `ImageLoadingBench` | `JpegDecode100` | Image pipeline perf | Benchmark |
| SMOKE-016 | `DataBindingTests` | `ItemsControlUpdatesOnChange` | INPC binding | Test |
| SMOKE-017 | `DataBindingTests` | `MultiBindingConverterChain` | MultiBinding | Test |
| SMOKE-018 | `AnimationTests` | `DoubleAnimationReachesTarget` | DoubleAnimation | Test |
| SMOKE-019 | `StyleTests` | `ResourceDictionaryAllStylesResolve` | ResourceDictionary | Test |
| SMOKE-020 | `SortTests` | `ArrayListSortGenericPath` | PR #6285 non-generic sort | Test |
| SMOKE-021 | `PresentationSourceTests` | `NoLeakAfter100Windows` | PR #6502 WeakRef leak | Test |
| SMOKE-022 | `LifecycleTests` | `AppRunShutdownClean` | Application lifecycle | Test |

---

## Known limitations

See `docs/known-limitations.md` for scenarios that are **not tested in CI** and
require manual sign-off (printer/print-preview, IME composition, Narrator/UIA,
multi-monitor DPI, high-contrast themes, RTL shell integration).

---

## Adding a new scenario

1. Pick the next available `SMOKE-0XX` ID.
2. Create a new `Smoke/<ClassName>Tests.cs` file.
3. Derive from `SmokeBase` (for STA helpers and suite-level guards).
4. Add a `[Test]` method named exactly as specified in the scenario table.
5. Add a corresponding benchmark in `Perf/PerfHarness.cs` if the scenario has a perf gate.
6. Update the scenario table in this README.
7. Add a validator assertion in `tests/test_smoke_harness_structure.py`.
