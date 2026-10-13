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
from fundamental_agent.metrics.provenance import OPTIONAL


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


def _reason_counts(rows: list[Any], predicate: Any) -> dict[str, dict[str, int]]:
    """``{reason: {rows, filings, companies}}`` over the rows *predicate* selects."""
    out: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"rows": 0, "filings": set(), "assets": set()}
    )
    for row in rows:
        inputs = json.loads(row["inputs_json"] or "{}")
        block = inputs.get("provenance")
        if block is None or row["value"] is not None:
            continue
        if not predicate(row, inputs, block):
            continue
        reason = block.get("empty_reason") or "(none)"
        out[reason]["rows"] += 1
        out[reason]["filings"].add(int(row["filing_id"]))
        out[reason]["assets"].add(int(row["asset_id"]))
    return {
        reason: {
            "rows": v["rows"],
            "filings": len(v["filings"]),
            "companies": len(v["assets"]),
        }
        for reason, v in sorted(out.items())
    }


def cmd_reasons(args: argparse.Namespace) -> None:  # noqa: C901 - one predicate per population
    """The H1-H3 and H7 populations (fix round 2)."""
    conn = open_ro(args.db)
    rows = _rows(conn, args.engine)
    payload: dict[str, Any] = {}

    def h1(row: Any, inputs: dict[str, Any], block: dict[str, Any]) -> bool:
        return any("debt_sum_refused_other_lines" in key for key in inputs)

    def h2(row: Any, inputs: dict[str, Any], block: dict[str, Any]) -> bool:
        """The plan's population: **every item is filed**, yet a TTM item was not formed."""
        unformed = any(
            entry.get("basis") == "ttm" and entry.get("method") in ("x4", "refused")
            for entries in (block.get("items") or {}).values()
            for entry in entries
        )
        if not unformed:
            return False
        optional = OPTIONAL.get(row["metric_name"], frozenset())
        for item, entries in (block.get("items") or {}).items():
            if item in optional:
                continue
            for entry in entries:
                if entry.get("basis") in ("period", "ttm") and entry.get("concept") is None:
                    return False  # another item is not filed: not this population
        return True

    def h3(row: Any, inputs: dict[str, Any], block: dict[str, Any]) -> bool:
        return bool(inputs.get("flag_enterprise_fcf_yield_prior_wc_missing"))

    def h7(row: Any, inputs: dict[str, Any], block: dict[str, Any]) -> bool:
        return bool(inputs.get("flag_nopat_tax_rate_outside_unit_interval"))

    for label, predicate, metrics in (
        (
            "H1_debt_refused",
            h1,
            ("debt_to_equity", "debt_to_assets", "enterprise_value", "net_debt_to_ebitda"),
        ),
        ("H2_ttm_not_formed", h2, None),
        ("H3_prior_wc_missing", h3, ("enterprise_fcf_yield",)),
        ("H7_negative_reit_rate_roic", h7, ("return_on_invested_capital",)),
        ("H7_negative_reit_rate_all", h7, None),
    ):
        selected = [r for r in rows if metrics is None or r["metric_name"] in metrics]
        payload[label] = _reason_counts(selected, predicate)

    Path(args.out).write_text(json.dumps(payload, indent=2))
    for label, counts in payload.items():
        total_rows = sum(entry["rows"] for entry in counts.values())
        total_filings = len({f for entry in counts.values() for f in [entry["filings"]]})
        print(f"\n{label}: {total_rows} rows")
        for reason, entry in counts.items():
            print(
                f"  {reason:<30}{entry['rows']:>7} rows {entry['filings']:>7} filings "
                f"{entry['companies']:>6} companies"
            )
        print(f"  total {total_filings} filings (distinct per reason)")
    print(f"\nWrote {args.out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("provenance")
    p.add_argument("--db", required=True)
    p.add_argument("--engine", default="metrics-v7")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_provenance)
    p = sub.add_parser("reasons")
    p.add_argument("--db", required=True)
    p.add_argument("--engine", default="metrics-v7")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_reasons)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
