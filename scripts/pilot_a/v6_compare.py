#!/usr/bin/env python3
"""V6: the fresh database's numbers against the copies' (the plan's "compare each number").

    uv run python scripts/pilot_a/v6_compare.py \
        --fresh-dir /workspaces/thesis/scratch/t143a/out \
        --copies /workspaces/thesis/scratch/v6_copies \
        --out /workspaces/thesis/scratch/t143a/out/p3_v6_comparison.json

Read-only. The copies hold the outputs the earlier tasks measured on their own database
(``/workspaces/thesis/scratch/v6_copies/<task>/out``); the fresh numbers come from the pilot
database. For every measurement that both sides have, the comparison prints the numbers side by side
and names the rows that differ -- the evidence the plan wants for "a decision whose evidence changes
materially". Counts, tickers and ratios only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def d13(fresh_dir: Path, copies: Path) -> dict[str, Any]:
    """D-13, the utilities' 5% check (T-071 part 2a)."""
    fresh = _load(fresh_dir / "p2b_v6_verify_t071b_d13.json")
    copy = _load(copies / "t071b" / "out" / "d13_utilities.json")
    if not fresh or not copy:
        return {"compared": False}
    fresh_rows = {r["ticker"]: r for r in fresh.get("table", [])}
    copy_rows = {r["ticker"]: r for r in copy.get("table", [])}
    changed = []
    for ticker in sorted(set(fresh_rows) | set(copy_rows)):
        f, c = fresh_rows.get(ticker), copy_rows.get(ticker)
        if f is None or c is None:
            changed.append(
                {
                    "ticker": ticker,
                    "fresh": "absent" if f is None else "present",
                    "copy": "absent" if c is None else "present",
                }
            )
            continue
        fy = {y["period_end"]: y for y in f.get("years", [])}
        cy = {y["period_end"]: y for y in c.get("years", [])}
        for period in sorted(set(fy) | set(cy)):
            a, b = fy.get(period), cy.get(period)
            if a is None or b is None:
                changed.append(
                    {
                        "ticker": ticker,
                        "period_end": period,
                        "fresh": a is not None,
                        "copy": b is not None,
                    }
                )
                continue
            for key in ("ratio_ambiguous_in", "ratio_ambiguous_as_wc", "cfo", "debt"):
                if a.get(key) != b.get(key):
                    changed.append(
                        {
                            "ticker": ticker,
                            "period_end": period,
                            "field": key,
                            "fresh": a.get(key),
                            "copy": b.get(key),
                        }
                    )
    return {
        "compared": True,
        "fresh": {
            "cycle_date": fresh.get("cycle_date"),
            "utilities": fresh.get("utilities"),
            "with_both_bounds": fresh.get("with_both_bounds"),
            "below_5_on_both_bounds": fresh.get("below_5_on_both_bounds"),
        },
        "copy": {
            "cycle_date": copy.get("cycle_date"),
            "utilities": copy.get("utilities"),
            "with_both_bounds": copy.get("with_both_bounds"),
            "below_5_on_both_bounds": copy.get("below_5_on_both_bounds"),
        },
        "differing_rows": len(changed),
        "differences": changed[:40],
    }


def multi_class(fresh_dir: Path, copies: Path) -> dict[str, Any]:
    """The multi-class measure (T-071 part 2b)."""
    fresh = _load(fresh_dir / "p2b_v6_verify_t071c_multi-class.json")
    copy = _load(copies / "t071c" / "out" / "multi_class.json")
    if not fresh or not copy:
        return {"compared": False}

    def flags(doc: Any) -> dict[str, dict[str, Any]]:
        rows = doc.get("rows") if isinstance(doc, dict) else None
        if rows is None:
            rows = doc if isinstance(doc, dict) else {}
        return {
            str(cik): {
                "ticker": r.get("ticker"),
                "cover_multi_class": r.get("cover_multi_class"),
                "cover_n_common_classes": r.get("cover_n_common_classes"),
            }
            for cik, r in rows.items()
        }

    f, c = flags(fresh), flags(copy)
    changed = []
    for cik in sorted(set(f) | set(c)):
        if f.get(cik) != c.get(cik):
            changed.append({"cik": cik, "fresh": f.get(cik), "copy": c.get(cik)})
    return {
        "compared": True,
        "fresh_rows": len(f),
        "copy_rows": len(c),
        "fresh_cover_multi_class": sum(1 for v in f.values() if v.get("cover_multi_class")),
        "copy_cover_multi_class": sum(1 for v in c.values() if v.get("cover_multi_class")),
        "differing_rows": len(changed),
        "differences": changed[:40],
    }


