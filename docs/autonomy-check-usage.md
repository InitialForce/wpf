# autonomy-check.yml — Usage Guide

`autonomy-check.yml` is a reusable GitHub Actions workflow that acts as the single
source-of-truth kill switch for all autonomous Claude activity in the InitialForce WPF
fork. Every Claude-invoking workflow calls it as its **first job** and only proceeds if
the gate outputs `proceed=true`. This design means a single `gh variable set` command
can halt all bot activity across every workflow in seconds, without needing to edit or
disable any individual workflow file.

## Calling the gate from another workflow

```yaml
jobs:
  autonomy-check:
    uses: ./.github/workflows/autonomy-check.yml
    with:
      requested_action: claude-invoke   # or: auto-merge, discovery-scan, release-publish
      bypass_for_human_dispatch: false  # set true to let operators bypass the auto-merge freeze

  my-claude-job:
    needs: autonomy-check
    if: needs.autonomy-check.outputs.proceed == 'true'
    runs-on: ubuntu-latest
    steps:
      - name: Do Claude work
        run: echo "Gate passed; reason: ${{ needs.autonomy-check.outputs.reason }}"
```

The gate job never exits non-zero — it always exits 0 and communicates the decision
through the `proceed` output. The calling workflow is responsible for gating downstream
jobs with `if: needs.autonomy-check.outputs.proceed == 'true'`.

## Repository variables

| Variable | Expected value | Effect when wrong |
|---|---|---|
| `IF_AUTONOMY_ENABLED` | `true` | Any value other than `true` sets `proceed=false` and blocks all Claude jobs. Toggle with `gh variable set IF_AUTONOMY_ENABLED -b false --repo InitialForce/wpf`. |
| `IF_AUTOMERGE_FROZEN` | `false` | When `true`, any job with `requested_action: auto-merge` is blocked (other actions are unaffected). Clear the freeze with `gh variable set IF_AUTOMERGE_FROZEN -b false --repo InitialForce/wpf`. |
| `IF_REVIEW_DOUBLE_REQUIRED` | `true` | Consumed directly by `pr-review.yml` (not by this workflow). When `false`, the second independent Opus reviewer is skipped. Should remain `true` in production. |
