# Beads: Cooldown, Halt, Tier C Downgrade

Working branch: `wpf-perf` in `/c/work/wpf-perf/`

All beads are orchestrator-only changes (microbench.py, ralph.sh, program.md).
Inner Claude never touches these files.

---

## Bead: program.md — halt sentinel protocol

- Type: task
- Priority: P1
- BlockedBy: none
- Files touched:
  - `/c/work/wpf-perf/autoresearch/program.md`
- Acceptance criteria:
  - [ ] Step 0 added at top of "Iteration protocol": check for HALT file, print
        reason, exit if present.
  - [ ] Quick reference section includes `ls .../HALT` command.
  - [ ] Hard rules section includes: "If HALT exists, stop immediately. Never
        delete or modify HALT."
  - [ ] Hard rules section includes: "NEVER WRITE to autoresearch/" (read-only
        for inner Claude).
- Test: Re-read program.md; confirm Step 0 is literally the first numbered step
  and hard rules contain both new items.

---

## Bead: microbench.py — halt threshold check + HALT file write

- Type: task
- Priority: P1
- BlockedBy: `program.md — halt sentinel protocol`
- Files touched:
  - `/c/work/wpf-perf/autoresearch/microbench.py`
- Acceptance criteria:
  - [ ] New constant `HALT_FILE = ROOT / "HALT"` and
        `HALT_UNCLEAR_THRESHOLD = int(os.environ.get("WPF_AR_HALT_UNCLEAR_THRESHOLD", "10"))`.
  - [ ] New function `check_halt_threshold()` that:
        a. Reads results.jsonl and filters to tier=="B" rows.
        b. Takes the tail of `HALT_UNCLEAR_THRESHOLD` rows.
        c. Returns True if ALL are verdict=="REJECT-UNCLEAR".
  - [ ] `check_halt_threshold()` called in `main()` immediately after appending
        the new row to results.jsonl.
  - [ ] If threshold reached: write HALT file (plain text, format per design doc),
        log `[microbench] HALT: 10 consecutive REJECT-UNCLEAR — writing HALT sentinel`,
        return exit code 7.
  - [ ] Exit code 7 documented in the module docstring exit-codes table.
  - [ ] `HALT_UNCLEAR_THRESHOLD` documented in the Configuration section comment.
- Test: Manually append 10 REJECT-UNCLEAR tier-B rows to a test copy of
  results.jsonl; run microbench.py with `--no-revert` on a clean commit; confirm
  exit code is 7 and HALT file is written with correct content. Delete HALT before
  committing.

---

## Bead: ralph.sh — handle exit code 7

- Type: task
- Priority: P1
- BlockedBy: `microbench.py — halt threshold check + HALT file write`
- Files touched:
  - `/c/work/wpf-perf/autoresearch/ralph.sh`
