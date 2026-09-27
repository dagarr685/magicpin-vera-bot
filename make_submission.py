#!/usr/bin/env python3
"""
make_submission.py — produce submission.jsonl for the 30 canonical test pairs.

Run from the project root, AFTER generating the expanded dataset:

    python3 dataset/generate_dataset.py --seed-dir dataset --out expanded
    python3 make_submission.py

Writes submission.jsonl (30 lines) next to this script.
"""

import json
import sys
from pathlib import Path

from composer import build_tick_actions, reset_state

ROOT = Path(__file__).parent
EXPANDED = ROOT / "expanded"


def load_contexts():
    """Load the expanded dataset into the same shape the bot's STORE uses."""
    contexts = {}

    for f in (EXPANDED / "categories").glob("*.json"):
        data = json.load(open(f, encoding="utf-8"))
        contexts[("category", data["slug"])] = {"version": 1, "payload": data}

    for f in (EXPANDED / "merchants").glob("*.json"):
        data = json.load(open(f, encoding="utf-8"))
        contexts[("merchant", data["merchant_id"])] = {"version": 1, "payload": data}

    for f in (EXPANDED / "customers").glob("*.json"):
        data = json.load(open(f, encoding="utf-8"))
        contexts[("customer", data["customer_id"])] = {"version": 1, "payload": data}

    for f in (EXPANDED / "triggers").glob("*.json"):
        data = json.load(open(f, encoding="utf-8"))
        contexts[("trigger", data["id"])] = {"version": 1, "payload": data}

    return contexts


def main():
    if not EXPANDED.exists():
        print("ERROR: expanded/ not found. Run this first:")
        print("  python3 dataset/generate_dataset.py --seed-dir dataset --out expanded")
        sys.exit(1)

    contexts = load_contexts()
    pairs = json.load(open(EXPANDED / "test_pairs.json", encoding="utf-8"))["pairs"]
    print(f"Loaded {len(contexts)} contexts, {len(pairs)} test pairs")

    rows, missing = [], []
    for pair in pairs:
        # Fresh state per pair so suppression never blocks a canonical composition.
        reset_state()
        actions = build_tick_actions([pair["trigger_id"]], contexts)

        if not actions:
            missing.append(pair["test_id"])
            continue

        a = actions[0]
        rows.append({
            "test_id": pair["test_id"],
            "body": a["body"],
            "cta": a["cta"],
            "send_as": a["send_as"],
            "suppression_key": a["suppression_key"],
            "rationale": a["rationale"],
        })

    out = ROOT / "submission.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Wrote {len(rows)} lines to {out}")
    if missing:
        print(f"WARNING: no action produced for {missing} — check those triggers")

    empty = [r["test_id"] for r in rows if not r["body"].strip()]
    if empty:
        print(f"ERROR: empty bodies for {empty}")
        sys.exit(1)
    print("All bodies non-empty. Ready to submit.")


if __name__ == "__main__":
    main()