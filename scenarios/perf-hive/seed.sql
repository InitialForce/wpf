-- perf-hive/seed.sql
--
-- Minimum-viable seed for the WPF perf test hive.
--
-- USAGE:
--   1. Bootstrap the hive (creates DB schema):
--        MotionCatalyst-cli.exe -p SwingCatalyst -d <hive>
--      Wait for it to reach the home screen and exit, or send WM_CLOSE.
--   2. Apply this seed:
--        sqlite3 "<hive>/ProgramData/SwingCatalystDB.s3db" < seed.sql
--
-- The seed is idempotent: it only inserts if no perf data exists yet
-- (guarded by the SELECT … WHERE NOT EXISTS pattern).
--
-- SCHEMA NOTES (verified against db_v69.s3db + live migration source):
--   Instructor  → InstructorID, FirstName, Password, Username
--   Student     → StudentID, FirstName, LastName, InstructorID, ...
--   Session     → SessionID, Date, Name, Type, StudentID   (ORM name: SessionStudent)
--   Take        → TakeID, Date, Rating, Favorite, OnlineID, SportID,
--                  Activity, TakeTypeEnum, SessionID, ...
--   Bookmark    → BookmarkID, IsDefault, Name, TimeTicks, TypeID, TakeID
--
-- DEFERRED (v2):
--   - .take file import (binary blobs): handled by MotionCatalyst-cli.exe import-takes,
--     not raw SQL. See README.md.
--   - Video-frame data (VideoClip, ForceData, HardwareData rows): populated by import-takes.
--   - Drawing-tool annotations (Line/Arrow/Circle overlays): in-memory UI only,
--     not stored in SQLite schema. Not seedable via SQL.

PRAGMA journal_mode=WAL;

-- ──────────────────────────────────────────────
-- Instructor (default login: username0 / MD5)
-- Row already inserted by MC bootstrap; insert only if missing.
-- ──────────────────────────────────────────────
INSERT OR IGNORE INTO Instructor (InstructorID, FirstName, Password, Username)
VALUES (1, 'instructor0', '5425AB89A474ED3C04982822B9C52251', 'username0');

-- ──────────────────────────────────────────────
-- Student: "Perf Test User"
-- ──────────────────────────────────────────────
INSERT OR IGNORE INTO Student (StudentID, FirstName, LastName, InstructorID,
                                IsLinks, FootLengthMm,
                                CenterOfMassXMm, CenterOfMassYMm, CenterOfMassZMm,
                                ReadOnly, Gender)
VALUES (100, 'Perf', 'Test User', 1,
        0, 270,
        0, 0, 0,
        0, 0);

-- ──────────────────────────────────────────────
-- Session: 1 multi-take session linking 3 takes
-- Type=0 → SingleAnalysis (matches SessionTypeEnum.SingleAnalysis)
-- ──────────────────────────────────────────────
INSERT OR IGNORE INTO Session (SessionID, Date, Name, TestSetName, Type, StudentID)
VALUES (100,
        '2026-01-01T00:00:00.0000000',
        'Perf Test Session',
        NULL,
        0,
        100);

-- ──────────────────────────────────────────────
-- Takes: 3 placeholder rows — one per .take fixture.
--
-- These rows establish the TakeID slots. Full video/sensor data is
-- loaded when MotionCatalyst-cli.exe import-takes is called (see README.md).
--
-- SportID=1 → Golf
-- Activity=1 → GolfSwing
-- TakeTypeEnum=0 → CreatedLocal
-- Rating=0 → Unrated
-- BeforeTriggerDurationTicks / AfterTriggerDurationTicks: typical values
--   from ThreeRecordings.s3db (30_000_000 = 3s @ 10MHz ticks)
-- ──────────────────────────────────────────────
INSERT OR IGNORE INTO Take (TakeID, Comment, Date, Favorite, OnlineID, Rating,
                             SportID, Activity, ActivityOrder, TakeTypeEnum,
                             MovementVariation,
                             CenterOfMassXMm, CenterOfMassYMm, CenterOfMassZMm,
                             FootLengthMm, UserWeightKg,
                             BeforeTriggerDurationTicks, AfterTriggerDurationTicks,
                             RecordApplicationVersion, ExportApplicationVersion,
                             ClubID, GroupSessionID, SessionID)