- Acceptance criteria:
  - [ ] After `run_claude` call, capture exit code.
  - [ ] If the most recent microbench.py exit code is 7 (detected by checking
        whether HALT file now exists, since ralph.sh cannot introspect inner
        Claude's subprocess exit codes directly), print a clear diagnostic and
        `break` the loop.
  - [ ] Alternatively: check for HALT file existence at the TOP of each ralph.sh
        loop iteration (before spawning claude), break if present, with message:
        `[ralph] HALT sentinel present — loop stopped. See autoresearch/HALT.`
  - [ ] Preferred implementation: check HALT at top of loop (simpler, catches
        manually-written HALT files too).
- Test: Write `autoresearch/HALT` manually; run `ralph.sh 3`; confirm loop breaks
  immediately after printing the diagnostic without spawning claude. Delete HALT.
  Note: this bead's test does NOT require running microbench.py.

---

## Bead: program.md — cooldown rule

- Type: task
- Priority: P1
- BlockedBy: `program.md — halt sentinel protocol`
- Files touched:
  - `/c/work/wpf-perf/autoresearch/program.md`
- Acceptance criteria:
  - [ ] Step 1 updated: results.jsonl instruction changed from `tail -20` to
        `grep '"tier":"B"'` (or read full file), with explicit cool-list
        computation algorithm and `Log: "Cool list: [...]"` instruction.
  - [ ] Step 2 updated: bullet "Few recent REJECT iters" replaced with cool-list
        enforcement (must not pick cooled filter; least-recently-rejected fallback
        if all cooled; log override).
  - [ ] Hard rules section: "COOLDOWN RULE" bullet added.
  - [ ] Quick reference: `cat .../cooldown.json 2>/dev/null` added.
  - [ ] Cooldown counting rule is unambiguous: "2 consecutive REJECT-UNCLEAR"
        means the 2 most-recent tier-B rows for that filter are both REJECT-UNCLEAR;
        only REJECT-UNCLEAR counts (REJECT does not); cooldown lasts until 5
        additional tier-B rows have been written.
- Test: Re-read program.md; verify Step 1 and Step 2 wording matches acceptance
  criteria; trace through the example of GeometryParser having 2 consecutive
  REJECT-UNCLEAR and confirm the rule would correctly cool it.

---

## Bead: microbench.py — write cooldown.json snapshot

- Type: task
- Priority: P2
- BlockedBy: `microbench.py — halt threshold check + HALT file write`
- Files touched:
  - `/c/work/wpf-perf/autoresearch/microbench.py`
- Acceptance criteria:
  - [ ] New function `write_cooldown_snapshot()` that:
        a. Reads results.jsonl, filters tier=="B".
        b. For each unique filter, computes current cooldown state (cooled=True/False,
           cooled_at_row, rows_since, eligible_after_row).
        c. Writes `autoresearch/cooldown.json` with `computed_at`, `cool_filters`
           list, `all_filters` list.
  - [ ] Called at end of `main()` after verdict is written (unconditionally —
        even on KEEP, the snapshot is useful for diagnostics).
  - [ ] `cooldown.json` is listed in `.gitignore` for the autoresearch dir OR
        is explicitly not staged by inner Claude (inner Claude can't write it anyway).
  - [ ] File is valid JSON; `cool_filters` is empty list when nothing is cooled.
- Test: Run microbench.py on a real iteration; confirm cooldown.json is written;
  `python3 -c "import json; print(json.load(open('cooldown.json')))"` succeeds.

---

## Bead: tools/cool-list.py — debug helper

- Type: task
- Priority: P3
- BlockedBy: none (standalone script, reads results.jsonl)
- Files touched:
  - `/c/work/wpf-perf/tools/cool-list.py` (new file)
- Acceptance criteria:
  - [ ] Standalone script, no third-party deps beyond stdlib.
  - [ ] Usage: `python3 tools/cool-list.py [results_jsonl_path]`
        (defaults to `autoresearch/results.jsonl` relative to repo root).
  - [ ] Output: table showing all unique filters, their last 2 verdicts,
        cooldown status (COOLED / eligible), and rows remaining on cooldown.
  - [ ] Exits 0; no side effects (read-only).
  - [ ] --all flag shows full history per filter, not just last 2 rows.
- Test: Run against current results.jsonl; confirm GeometryParser shows cooldown
  status consistent with manual count from the file tail.

---

## Summary

5 beads total:

| Bead | Priority | Touches |
|---|---|---|
| program.md — halt sentinel protocol | P1 | program.md |
| microbench.py — halt threshold + HALT write | P1 | microbench.py |
| ralph.sh — handle exit code 7 / HALT file | P1 | ralph.sh |
| program.md — cooldown rule | P1 | program.md |
| microbench.py — cooldown.json snapshot | P2 | microbench.py |
| tools/cool-list.py debug helper | P3 | tools/cool-list.py (new) |

Dependency order:
1. `program.md — halt sentinel protocol` (no deps)
2. `microbench.py — halt threshold + HALT write` (needs program.md halt bead merged first so the exit-code contract is clear)
3. `ralph.sh — handle exit code 7` (needs microbench.py halt bead)
4. `program.md — cooldown rule` (can be done in parallel with or after halt sentinel bead)
5. `microbench.py — cooldown.json snapshot` (needs halt bead; can be batched with it)
6. `tools/cool-list.py` (standalone, any time)

Recommended execution order for a single implementer: 1 → 4 (both program.md
changes in one sitting) → 2+5 (both microbench.py changes in one sitting) → 3 → 6.
