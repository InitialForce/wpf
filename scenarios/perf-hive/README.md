# perf-hive

Template directory for the WPF perf test hive.

The perf-runner copies these files into a fresh hive before each MC run to ensure a clean, deterministic, hardware-disabled starting state.

---

## Files

| File | Purpose |
|------|---------|
| `GeneralSettings.xml` | All hardware vendor `Use*` flags `false`. Debug cameras enabled (2-camera synthetic rig). Audio, sounds, wizards, network-on-startup suppressed. DX rendering stays enabled (no software fallback — that would corrupt GPU profiling). |
| `DebugSettings.xml` | `EnableSimulatedHardwareSystem=true` (synthetic force plates + pressure pads). Fake LM devices. Exception dumps off. Repository logging off. |
| `PrivacySettings.xml` | Analytics consent `false`. Suppresses Sentry + analytics consent dialogs. |
| `seed.sql` | SQLite seed: 1 student, 1 session, 3 takes, 9 timeline bookmarks (Take Away / Top / Impact for each take). Applied after MC bootstraps the DB schema. |

---

## Runner workflow

```
# 1. Create a clean hive directory
$hive = "C:\perf-hive-$(Get-Date -f yyyyMMdd_HHmmss)"
New-Item -ItemType Directory $hive

# 2. Copy settings templates
$settingsDir = "$hive\ProgramData\settings"
New-Item -ItemType Directory $settingsDir
Copy-Item GeneralSettings.xml, DebugSettings.xml, PrivacySettings.xml $settingsDir

# 3. Bootstrap: run MC once to create DB schema, then exit
# (runner sends WM_CLOSE after MC_READY sentinel — W1.1)
MotionCatalyst-cli.exe -p SwingCatalyst -d "$hive"

# 4. Apply seed
$db = "$hive\ProgramData\SwingCatalystDB.s3db"
sqlite3 $db < seed.sql

# 5. (Optional) Import .take fixtures for video/sensor data
MotionCatalyst-cli.exe import-takes `
    --dir "<wpf-test-root>\src\motioncatalyst\Tests\SwingCatalyst.Test.Integration.Windows\Assets\Takes" `
    --hiveDir "$hive"

# 6. Run the perf scenario
MotionCatalyst-cli.exe -p SwingCatalyst -d "$hive" --ui-mcp-pipe perf-session
```

**Settings path inside the hive:** `<hive>/ProgramData/settings/` (same path used by `AppLauncher.SetupHive()` in the QA test harness).

**DB path inside the hive:** `<hive>/ProgramData/SwingCatalystDB.s3db` (constant `SeedHelper.DatabaseRelativePath`).

---

## Seed coverage

### Delivered (v1)

| Item | Status | Notes |
|------|--------|-------|
| 1 student "Perf Test User" | Seeded | `StudentID=100`, linked to `InstructorID=1` |
| 1 multi-take session | Seeded | `SessionID=100`, type=SingleAnalysis |
| 3 take rows | Seeded | `TakeID=101/102/103`, linked to session 100 |
| 9 timeline bookmarks | Seeded | 3 per take: Take Away / Top of Swing / Impact |

### Deferred (v2)

| Item | Why deferred | Path forward |
|------|-------------|--------------|
| Video / sensor data for takes | Binary blob format in `.take` files; not seedable via SQL | Call `MotionCatalyst-cli.exe import-takes --dir <takes-dir> --hiveDir <hive>` as a runner pre-step (step 5 above). The CLI command imports `.take` files and wires them into the DB. |
| Drawing-tool annotations (Line/Arrow/Circle overlay) | These are in-memory UI canvas overlays; they are not persisted in the SQLite schema | Use `wpf_act_sequence` in the v1 scenario to draw annotations live during the scenario run, or defer to v2 if not needed for the perf workload. |
| GroupSession (multi-cam session linking) | `GroupSession` table exists but MC does not expose a CLI command to create one; requires app interaction | Seed via `wpf_*` MCP tools during scenario setup, or defer. The 3 takes linked to one `Session` row is sufficient for the analysis-view workload. |

---

## Notes on XML format

The three XML files are deserialized by `XmlSerializer<GeneralSettings>` / `XmlSerializer<DebugSettings>` / `XmlSerializer<PrivacySettings>` inside `SettingsFile<T>`.

- Root element names must match the C# class name exactly.
- `UseSkyTrak` serializes as `<UseSkyTrak2>` (see `[XmlAttribute("UseSkyTrak2")]` in `GeneralSettings.cs`).
- `UseDxRendering` serializes as `<UseDxRendering2>` and `UseDxEncoding` as `<UseDxEncoding2>` (same pattern).
- `UseCoreAudio` is a forward-compat field: it is not yet in `GeneralSettings.cs` (pending W1.9). MC will ignore the element until W1.9 lands; including it now avoids a re-deploy of the template later.

## Notes on seed idempotency

The seed uses `INSERT OR IGNORE` with explicit fixed primary keys (100–103). Re-running on an already-seeded DB is safe — no duplicates will be created.
