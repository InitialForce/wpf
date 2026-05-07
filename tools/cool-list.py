#!/usr/bin/env python3
"""cool-list.py — read-only cooldown-state inspector for autoresearch/results.jsonl.

Usage:
    python3 tools/cool-list.py [results_jsonl_path] [--all] [--threshold N]

Exits 0 on success, 2 if the file is missing.
"""

import argparse
import json
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RESULTS = os.path.join(REPO_ROOT, "autoresearch", "results.jsonl")


def parse_args():
    p = argparse.ArgumentParser(
        description="Show per-filter cooldown state from results.jsonl"
    )
    p.add_argument(
        "results",
        nargs="?",
        default=DEFAULT_RESULTS,
        help="Path to results.jsonl (default: autoresearch/results.jsonl)",
    )
    p.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help="Show full verdict history per filter below the table",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=5,
        metavar="N",
        help="Cooldown duration in tier-B rows (default: 5)",
    )
    return p.parse_args()


def load_tier_b_rows(path):
    """Read results.jsonl and return all tier=='B' rows as a list of dicts,
    in file order.  Also returns the list of ALL rows (any tier) for
    context, though cooldown logic only needs tier-B rows."""
    if not os.path.exists(path):
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(2)

    all_rows = []
    tier_b = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(
                    f"warning: skipping malformed JSON on line {lineno}: {exc}",
                    file=sys.stderr,
                )
                continue
            all_rows.append(obj)
            if obj.get("tier") == "B":
                tier_b.append(obj)

    return tier_b


def compute_cooldown(tier_b_rows, threshold):
    """Compute per-filter cooldown state.

    Algorithm (from design doc §1 "Query algorithm"):
    1. Parse all tier-B rows in order.
    2. For each unique filter, find the two most recent rows.
    3. If both are REJECT-UNCLEAR:
       a. Find the tier-B index (0-based) of the second-most-recent REJECT-UNCLEAR.
       b. Count tier-B rows written AFTER that index.
       c. If count < threshold, the filter is ON COOLDOWN.

    Returns a dict:  filter_value -> {
        'cooled': bool,
        'last2': [verdict, verdict],   # most-recent first, '-' if fewer than 2
        'rows_since': int or None,      # None if not cooled
        'eligible_after_row': int or None,  # None if not cooled
        'trigger_b_index': int or None,     # 0-based index in tier_b_rows
        'history': [verdict, ...],          # all verdicts in chron order
    }
    """
    # Group by filter, recording (tier_b_index, verdict) pairs in order.
    filter_indices = {}  # filter -> list of (tier_b_index, verdict)
    for i, row in enumerate(tier_b_rows):
        filt = row.get("filter", "<no-filter>")
        verdict = row.get("verdict", "<unknown>")
        filter_indices.setdefault(filt, []).append((i, verdict))

    total_b = len(tier_b_rows)
    results = {}

    for filt, entries in filter_indices.items():
        history = [v for _, v in entries]
        last2_entries = entries[-2:] if len(entries) >= 2 else entries[:]
        last2 = [v for _, v in last2_entries]

        # Pad to 2 for display, most-recent first
        if len(last2) == 2:
            display_last2 = [last2[1], last2[0]]  # most-recent first
        elif len(last2) == 1:
            display_last2 = [last2[0], "-"]
        else:
            display_last2 = ["-", "-"]

        # Check cooldown trigger: both of the two most recent are REJECT-UNCLEAR
        cooled = False
        rows_since = None
        eligible_after_row = None
        trigger_b_index = None

        if len(entries) >= 2:
            idx_second_last, v_second_last = entries[-2]
            idx_last, v_last = entries[-1]
            if v_last == "REJECT-UNCLEAR" and v_second_last == "REJECT-UNCLEAR":
                # Rows written AFTER the second-most-recent REJECT-UNCLEAR
                rows_since = total_b - 1 - idx_second_last
                if rows_since < threshold:
                    cooled = True
                    trigger_b_index = idx_second_last
                    # eligible after row: the (threshold)th tier-B row after trigger
                    eligible_after_row = idx_second_last + threshold  # 0-based

        results[filt] = {
            "cooled": cooled,
            "last2": display_last2,
            "rows_since": rows_since,
            "eligible_after_row": eligible_after_row,
            "trigger_b_index": trigger_b_index,
            "history": history,
        }

    return results


def print_table(cooldown_map):
    """Print the summary table."""
    col_filter = 32
    col_status = 10
    col_last2 = 40
    col_rows_since = 12
    col_eligible = 20

    header = (
        f"{'FILTER':<{col_filter}}"
        f"{'STATUS':<{col_status}}"
        f"{'LAST 2 VERDICTS':<{col_last2}}"
        f"{'ROWS-SINCE':<{col_rows_since}}"
        f"{'ELIGIBLE-AFTER-ROW':<{col_eligible}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)

    # Sort: cooled first (most rows-since descending), then eligible
    def sort_key(item):
        filt, info = item
        if info["cooled"]:
            return (0, -(info["rows_since"] or 0))
        return (1, 0)

    for filt, info in sorted(cooldown_map.items(), key=sort_key):
        status = "COOLED" if info["cooled"] else "eligible"
        last2_str = ", ".join(info["last2"])
        rows_since_str = str(info["rows_since"]) if info["rows_since"] is not None else "-"
        eligible_str = (
            str(info["eligible_after_row"]) if info["eligible_after_row"] is not None else "-"
        )
        print(
            f"{filt:<{col_filter}}"
            f"{status:<{col_status}}"
            f"{last2_str:<{col_last2}}"
            f"{rows_since_str:<{col_rows_since}}"
            f"{eligible_str:<{col_eligible}}"
        )


def print_full_history(cooldown_map):
    """Print per-filter full verdict history below the table."""
    print()
    print("=== Full verdict history per filter ===")
    for filt, info in sorted(cooldown_map.items()):
        print()
        print(f"Filter: {filt}")
        history = info["history"]
        if history:
            for i, v in enumerate(history, 1):
                print(f"  [{i:3d}] {v}")
        else:
            print("  (no tier-B rows)")


def main():
    args = parse_args()
    tier_b_rows = load_tier_b_rows(args.results)

    if not tier_b_rows:
        print("No tier-B rows found in results.jsonl.")
        return

    cooldown_map = compute_cooldown(tier_b_rows, args.threshold)

    print(f"Results: {args.results}")
    print(f"Tier-B rows: {len(tier_b_rows)}  |  Threshold: {args.threshold}")
    print()
    print_table(cooldown_map)

    if args.show_all:
        print_full_history(cooldown_map)


if __name__ == "__main__":
    main()
