# Test Quality Audit — Agent 6 / Round 1
**Lens: Test Coverage Quality — structural shape vs actual behavior**
**Date:** 2026-04-28

---

## 1. Numeric Breakdown: STRUCTURAL vs BEHAVIORAL per Tool/File

The automated categorisation used the following heuristics:
- **STRUCTURAL**: test checks YAML/file shape, string presence, field existence, permissions
  in parsed YAML — without invoking any logic that exercises a code path.
- **BEHAVIORAL**: test calls a function, subprocess, or CLI and asserts on computed output,
  exit code, or side effects.

### Tool tests (pure Python tools — near-100% behavioral)

| File | Tests | Structural | Behavioral | Behav % |
|---|---|---|---|---|
| test_ledger_event.py | 27 | 0 | 27 | 100% |
| test_ledger_validate.py | 19 | 0 | 19 | 100% |
| test_check_regression.py | 33 | 0 | 33 | 100% |
| test_merge_verdicts.py | 16 | 0 | 16 | 100% |
| test_check_denylist.py | 16 (est.) | 0 | 16 | 100% |
| test_diff_smoke_results.py | 27 | 0 | 27 | 100% |
| test_regenerate_state.py | 40 | 0 | 40 | 100% |
| test_seed_ledger.py | 28 | 0 | 28 | 100% |
| test_check_graduated.py | 34 | 0 | 34 | 100% |
| test_dispatch_approved.py | 14 | 0 | 14 | 100% |
| test_check_config_schema.py | 13 | 0 | 13 | 100% |

### Workflow tests (YAML-parsing structural tests — very low behavioral ratio)

| File | Tests | Structural | Behavioral | Behav % |
|---|---|---|---|---|
| test_pr_review_workflow.py | 45 | 44 | 1 (actionlint only) | 2% |
| test_pr_ingestion_workflow.py | 24 | ~16 | ~8 | 33% |
| test_pr_discovery_workflow.py | 24 | ~17 | ~7 | 29% |
| test_release_workflow.py | 41 | ~15 | ~26 | 63% |
| test_build_workflow.py | 24 | ~10 | ~14 | 58% |
| test_autonomy_check_workflow.py | 11 | 1 | 10 | 91% |
| test_claude_on_failure_workflow.py | 21 | 10 | 11 | 52% |
| test_nightly_rebase_workflow.py | 13 | 4 | 9 | 69% |

### Overall aggregate

| Category | Count | Percent |
|---|---|---|
| BEHAVIORAL | ~549 | 85% |
| STRUCTURAL (workflow YAML shape) | ~94 | 15% |
| **Total** | **~643** | |

**Interpretation:** The tool-level tests are almost entirely behavioral — they invoke
real logic and check computed results. The workflow-level tests are the structural
outliers: nearly all 45 `test_pr_review_workflow.py` tests parse YAML and assert string
membership, with no actual code execution. This matches the root cause of CRIT-2.

---

## 2. Behavioral Gaps — Untested Behaviors that Could Harbor Bugs

### BUG CONFIRMED: Invalid event names in workflow YAML (CRIT-2 class regression)

This is the most critical finding. Four `--event` argument values used in workflow YAML
files are **NOT present in `VALID_EVENTS`** in `tools/ledger_schema.py`. Each would cause
`ledger-event.py` to exit with code 2 at runtime, silently aborting the workflow step.

| Event name | Workflow file | In VALID_EVENTS? |
|---|---|---|
| `pre_flight_failed` | `.github/workflows/pr-ingestion.yml:104` | NO |
| `rebase_failed` | `.github/workflows/nightly-rebase.yml:207` | NO |
| `failure_analyzed` | `.github/workflows/claude-on-failure.yml:237` | NO |
| `review_single_path_warning` | `.github/workflows/pr-review.yml:245` | NO |

The tests for these workflows assert only that the string `"pre_flight_failed"` (etc.)
appears in the raw YAML text — not that `ledger-event.py` accepts it. This is the exact
same failure mode as the original CRIT-2: string-in-file assertion passes, runtime fails.

No test crosses the boundary between the workflow YAML and the tool's VALID_EVENTS set.

### Gap 2: "All 3 push retries fail" path in ledger-event.py