VALUES
  -- Take 1: mirrors "Take_v01 - Golf swing - MP1 - TrackMan.take"
  (101,
   'Perf fixture: Take_v01 Golf swing MP1 TrackMan',
   '2026-01-01T00:01:00.0000000',
   0, 0, 0,
   1, 1, NULL, 0,
   NULL,
   0, 0, 0,
   270, 80.0,
   30000000, 20000000,
   'perf-seed', 'perf-seed',
   NULL, NULL, 100),

  -- Take 2: mirrors "Take_v07 - Golf swing - MP4 - FlightScope.take"
  (102,
   'Perf fixture: Take_v07 Golf swing MP4 FlightScope',
   '2026-01-01T00:02:00.0000000',
   0, 0, 0,
   1, 1, NULL, 0,
   NULL,
   0, 0, 0,
   270, 80.0,
   30000000, 20000000,
   'perf-seed', 'perf-seed',
   NULL, NULL, 100),

  -- Take 3: mirrors "Take_v11 - Golf swing - MP5 - TrackMan.take"
  (103,
   'Perf fixture: Take_v11 Golf swing MP5 TrackMan',
   '2026-01-01T00:03:00.0000000',
   0, 0, 0,
   1, 1, NULL, 0,
   NULL,
   0, 0, 0,
   270, 80.0,
   30000000, 20000000,
   'perf-seed', 'perf-seed',
   NULL, NULL, 100);

-- ──────────────────────────────────────────────
-- Bookmarks (timeline annotations — 3 per take = 9 total).
--
-- TypeID GUIDs are the canonical swing-phase bookmark types defined in
-- MigrationStepTo40.cs and used throughout the bookmark model:
--   5bdb8bfd-... → Start of Take Away (@ 2s)
--   4161f12f-... → Top of Swing     (@ 2.67s)
--   269cf972-... → Impact            (@ 3s)
--
-- TimeTicks: TimeSpan.Ticks (1 tick = 100 ns).
--   2s   = 20_000_000 ticks
--   2.67s= 26_700_000 ticks
--   3s   = 30_000_000 ticks
--
-- IsDefault=1 → these are the system-default swing phase markers.
-- ──────────────────────────────────────────────

-- Take 101 bookmarks
INSERT OR IGNORE INTO Bookmark (BookmarkID, IsDefault, Name, TimeTicks, TypeID, TakeID)
VALUES
  (1001, 1, 'Start of Take Away', 20000000, '5bdb8bfd-2576-4cc1-a9fe-af8b37299a97', 101),
  (1002, 1, 'Top of Swing',       26700000, '4161f12f-ba49-4b9c-a1a0-48ec9be76a45', 101),
  (1003, 1, 'Impact',             30000000, '269cf972-5d8d-41b5-9e4c-758367b62426', 101);

-- Take 102 bookmarks
INSERT OR IGNORE INTO Bookmark (BookmarkID, IsDefault, Name, TimeTicks, TypeID, TakeID)
VALUES
  (1004, 1, 'Start of Take Away', 20000000, '5bdb8bfd-2576-4cc1-a9fe-af8b37299a97', 102),
  (1005, 1, 'Top of Swing',       26700000, '4161f12f-ba49-4b9c-a1a0-48ec9be76a45', 102),
  (1006, 1, 'Impact',             30000000, '269cf972-5d8d-41b5-9e4c-758367b62426', 102);

-- Take 103 bookmarks
INSERT OR IGNORE INTO Bookmark (BookmarkID, IsDefault, Name, TimeTicks, TypeID, TakeID)
VALUES
  (1007, 1, 'Start of Take Away', 20000000, '5bdb8bfd-2576-4cc1-a9fe-af8b37299a97', 103),
  (1008, 1, 'Top of Swing',       26700000, '4161f12f-ba49-4b9c-a1a0-48ec9be76a45', 103),
  (1009, 1, 'Impact',             30000000, '269cf972-5d8d-41b5-9e4c-758367b62426', 103);
