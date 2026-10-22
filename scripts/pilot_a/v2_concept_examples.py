#!/usr/bin/env python3
"""V2: the itemcheck examples, grouped by class (the concept differences the report must name).

    uv run python scripts/pilot_a/v2_concept_examples.py \
        --db /workspaces/thesis/pilot_a/financial.db \
        --cache /home/vscode/pilot_a_run/sec_cache \
        --out /workspaces/thesis/scratch/t143a/out/p3_v2_concept_examples.json

Read-only. ``scripts/sec_xcheck/itemcheck.py`` prints only the first five examples per line item,
mixed across classes; the plan's V2 rule names the items whose *resolved concept* must match the
SEC's (income tax, equity, net income, short-term debt, D&A, capex, operating income), so the
examples of each class are needed separately. This script re-runs the same comparison and prints
every example per class.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from sec_xcheck.common import cache_dir, open_ro
from sec_xcheck.itemcheck import run

#: The index of the class in an example row (ticker, form, period_end, value, class, extra).
CLASS_INDEX = 4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    conn = open_ro(args.db)
    client = cache_dir(args.cache)
    from sec_xcheck.common import SecClient  # noqa: PLC0415

    sec = SecClient(client, offline=True)
    results, examples = run(conn, sec, None)
    grouped: dict[str, dict[str, list[list[object]]]] = defaultdict(lambda: defaultdict(list))
    for item, rows in examples.items():
        for row in rows:
            # The example row is (ticker, form, period_end, stored value, extra); ``extra`` names
            # the class: a list of concepts for OTHER_CONCEPT, "derived" for a rebuilt value,
            # "flip" for a sign flip.
            extra = row[CLASS_INDEX] if len(row) > CLASS_INDEX else None
            if extra == "derived":
                klass = "NOT_IN_SEC(rebuilt/derived)"
            elif extra == "flip":
                klass = "FLIPPED"
            elif isinstance(extra, list):
                klass = "OTHER_CONCEPT"
            else:
                klass = "unknown"
            grouped[item][klass].append(list(row))
    doc = {
        "counts": {item: dict(counts) for item, counts in results.items()},
        "examples_by_class": {
            item: {cls: rows[:40] for cls, rows in classes.items()}
            for item, classes in grouped.items()
        },
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    for item, classes in doc["examples_by_class"].items():
        for cls, rows in classes.items():
            print(f"{item} [{cls}]: {len(rows)} examples")
            for row in rows[:6]:
                print("   ", row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