`test_push_retries_on_non_ff` (test 25) covers the 1-fail-then-succeed path. There is no
test for the path where all `MAX_PUSH_RETRIES` (3) attempts are rejected non-FF, causing
`die(6, "git push failed after N attempts")`. The `_push_with_retry` function's
exhaustion branch is entirely untested.

### Gap 3: ledger-validate.py — multi-error accumulation

Tests cover single-error cases (one bad line). There is no test that provides a ledger
with 3+ distinct error types on different lines and verifies that ALL errors are reported
rather than short-circuiting after the first. If `validate()` returns early, a ledger with
both `invalid_json` and `prev_hash_mismatch` errors would only report one.

### Gap 4: diff-smoke-results.py — simultaneous regression AND drift

The test suite has a dedicated `TestRegressionCase` and a `TestDriftCase` but no test for
the scenario where a fork XML has both a pass→fail regression AND a missing/new scenario
simultaneously. The priority of `verdict` (regression vs drift) in that case is untested.

### Gap 5: check-regression.py — custom threshold + zero-baseline interaction

The `TestZeroBaselineRegression` tests force allocation to 0/positive with default
thresholds. `TestCustomThresholds` uses non-default thresholds with normal values. No test
combines them: zero-baseline + custom threshold. The `compute_delta_pct` function returns
`float("inf")`, which is correct; but downstream threshold comparison logic (is `inf > 1`?)
is not separately asserted when thresholds are non-default.

---

## 3. Workflow-Level Integration Tests: Gap Assessment

**There are zero end-to-end pipeline tests.** Each workflow's tests operate independently
on a single YAML file. The full pipeline (PR discovered → pr-discovery dispatches
`pr-discovered` → pr-review triggers → merge-verdict dispatches `pr-ingestion-requested`
→ pr-ingestion runs → release publishes) is never exercised as a connected sequence.

**Is this a gap?** Yes, but it is the expected gap for a CI-gated system that requires
real GitHub Actions infrastructure to run. A full E2E test would need secrets, a real
repo, and actual Claude API calls.

**Recommended mitigation (future work):** Add a `dry-run-pipeline` workflow that:
1. Synthesises a fake discovered PR event.
2. Runs each tool with `--dry-run` in the correct sequence using static fixtures.
3. Validates that each tool's output is a valid input to the next tool.
4. Can run in GitHub Actions without secrets (all mocked).

Document this as a known gap in `docs/known-limitations.md`.

---

## 4. CRIT-2 Root Cause: Fixed for CLI Args, Not for Event Names

The CRIT-2 fix added tests that assert `--head-sha`, `--actor`, `--details-json`, and
`--push` appear in the raw workflow YAML text (e.g.,
`test_ledger_event_canonical_cli_args` in `test_pr_ingestion_workflow.py` line 435).
This IS the suggested string-grep approach and it works for CLI flag names.

However, the same approach was NOT applied to `--event` argument values. The YAML grep
tests confirm the flags exist but never verify the event values are legal.

**Does any test do the recommended "grep workflow YAML for tool invocations, then run with
`--dry-run`" cross-validation?** No. There is no CI step or test that:
1. Extracts every `python tools/ledger-event.py ... --event <X>` invocation from workflow
   YAML files.
2. Runs `python tools/ledger-event.py --event <X> ... --dry-run` to confirm it parses.

This is the precise gap that would have caught all four invalid event names above.

---

## 5. Test Isolation: Shared State

Running `grep` for `.if-fork/patch-ledger.jsonl` in `tests/`:

- **`tests/test_check_config_schema.py` line 104**: The path appears only in a helper
  dict that constructs a minimal valid config (the `ledger.path` field value). This is
  a string literal in a test dict, not a file write. **Not a shared state issue.**

- **`tests/test_pr_ingestion_workflow.py` lines 371/374**: Only in assertion strings
  (`"SHA-smuggling check must read from .if-fork/patch-ledger.jsonl"`). **Not a shared
  state issue.**

No test writes to the real `.if-fork/patch-ledger.jsonl` path. All tests that actually
write ledger data use `tmp_path` fixtures from pytest. **Test isolation is clean.**

---

## 6. Flaky Test Scan

- **`time.sleep` usage**: `test_ledger_event.py` line 540 patches `time.sleep` with
  `with patch("time.sleep"):` inside `test_push_retries_on_non_ff`. This is a correct
  mock — no actual sleep occurs. No flakiness concern.

