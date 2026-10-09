"""Sign check against the SEC: is a stored input the filed value, or its negative (D1)?

    uv run python scripts/sec_xcheck/signcheck.py [--db PATH] [--cache DIR]

For the signed inputs the audit tracks (income tax, cost of goods sold, interest expense, capital expenditure),
compares the value in ``fundamental_metrics.inputs_json`` with the SEC ``companyfacts`` value of the same
accession and period: ``same_sign``, ``FLIPPED`` (equal to the negative), ``other_value``. Read-only.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import (
    SecClient,
    cache_dir,
    cik_family,
    duration_matches,
    iter_us_gaap,
    match_value,
    open_ro,
)

CHECKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("effective_tax_rate", "income_tax", ("IncomeTaxExpenseBenefit",)),
    (
        "inventory_turnover",
        "cogs",
        ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "CostOfServices"),
    ),
    (
        "interest_coverage",
        "interest_expense",
        ("InterestExpense", "InterestExpenseNonoperating", "InterestAndDebtExpense"),
    ),
    (
        "free_cash_flow_margin",
        "capital_expenditure",
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsForCapitalImprovements",
            "PaymentsToExploreAndDevelopOilAndGasProperties",
            "PaymentsToAcquireOilAndGasPropertyAndEquipment",
            "PaymentsToAcquireOilAndGasProperty",
        ),
    ),
)


def sec_values(
    docs: list[dict[str, Any]], concepts: tuple[str, ...], accn: str, end: str, form: str
) -> list[float]:
    """The accession's duration values of *concepts* at *end* (a year for a 10-K, a quarter for a 10-Q)."""
    wanted = set(concepts)
    return [
        fact["val"]
        for doc in docs
        for _tax, concept, fact in iter_us_gaap(doc)
        if concept in wanted
        and fact["accn"] == accn
        and fact["end"] == end
        and duration_matches(form, fact.get("start"), end)
    ]


def sign_class(stored: float, secs: list[float]) -> str:
    if any(match_value(s, stored) for s in secs):
        return "same_sign"
    if any(match_value(s, -stored) for s in secs):
        return "FLIPPED"
    return "other_value"


def check(
    conn: Any, client: SecClient, metric: str, key: str, concepts: tuple[str, ...]
) -> dict[Any, int]:
    rows = conn.execute(
        "SELECT a.ticker, a.cik, f.form, f.period_end, f.accession_number, m.value, m.inputs_json "
        "FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
        "JOIN assets a ON a.id = f.asset_id WHERE m.metric_name = ?",
        (metric,),
    ).fetchall()
    classes: collections.Counter[Any] = collections.Counter()
    examples: dict[str, list[tuple[Any, ...]]] = collections.defaultdict(list)
    for r in rows:
        stored = json.loads(r["inputs_json"] or "{}").get(key)
        if stored is None:
            classes["none"] += 1
            continue
        docs = [d for c in cik_family(r["cik"]) if (d := client.get("companyfacts", c))]
        secs = sec_values(docs, concepts, r["accession_number"], r["period_end"], r["form"])
        if not secs:
            classes["no_sec"] += 1
            continue
        cls = sign_class(stored, secs)
        classes[(cls, "stored<0" if stored < 0 else "stored>=0")] += 1
        if cls != "same_sign":
            examples[cls].append(
                (r["ticker"], r["form"], r["period_end"], stored, secs[:2], r["value"])
            )
    print(metric, key, dict(classes))
    for cls, items in examples.items():
        print(" ", cls, dict(collections.Counter(e[0] for e in items)))
        for e in items[:4]:
            print("    ", e)
    return dict(classes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--cache", help="SEC cache directory (SEC_XCHECK_CACHE)")
    args = parser.parse_args(argv)
    conn = open_ro(args.db)
    client = SecClient(cache_dir(args.cache), offline=True)
    for metric, key, concepts in CHECKS:
        check(conn, client, metric, key, concepts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
