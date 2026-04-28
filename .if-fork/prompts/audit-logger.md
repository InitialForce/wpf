## Inherits from preamble.md

All 12 hard prohibitions in `preamble.md` are in effect for this prompt.

---

## Note: This is a workflow step, not a Claude invocation

This prompt is implemented as a YAML workflow step using `python tools/ledger-event.py`
directly — there is no Claude model invocation. It is documented here because every
workflow must embed the steps below as its final step (runs even on failure via
`if: always()`), and the schema below defines the audit event shape that all other prompts
must produce.

---

## Allowed tools

Not applicable (pure bash + python helper, no Claude model invoked).

---

## Inputs

Environment variables set by prior steps:

| Variable | Description |
|---|---|
| `AUDIT_EVENT` | Event name, e.g. `cherry_pick`, `review_1`, `discovery` |
| `PR_NUMBER` | PR number being processed (may be empty for non-PR workflows) |
| `OUTCOME` | `success`, `failure`, or `cancelled` (from `job.status`) |
| `ACTOR` | `claude-wpf-bot` |
| `RUN_URL` | Full URL of the current workflow run |

---

## Output contract

Each audit entry has this JSON shape:

```json
{
  "schema_version": 1,
  "ts": "2026-04-27T14:23:00Z",
  "event": "audit_cherry_pick",
  "actor": "claude-wpf-bot",
  "actor_run_url": "https://github.com/InitialForce/wpf/actions/runs/12345",
  "pr_number": 6511,
  "outcome": "success",
  "details": {}
}
```

Two destinations per run:
1. Patch ledger (via `ledger-event.py`) — signed commit to `audit/` orphan branch
2. `audit-entries.jsonl` artifact uploaded with `retention-days: 400`

The `audit/` orphan branch git history IS the tamper-evident audit log.

---

## Procedure

Embed as the final step in every workflow that calls Claude:

```yaml
- name: Emit audit log entry
  if: always()
  env:
    AUDIT_EVENT: ${{ env.AUDIT_EVENT }}
    PR_NUMBER: ${{ env.PR_NUMBER }}
    OUTCOME: ${{ job.status }}
    ACTOR: claude-wpf-bot
    RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
  run: |
    python tools/ledger-event.py \
      --event "audit_${{ env.AUDIT_EVENT }}" \
      --pr-number "$PR_NUMBER" \
      --details "{\"outcome\":\"$OUTCOME\",\"run_url\":\"$RUN_URL\",\"actor\":\"$ACTOR\"}"

    echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"$AUDIT_EVENT\",\
\"outcome\":\"$OUTCOME\",\"run_url\":\"$RUN_URL\"}" \
      >> audit-entries.jsonl

- name: Upload audit artifact
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: audit-log-${{ github.run_id }}
    path: audit-entries.jsonl
    retention-days: 400
```

---

## Hard-fail patterns

- This step MUST use `if: always()` — it runs even on workflow failure.
- Never bypass `ledger-event.py`; it validates the schema and signs the commit.
- `audit-entries.jsonl` is a local artifact only; `audit/` orphan branch is the authoritative log.
- Never edit `audit-entries.jsonl` directly — it is append-only within a single run.
