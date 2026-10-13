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
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.metrics.provenance import OPTIONAL


def _rows(conn: Any, engine: str) -> list[Any]:
    return list(
        conn.execute(
            "SELECT m.filing_id, m.metric_name, m.value, m.inputs_json, f.asset_id, a.ticker, "
            "f.accession_number, f.form FROM fundamental_metrics m "
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


def _j1_block(rows: list[Any]) -> dict[str, Any]:
    """J1: for the metrics whose `capture_failure` came from a sibling flag, the reason
    distribution now and the own flag that explains each remaining `capture_failure`."""
    # J1: the metrics whose `capture_failure` came from a sibling's flag (the reviewer's v3 table).
    # For each, the reason distribution now, and for the `capture_failure` rows the own flag that
    # explains them (expected: nothing left from the sibling flags).
    j1_metrics = (
        "gross_margin",
        "enterprise_value",
        "free_cash_flow_yield",
        "sbc_adjusted_fcf_yield",
        "effective_tax_rate",
        "return_on_equity",
        "interest_coverage",
        "ebitda",
        "market_capitalization",
        "operating_margin",
        "net_margin",
    )
    j1: dict[str, Any] = {}
    for metric in j1_metrics:
        per_reason: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"rows": 0, "filings": set(), "assets": set(), "example": None}
        )
        own_flags: dict[str, int] = defaultdict(int)
        sibling_only = 0
        for row in rows:
            if row["metric_name"] != metric or row["value"] is not None:
                continue
            inputs = json.loads(row["inputs_json"] or "{}")
            block = inputs.get("provenance")
            if block is None:
                continue
            reason = block.get("empty_reason") or "(none)"
            entry = per_reason[reason]
            entry["rows"] += 1
            entry["filings"].add(int(row["filing_id"]))
            entry["assets"].add(int(row["asset_id"]))
            if entry["example"] is None:
                entry["example"] = f"{row['ticker']} {row['accession_number']}"
            if reason == "capture_failure":
                items = tuple(block.get("items") or {})
                result = MetricResult(metric, None, "ratio", inputs)
                own = [
                    key
                    for key in provenance._candidate_keys(result)
                    if provenance._is_own_flag(key, result, items)
                    and provenance._flag_reason([key]) == provenance.CAPTURE_FAILURE
                ]
                # an own *item* explains it too: a refused/x4 TTM (H2) or a refusal flag on one of
                # the metric's own item resolutions
                own_item = any(
                    (entry.get("basis") == "ttm" and entry.get("method") in ("x4", "refused"))
                    or any(
                        provenance._flag_reason([flag]) == provenance.CAPTURE_FAILURE
                        for flag in entry.get("flags") or []
                    )
                    for entries in (block.get("items") or {}).values()
                    for entry in entries
                )
                if own:
                    own_flags[own[0]] += 1
                elif own_item:
                    own_flags["(an own item's resolution)"] += 1
                else:
                    sibling_only += 1
        j1[metric] = {
            "reasons": {
                reason: {
                    "rows": v["rows"],
                    "filings": len(v["filings"]),
                    "companies": len(v["assets"]),
                    "example": v["example"],
                }
                for reason, v in sorted(per_reason.items())
            },
            "capture_failure_own_flags": dict(sorted(own_flags.items())),
            "capture_failure_without_an_own_flag": sibling_only,
        }
    return j1


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

    # H7 is a flag count, not an empty-reason population: every row that carries it, whether the
    # metric is computed or empty, by form.
    h7_rows = [
        row
        for row in rows
        if row["metric_name"] == "return_on_invested_capital"
        and json.loads(row["inputs_json"] or "{}").get("flag_nopat_tax_rate_outside_unit_interval")
    ]
    h7_forms = {
        form: {
            "rows": sum(1 for r in h7_rows if r["form"] == form),
            "computed": sum(1 for r in h7_rows if r["form"] == form and r["value"] is not None),
            "filings": len({int(r["filing_id"]) for r in h7_rows if r["form"] == form}),
            "companies": len({int(r["asset_id"]) for r in h7_rows if r["form"] == form}),
        }
        for form in sorted({r["form"] for r in h7_rows})
    }
    payload["H7_rows"] = {
        "total": {
            "rows": len(h7_rows),
            "computed": sum(1 for r in h7_rows if r["value"] is not None),
            "filings": len({int(r["filing_id"]) for r in h7_rows}),
            "companies": len({int(r["asset_id"]) for r in h7_rows}),
        },
        "by_form": h7_forms,
        "tickers": sorted({r["ticker"] for r in h7_rows}),
    }

    j1 = _j1_block(rows)

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

    payload["J1"] = j1
    print("\nJ1 (empty rows per metric, after):")
    for metric, entry in j1.items():
        parts = ", ".join(
            f"{reason} {v['rows']}/{v['companies']} ({v['example']})"
            for reason, v in entry["reasons"].items()
        )
        own = ", ".join(f"{k} {v}" for k, v in entry["capture_failure_own_flags"].items())
        print(f"  {metric:<32}{parts}")
        print(
            f"    capture_failure own flags: {own or '-'}; without an own flag: "
            f"{entry['capture_failure_without_an_own_flag']}"
        )
    h7 = payload.pop("H7_rows")
    print(f"\nH7_rows: {h7['total']}")
    for form, entry in h7["by_form"].items():
        print(f"  {form:<6}{entry}")
    print(f"  tickers {h7['tickers']}")
    payload["H7_rows"] = h7
    for label, counts in payload.items():
        if label in ("H7_rows", "J1"):
            continue
        total_rows = sum(entry["rows"] for entry in counts.values())
        total_filings = len({f for entry in counts.values() for f in [entry["filings"]]})
        print(f"\n{label}: {total_rows} rows")
        for reason, entry in counts.items():
            print(
                f"  {reason:<30}{entry['rows']:>7} rows {entry['filings']:>7} filings "
                f"{entry['companies']:>6} companies"
            )
        print(f"  total {total_filings} filings (distinct per reason)")
    Path(args.out).write_text(json.dumps(payload, indent=2))
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
