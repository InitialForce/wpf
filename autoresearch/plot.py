#!/usr/bin/env python3
"""Plot autoresearch progress from results.jsonl.

Generates a single multi-panel SVG (no external deps beyond matplotlib):
- Composite z over iterations, with KEEP/REVERT decision markers
- Each individual metric (alloc_bytes, render_total_ms, gc_max_pause_ms,
  frame_p95_ms) over iterations, with per-rep variance bands
- Per-iteration std-dev (so you can see when the signal degrades into noise)

Usage:  python3 plot.py [output.svg]
        Defaults to plot.svg in the same directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
JSONL = ROOT / "results.jsonl"
BASELINE_PATH = ROOT / "baseline.json"

METRIC_KEYS = ["alloc_bytes", "render_total_ms", "gc_max_pause_ms", "frame_p95_ms"]
DECISION_COLOURS = {
    "KEEP": "#1a9850",
    "REVERT": "#d73027",
    "REJECT-PARETO": "#fc8d59",
    "BUILD-FAIL": "#999999",
    "SPIKE-FAIL": "#bbbbbb",
}


def main(out: Path) -> int:
    if not JSONL.is_file():
        print("no results.jsonl yet", file=sys.stderr)
        return 1

    rows = [json.loads(line) for line in JSONL.open() if line.strip()]
    if not rows:
        print("results.jsonl is empty", file=sys.stderr)
        return 1

    baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.is_file() else None

    iters = [r["iter"] for r in rows]
    decisions = [r["decision"] for r in rows]
    z_vals = [r.get("z", float("nan")) for r in rows]

    fig, axes = plt.subplots(3, 2, figsize=(14, 10), sharex=False)
    fig.suptitle(f"WPF Autoresearch — {len(rows)} iterations  "
                 f"({sum(1 for d in decisions if d == 'KEEP')} kept)")

    # Panel 1 (top-left): composite z over time.
    ax = axes[0][0]
    ax.set_title("Composite z (lower is better)")
    ax.set_xlabel("iteration")
    ax.set_ylabel("z")
    ax.axhline(0, color="#666", linewidth=0.5, linestyle="--", label="baseline")
    for r, z in zip(rows, z_vals):
        ax.scatter(r["iter"], z, color=DECISION_COLOURS.get(r["decision"], "#000"),
                   s=20, label=r["decision"] if r["iter"] == iters[0] else None)
    # Connect kept iterations with a line for clarity.
    keep_iters = [r["iter"] for r in rows if r["decision"] == "KEEP"]
    keep_z = [r["z"] for r in rows if r["decision"] == "KEEP"]
    if keep_iters:
        ax.plot(keep_iters, keep_z, color=DECISION_COLOURS["KEEP"],
                linewidth=1, alpha=0.6)
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)

    # Panel 2 (top-right): decision histogram (small).
    ax = axes[0][1]
    ax.set_title("Decisions")
    counts = {}
    for d in decisions:
        counts[d] = counts.get(d, 0) + 1
    ks = list(counts.keys())
    vs = [counts[k] for k in ks]
    cs = [DECISION_COLOURS.get(k, "#000") for k in ks]
    ax.bar(ks, vs, color=cs)
    ax.set_ylabel("count")
    ax.tick_params(axis="x", rotation=20)

    # Panels 3-6: each individual metric, with per-rep variance band.
    panel_positions = [(1, 0), (1, 1), (2, 0), (2, 1)]
    for (row_i, col_i), key in zip(panel_positions, METRIC_KEYS):
        ax = axes[row_i][col_i]
        ax.set_title(key)
        ax.set_xlabel("iteration")
        ax.set_ylabel(key)
        if baseline is not None:
            b_med = baseline["medians"][key]
            ax.axhline(b_med, color="#666", linewidth=0.5,
                       linestyle="--", label="baseline median")

        # Per-iteration: median, with lo/hi error bars from per-rep min/max.
        for r in rows:
            if "medians" not in r or "per_rep" not in r:
                continue
            med = r["medians"].get(key)
            if med is None:
                continue
            reps = [rep[key] for rep in r["per_rep"] if key in rep]
            if reps:
                lo, hi = min(reps), max(reps)
                colour = DECISION_COLOURS.get(r["decision"], "#000")
                ax.errorbar(r["iter"], med, yerr=[[med - lo], [hi - med]],
                            fmt="o", color=colour, markersize=3, alpha=0.6,
                            elinewidth=0.5, capsize=2)

        # Connect kept iterations
        keep_x = [r["iter"] for r in rows
                  if r["decision"] == "KEEP" and "medians" in r and key in r["medians"]]
        keep_y = [r["medians"][key] for r in rows
                  if r["decision"] == "KEEP" and "medians" in r and key in r["medians"]]
        if keep_x:
            ax.plot(keep_x, keep_y, color=DECISION_COLOURS["KEEP"],
                    linewidth=1, alpha=0.5)
        ax.legend(loc="best", fontsize=8)
        ax.grid(True, alpha=0.3)

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out)
    print(f"wrote {out}  ({len(rows)} iterations)")
    return 0


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "plot.svg"
    sys.exit(main(out))
