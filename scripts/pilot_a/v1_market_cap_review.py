#!/usr/bin/env python3
"""V1's market-cap review: the MKT-02 rule measured against the market move, and the refusals.

    uv run python scripts/pilot_a/v1_market_cap_review.py \
        --db /workspaces/thesis/pilot_a/financial.db \
        --market-cap /workspaces/thesis/scratch/t143a/out/p3_v1_market_cap.json \
        --out /workspaces/thesis/scratch/t143a/out/p3_v1_market_cap_review.json

Read-only. Two corrections and one listing, on the output of ``scripts/pilot_a/v1_market_cap.py``:

* **the MKT-02 rule, measured properly.** A split adjusts the price by the ratio and the share
  count by the same ratio, so the cap is continuous *at the same instant*; the cap on the last
  session before the ex-date and on the first session after it differ by the **market move** of that
  day, not by zero. So the rule is: a **split** — the cap ratio equals the stored-close ratio; a
  **spin-off** — the cap ratio equals the stored-close ratio divided by the event's factor (the
  parent's shares do not change, its price drops by the distribution). The closes come from the
  stored ``price_daily``.
* **the refusals that are not K1** — every filing whose cap was refused for a reason other than
  ``no_cover_shares``, named by filing.

Counts, dates, reasons and ratios only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

#: The classes whose factor never applies to the count (MKT-02).
SPLIT_CLASSES = ("split", "reverse_split")
SPINOFF_CLASSES = ("spinoff", "reverse_split_spinoff", "split_spinoff")
#: How far the measured cap ratio may sit from the rule's expectation.
TOLERANCE = 0.02


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--market-cap", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    doc = json.loads(Path(args.market_cap).read_text())
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    asset_of = {
        str(r["ticker"]): int(r["id"]) for r in conn.execute("SELECT id, ticker FROM assets")
    }
    closes: dict[tuple[int, str], float] = {}
    for row in conn.execute(
        "SELECT asset_id, date, close FROM price_daily WHERE close IS NOT NULL"
    ):
        closes[(int(row["asset_id"]), str(row["date"]))] = float(row["close"])

    events: list[dict[str, Any]] = []
    for entry in doc["cap_across_events"]["detail"]:
        asset_id = asset_of.get(entry["ticker"])
        before = closes.get((asset_id, entry["session_before"])) if asset_id else None
        after = closes.get((asset_id, entry["session_after"])) if asset_id else None
        row = dict(entry)
        if before and after and entry.get("cap_ratio") is not None:
            close_ratio = after / before
            expected = (
                close_ratio
                if entry["event_class"] in SPLIT_CLASSES
                else close_ratio / entry["factor"]
            )
            row["close_before"] = before
            row["close_after"] = after
            row["close_ratio"] = round(close_ratio, 6)
            row["expected_cap_ratio"] = round(expected, 6)
            row["ok"] = abs(entry["cap_ratio"] - expected) <= TOLERANCE
            row["rule"] = (
                "the cap ratio equals the stored-close ratio (a split adjusts both)"
                if entry["event_class"] in SPLIT_CLASSES
                else "the cap ratio = the stored-close ratio / the event's factor (a spin-off)"
            )
        events.append(row)
    measured = [e for e in events if e.get("ok") is not None]
    failed = [e for e in measured if not e["ok"]]

    refusals: list[dict[str, Any]] = []
    for entry in doc["refusal_reasons"].get("examples_beyond_no_cover_shares", []):
        if entry.get("reason") not in ("cap_computed", "no_cover_shares"):
            refusals.append(entry)

    out = {
        "events_in_the_window": len(events),
        "measured": len(measured),
        "ok": len(measured) - len(failed),
        "failed": len(failed),
        "not_measured": len(events) - len(measured),
        "failures": failed,
        "not_measured_detail": [e for e in events if e.get("ok") is None],
        "refusals_beyond_no_cover_shares": refusals,
        "refusal_reasons": doc["refusal_reasons"]["reasons"],
        "combined_corporate_action_refusals": doc["combined_event_refusals"][
            "combined_corporate_action_refusals"
        ],
    }
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=True, default=str) + "\n")
    print(
        json.dumps(
            {
                "events": out["events_in_the_window"],
                "measured": out["measured"],
                "ok": out["ok"],
                "failed": out["failed"],
                "not_measured": out["not_measured"],
                "combined_corporate_action_refusals": out["combined_corporate_action_refusals"],
                "refusal_reasons": out["refusal_reasons"],
            },
            indent=1,
            sort_keys=True,
        )
    )
    for row in failed:
        print(
            "  FAIL",
            row["ticker"],
            row["ex_date"],
            row["event_class"],
            row.get("cap_ratio"),
            row.get("expected_cap_ratio"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
