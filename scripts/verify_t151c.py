"""T-151 part 3 verification (fix round 1, G1).

    uv run python scripts/verify_t151c.py provenance --db DB [--engine metrics-v7] --out out/f1_provenance.json

Acceptance (G1): every ``metrics-v7`` row carries a ``provenance`` block; every empty row's
``empty_reason`` is one of the fixed codes; the distribution of reasons per metric (filings /
companies); and one example filing per reason.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import open_ro

from fundamental_agent.metrics import provenance


def _rows(conn: Any, engine: str) -> list[Any]:
    return list(
        conn.execute(
            "SELECT m.filing_id, m.metric_name, m.value, m.inputs_json, f.asset_id, a.ticker, "
            "f.accession_number FROM fundamental_metrics m "
            "JOIN sec_filings f ON f.id = m.filing_id JOIN assets a ON a.id = f.asset_id "
            "WHERE m.engine_version = ?",
            (engine,),
        )
    )


def cmd_provenance(args: argparse.Namespace) -> None:
    conn = open_ro(args.db)
    rows = _rows(conn, args.engine)
    with_block = 0
    empty_without_reason = 0
    bad_reason = 0
    reasons: dict[str, dict[str, set[int]]] = defaultdict(
        lambda: {"filings": set(), "assets": set()}
    )
    examples: dict[str, dict[str, Any]] = {}
    for row in rows:
        inputs = json.loads(row["inputs_json"] or "{}")
        block = inputs.get("provenance")
        if block is None:
            continue
        with_block += 1
        if row["value"] is not None:
            continue
        reason = block.get("empty_reason")
        if reason is None:
            empty_without_reason += 1
            continue
        if reason not in provenance.EMPTY_REASONS:
            bad_reason += 1
            continue
        reasons[reason]["filings"].add(int(row["filing_id"]))
        reasons[reason]["assets"].add(int(row["asset_id"]))
        examples.setdefault(
            reason,
            {
                "ticker": row["ticker"],
                "accession": row["accession_number"],
                "metric": row["metric_name"],
            },
        )
    payload = {
        "rows": len(rows),
        "rows_with_provenance": with_block,
        "rows_without_provenance": len(rows) - with_block,
        "empty_rows_without_reason": empty_without_reason,
        "empty_rows_with_an_unknown_reason": bad_reason,
        "reasons": {
            reason: {"filings": len(v["filings"]), "companies": len(v["assets"])}
            for reason, v in sorted(reasons.items())
        },
        "examples": examples,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"rows {len(rows)}; with provenance {with_block}; without {len(rows) - with_block}")
    print(f"empty rows without a reason {empty_without_reason}; unknown reason {bad_reason}")
    print(f"{'reason':<30}{'filings':>10}{'companies':>12}  example")
    for reason, entry in payload["reasons"].items():
        ex = examples[reason]
        print(
            f"{reason:<30}{entry['filings']:>10}{entry['companies']:>12}  "
            f"{ex['ticker']} {ex['accession']} {ex['metric']}"
        )
    print(f"Wrote {args.out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("provenance")
    p.add_argument("--db", required=True)
    p.add_argument("--engine", default="metrics-v7")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_provenance)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