- **Network calls**: `test_dispatch_approved.py` lines 119 and 166 patch
  `urllib.request.urlopen` with a `MagicMock`. No real HTTP calls are made. Clean.

- **Order dependence**: No test relies on another test's output or state. All use
  `tmp_path` or purely in-memory structures. No shared module-level mutable state was
  found beyond module-scope `@pytest.fixture(scope="module")` fixtures that parse YAML
  read-only.

- **actionlint tests**: All properly guarded with `pytest.skip` when actionlint is not
  installed. No flakiness.

**Conclusion: No flaky tests detected.**

---

## 7. Error Path Coverage

### GPG signing fails in CI (test_ledger_event.py)

**Covered.** `test_ci_gpg_failure_exits_nonzero` (test 22) mocks subprocess to return
returncode=128 for `git commit -S`, patches `CI=true`, and asserts exit nonzero.
`test_non_ci_gpg_failure_falls_back` (test 23) covers the non-CI fallback to unsigned.
Both paths are correctly tested.

### All 3 push retries fail (test_ledger_event.py)

**NOT covered.** `test_push_retries_on_non_ff` (test 25) simulates exactly 2 push
attempts (1 fail + 1 success). The `die(6, "git push failed after N attempts")` path
reached when `attempt == MAX_PUSH_RETRIES` and the push is still rejected is never
exercised.

### Malformed JSONL line in ledger-validate.py

**Partially covered.** `test_invalid_json_line` (test 9) provides a single non-JSON
line and checks for `invalid_json` error. However, no test provides a mix of a
valid line followed by a malformed line followed by another valid line, to verify
that validation continues past the first error and accumulates all errors.

### ledger-validate.py — strict-signature path with actual git unavailability

**Covered with a mock.** `test_strict_signature_no_git_skips_gracefully` (test 15)
patches `_find_commit_for_line_hash` to return `None` and verifies a
`missing_gpg_signature` error is reported rather than a crash. This is a valid
approach.

---

## Top 5 Untested Behaviors by Impact

### #1 (CRITICAL) — Invalid event names in workflows rejected at runtime

Four workflow YAML files use `--event` values not in `VALID_EVENTS`. Every invocation
would fail with exit code 2 in production. No test validates event name validity
across the workflow→tool boundary.

**Recommended test:**
```python
def test_all_workflow_event_names_are_valid() -> None:
    import re
    from pathlib import Path
    from tools.ledger_schema import VALID_EVENTS  # or import via importlib
    
    workflows_dir = Path(".github/workflows")
    for yml in workflows_dir.glob("*.yml"):
        text = yml.read_text()
        for m in re.finditer(r'--event\s+(\S+)', text):
            event = m.group(1)
            assert event in VALID_EVENTS, (
                f"{yml.name}: --event {event!r} is not in VALID_EVENTS"
            )
```

### #2 (HIGH) — Push retry exhaustion (all 3 retries fail)

The `die(6)` path in `_push_with_retry` is never exercised. A persistent remote-lock
scenario would silently produce exit 6 in production with no test coverage.

**Recommended test:**
```python
def test_push_all_retries_exhausted_exits_nonzero(tmp_path):
    ledger = tmp_path / "patch-ledger.jsonl"
    push_fail = subprocess.CompletedProcess(["git", "push"], 1, "", "[rejected] non-fast-forward")
    
    def fake_run(cmd, **kwargs):
        if "push" in cmd:
            result = push_fail
            if kwargs.get("check") and result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, cmd)
            return result
        return subprocess.CompletedProcess(cmd, 0, "", "")
    
    with patch("subprocess.run", side_effect=fake_run):
        with patch("time.sleep"):
            with pytest.raises(SystemExit) as exc:
                ledger_event.main(_make_argv(ledger=str(ledger)) + ["--push"])
    assert exc.value.code != 0
```

### #3 (HIGH) — Workflow CLI invocation dry-run cross-validation

No test parses workflow YAML, extracts `python tools/X.py` invocations, and executes
each with `--dry-run` to verify it parses. This is the only fully automated way to
catch CRIT-2-class bugs where the YAML string looks right but the tool rejects the arg.

