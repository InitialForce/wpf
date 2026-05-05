#!/usr/bin/env python3
"""Bootstrap baseline.json by running the spike-9 scenario REPS times against
the CURRENT WPF build (whatever's in /c/work/wpf-perf/artifacts/...) and
capturing per-metric medians + std-devs.

Run this ONCE before the autoresearch loop starts. Re-run only if the
baseline assumptions change (different scenario, different MC build, etc.).
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import eval as ev  # reuse run_spike, aggregate, build_wpf, swap, etc.

REPS = int(os.environ.get("WPF_AR_BOOTSTRAP_REPS", "10"))
NO_SWAP = os.environ.get("WPF_AR_NO_SWAP", "0") == "1"


def main() -> int:
    print(f"[bootstrap] capturing baseline ({REPS} reps, no_swap={NO_SWAP}) …")

    if not NO_SWAP:
        if not ev.build_presentation_core():
            print("[bootstrap] FATAL: initial WPF build failed", file=sys.stderr)
            return 1

        if not ev.do_swap():
            print("[bootstrap] FATAL: swap-wpf failed", file=sys.stderr)
            return 1

    per_rep: list[dict] = []
    try:
        for i in range(REPS):
            m = ev.run_spike(rep=i, iter_num=0)
            if m is None:
                print(f"[bootstrap] rep {i} failed; abort", file=sys.stderr)
                return 2
            per_rep.append(m)
    finally:
        if not NO_SWAP:
            ev.do_restore()

    medians, stds = ev.aggregate(per_rep)

    # Sanity floor for std (so a flat metric doesn't blow up z-scores).
    floored_stds = {k: max(stds[k], 0.01 * abs(medians[k])) for k in ev.METRICS}

    baseline = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reps": REPS,
        "medians": medians,
        "stds": floored_stds,
        "raw_stds": stds,
        "per_rep": per_rep,
        "weights": ev.WEIGHTS,
        "pareto_threshold": ev.PARETO_THRESHOLD,
    }
    ev.BASELINE_PATH.write_text(json.dumps(baseline, indent=2))
    print(f"[bootstrap] wrote {ev.BASELINE_PATH}")

    print("\nBaseline summary:")
    print(f"  {'metric':<22} {'median':>14} {'std':>10} {'cv':>8}")
    for k in ev.METRICS:
        m, s = medians[k], stds[k]
        cv = (s / abs(m)) if m else 0.0
        marker = "  ⚠ HIGH" if cv > 0.10 else ""
        print(f"  {k:<22} {m:>14.3f} {s:>10.3f} {cv*100:>6.1f}%{marker}")
    print("\nNote: any metric with CV > 10% will be a noisy signal.")
    print("Consider raising REPS or pinning workload variability before optimizing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
