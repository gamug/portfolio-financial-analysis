"""Line-item resolution against the SEC: which concept does each stored input come from?

    uv run python scripts/sec_xcheck/itemcheck.py [--db PATH] [--cache DIR] [--tickers A,B]

For each stored ``fundamental_metrics.inputs_json`` value of a checked line item, looks the filing's accession,
period end and duration (a year for a 10-K, a quarter for a 10-Q) up in the SEC ``companyfacts`` cache and
classifies the stored value:

* ``ok_own_concept``  -- equals a fact of one of the item's own registry concepts;
* ``FLIPPED``         -- equals the negative of such a fact (a sign defect, D1);
* ``OTHER_CONCEPT``   -- equals a fact of a concept the registry does not list for the item (D2);
* ``NOT_IN_SEC(rebuilt/derived)`` -- equals no fact of the accession (a derived value, or a custom concept);
* ``no_sec_facts_for_accn`` / ``none`` -- the accession has no facts at the source / the input is absent.

The database is opened read-only. Companyfacts files come from the cache (``audit_sec_checklist.py --fetch``).
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fundamental_agent.statements import REGISTRY

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import (
    SecClient,
    cache_dir,
    cik_family,
    duration_matches,
    filings,
    iter_us_gaap,
    match_value,
    open_ro,
)

CHECK: dict[str, list[str]] = {
    "net_margin": ["revenue", "net_income"],
    "operating_margin": ["operating_income"],
    "effective_tax_rate": ["pretax_income", "income_tax"],
    "return_on_assets": ["total_assets"],
    "return_on_equity": ["equity"],
    "operating_cash_flow_margin": ["operating_cash_flow"],
    "net_debt_to_ebitda": ["depreciation_amortization", "cash"],
}
INSTANT_ITEMS = {"total_assets", "equity", "cash"}


def own_concepts() -> dict[str, set[str]]:
    """Registry item -> its own concept names, without the ``us-gaap_`` prefix."""
    return {
        name: {
            c.split("_", 1)[1]
            for c in (*item.concepts, *item.total_concepts, *item.fallback_concepts)
        }
        for name, item in REGISTRY.items()
    }


def facts_of(
    docs: Iterable[dict[str, Any]], accn: str, end: str, form: str, *, instant: bool
) -> dict[float, set[str]]:
    """``{value: {concepts}}`` of the accession's facts at *end*: instants, or durations of the form's length."""
    out: dict[float, set[str]] = collections.defaultdict(set)
    for doc in docs:
        for _tax, concept, fact in iter_us_gaap(doc):
            if fact["accn"] != accn or fact["end"] != end:
                continue
            if (
                (fact.get("start") is None)
                if instant
                else duration_matches(form, fact.get("start"), end)
            ):
                out[fact["val"]].add(concept)
    return out


def classify(stored: float | None, own: set[str], values: dict[float, set[str]]) -> tuple[str, Any]:
    """Class of one stored input against the SEC facts of its accession and period (see the module docstring)."""
    if stored is None:
        return "none", None
    hit = [cons for v, cons in values.items() if match_value(v, stored)]
    flip = [cons for v, cons in values.items() if match_value(v, -stored)]
    if any(own & cs for cs in hit):
        return "ok_own_concept", None
    if any(own & cs for cs in flip):
        return "FLIPPED", "flip"
    if hit:
        return "OTHER_CONCEPT", sorted(set().union(*hit))[:3]
    if not values:
        return "no_sec_facts_for_accn", None
    return "NOT_IN_SEC(rebuilt/derived)", "derived"


def run(
    conn: Any, client: SecClient, tickers: set[str] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    own = own_concepts()
    results: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    examples: dict[str, list[tuple[Any, ...]]] = collections.defaultdict(list)
    for f in filings(conn):
        if tickers and f["ticker"] not in tickers:
            continue
        docs = [d for c in cik_family(f["cik"]) if (d := client.get("companyfacts", c))]
        dur = facts_of(docs, f["accession_number"], f["period_end"], f["form"], instant=False)
        ins = facts_of(docs, f["accession_number"], f["period_end"], f["form"], instant=True)
        for metric, keys in CHECK.items():
            row = conn.execute(
                "SELECT inputs_json FROM fundamental_metrics WHERE filing_id = ? AND metric_name = ?",
                (f["id"], metric),
            ).fetchone()
            if not row or not row["inputs_json"]:
                continue
            inputs = json.loads(row["inputs_json"])
            for key in keys:
                values = ins if key in INSTANT_ITEMS else dur
                cls, extra = classify(inputs.get(key), own.get(key, set()), values)
                results[key][cls] += 1
                if cls in ("FLIPPED", "OTHER_CONCEPT", "NOT_IN_SEC(rebuilt/derived)"):
                    examples[key].append(
                        (f["ticker"], f["form"], f["period_end"], inputs.get(key), extra)
                    )
    return results, examples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--cache", help="SEC cache directory (SEC_XCHECK_CACHE)")
    parser.add_argument("--tickers", help="comma-separated tickers to restrict to")
    args = parser.parse_args(argv)
    conn = open_ro(args.db)
    client = SecClient(cache_dir(args.cache), offline=True)
    tickers = set(args.tickers.split(",")) if args.tickers else None
    results, examples = run(conn, client, tickers)
    for key, counts in results.items():
        print(key, dict(counts))
        for ex in examples[key][:5]:
            print("    ", ex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