def reit_capex(fresh_dir: Path, copies: Path) -> dict[str, Any]:
    """D-14, the REIT capex table (T-071 part 2b)."""
    fresh = _load(fresh_dir / "p2b_v6_verify_t071c_reit-capex.json")
    copy = _load(copies / "t071c" / "out" / "reit_capex.json")
    if not fresh or not copy:
        return {"compared": False}
    f = fresh.get("companies") or {}
    c = copy.get("companies") or copy.get("rows") or {}
    changed = []
    for ticker in sorted(set(f) | set(c)):
        a, b = f.get(ticker), c.get(ticker)
        if a is None or b is None:
            changed.append({"ticker": ticker, "fresh": a is not None, "copy": b is not None})
            continue
        for key in ("capex", "operating_cash_flow", "fcf", "terms"):
            if a.get(key) != b.get(key):
                changed.append(
                    {"ticker": ticker, "field": key, "fresh": a.get(key), "copy": b.get(key)}
                )
    return {
        "compared": True,
        "fresh_companies": len(f),
        "copy_companies": len(c),
        "differing_rows": len(changed),
        "differences": changed[:40],
    }


#: The measurements both sides have: the fresh output and the copy's output for the same
#: measurement (the copies' own databases are not in this checkout, only their outputs).
PAIRS: tuple[tuple[str, str, str], ...] = (
    ("verify_t073/status", "p2b_v6_verify_t073_status.json", "t073/out/pillar_status.json"),
    ("verify_t073/unscored", "p2b_v6_verify_t073_unscored.json", "t073/out/unscored.json"),
    (
        "verify_t073/fallback",
        "p2b_v6_verify_t073_fallback.json",
        "t073/out/fallback_distribution.json",
    ),
    ("verify_t073/weights", "p2b_v6_verify_t073_weights.json", "t073/out/weights_sensitivity.json"),
    (
        "verify_t071c/book-equity",
        "p2b_v6_verify_t071c_book-equity.json",
        "t071c/out/book_equity.json",
    ),
    ("verify_t071c/dq02", "p2b_v6_verify_t071c_dq02.json", "t071c/out/dq02.json"),
    ("verify_t071c/d14", "p2b_v6_verify_t071c_d14.json", "t071c/out/d14_reits_v2.json"),
    (
        "verify_t151c/provenance",
        "p2b_v6_verify_t151c_provenance.json",
        "t151c/out/f3_provenance.json",
    ),
    ("verify_t151b/roa", "p2b_v6_verify_t151b_roa.json", "t151b/out/f1_roa_breakdown.json"),
    ("verify_t155/measure", "p2b_v6_verify_t155_measure.json", "t155/out/p3_measure.json"),
)


def _scalars(doc: Any) -> dict[str, Any]:
    """The numbers (and the short strings) a JSON document states at its top level."""
    if not isinstance(doc, dict):
        return {}
    return {
        key: value
        for key, value in doc.items()
        if isinstance(value, (int, float, str)) and not isinstance(value, bool)
    }


def scalars_pair(fresh_dir: Path, copies: Path) -> dict[str, Any]:
    """Every measurement both sides have: the numbers side by side, and the ones that differ."""
    compared: dict[str, Any] = {}
    for name, fresh_name, copy_name in PAIRS:
        fresh = _load(fresh_dir / fresh_name)
        copy = _load(copies / copy_name)
        if not fresh or not copy:
            compared[name] = {"compared": False}
            continue
        f, c = _scalars(fresh), _scalars(copy)
        changed = [
            {"field": key, "fresh": f.get(key), "copy": c.get(key)}
            for key in sorted(set(f) | set(c))
            if f.get(key) != c.get(key)
        ]
        compared[name] = {
            "compared": True,
            "fresh": f,
            "copy": c,
            "differing_fields": len(changed),
            "differences": changed[:40],
        }
    return compared


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh-dir", required=True)
    parser.add_argument("--copies", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    fresh_dir = Path(args.fresh_dir)
    copies = Path(args.copies)
    doc = {
        "d13_utilities": d13(fresh_dir, copies),
        "multi_class": multi_class(fresh_dir, copies),
        "reit_capex": reit_capex(fresh_dir, copies),
        "scalars": scalars_pair(fresh_dir, copies),
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(doc, indent=1, sort_keys=True, default=str)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