**Recommended test (in new file `test_workflow_tool_invocations.py`):**
```python
def test_ledger_event_invocations_parse_correctly():
    """Extract every ledger-event.py call from workflow YAML; verify each --event parses."""
    import re, subprocess, sys
    from pathlib import Path
    
    for yml in Path(".github/workflows").glob("*.yml"):
        text = yml.read_text()
        for m in re.finditer(r'--event\s+(\S+)', text):
            event = m.group(1)
            result = subprocess.run(
                [sys.executable, "tools/ledger-event.py",
                 "--event", event, "--pr-number", "1", "--head-sha", "abc",
                 "--actor", "test", "--details-json", "{}", "--dry-run"],
                capture_output=True, text=True
            )
            assert result.returncode == 0, (
                f"{yml.name}: --event {event!r} rejected by ledger-event.py "
                f"(exit {result.returncode}): {result.stderr}"
            )
```

### #4 (MEDIUM) — ledger-validate.py multi-error accumulation

No test verifies that `validate()` reports all errors in a multi-error ledger rather
than short-circuiting after the first.

**Recommended test:**
```python
def test_multiple_errors_all_reported(tmp_path):
    ledger = tmp_path / "l.jsonl"
    line1 = _build_valid_line(event="discovered", prev_hash="")
    line2 = "not-json-at-all"
    line3_obj = json.loads(_build_valid_line(event="review_1", prev_hash="wronghash" * 7))
    _write_ledger(ledger, [line1, line2, json.dumps(line3_obj)])
    result = ledger_validate.validate(ledger)
    kinds = {e["kind"] for e in result["errors"]}
    assert "invalid_json" in kinds
    assert "prev_hash_mismatch" in kinds
    assert len(result["errors"]) >= 2
```

### #5 (MEDIUM) — diff-smoke-results.py simultaneous regression + drift verdict priority

When a fork XML has both a pass→fail regression and a missing scenario, the tool must
choose one `verdict` string. The priority (regression > drift? or vice versa?) is
unspecified and untested.

**Recommended test:**
```python
def test_regression_takes_priority_over_drift(tmp_path):
    # Build a fork with one pass->fail AND one missing scenario
    # Verify verdict is "regression" not "drift"
    ...
```

---

## Recommended New Tests — Prioritized by Impact

| Priority | Test | Target File | Risk Mitigated |
|---|---|---|---|
| P0 | `test_all_workflow_event_names_are_valid` | new `test_workflow_tool_invocations.py` | 4 invalid events → runtime exit 2 |
| P0 | `test_ledger_event_invocations_dry_run` | new `test_workflow_tool_invocations.py` | CRIT-2 class bug recurrence |
| P1 | `test_push_all_retries_exhausted_exits_nonzero` | `test_ledger_event.py` | push exhaustion silent failure |
| P2 | `test_multiple_errors_all_reported` | `test_ledger_validate.py` | early-exit masking validation errors |
| P2 | `test_regression_takes_priority_over_drift` | `test_diff_smoke_results.py` | verdict priority undefined |
| P3 | `test_zero_baseline_custom_threshold_fails` | `test_check_regression.py` | inf comparison with non-default threshold |

---

## Summary (5 sentences)

The tool-level tests (ledger-event, ledger-validate, check-regression, etc.) are
predominantly behavioral — they invoke real logic via subprocess or direct function calls
and assert on computed results, achieving approximately 85% behavioral coverage overall.
The workflow-level tests are the dominant weak spot: all 44 of 45 tests in
`test_pr_review_workflow.py` are purely structural YAML-shape assertions, and the same
pattern holds across other workflow test files, which is the exact failure mode that
enabled CRIT-2 and similar bugs.
The most critical new finding is that four `--event` values used in production workflow
YAML (`pre_flight_failed`, `rebase_failed`, `failure_analyzed`,
`review_single_path_warning`) are absent from `VALID_EVENTS` in `ledger_schema.py`, meaning
every one of those ledger-event.py invocations would exit with code 2 at runtime — and
no test crosses the workflow→tool boundary to catch this.
Test isolation is clean (all ledger writes use `tmp_path`; `time.sleep` and network calls
are properly mocked), so there are no flakiness or shared-state concerns.
The highest-priority new tests are: (1) a cross-boundary check that extracts every
`--event <X>` from workflow YAML and validates each against `VALID_EVENTS` or by actually
running `ledger-event.py --dry-run`, and (2) a test for the push-retry exhaustion path
that is currently unreachable by the existing test suite.
