"""Income-tax identity check (D3): does ``pretax - tax = after-tax`` pick the SEC's tax value?

    uv run python scripts/sec_xcheck/taxid.py [--db PATH] [--cache DIR]

For each stored filing whose statements carry an ``IncomeTaxExpenseBenefit`` row, the pre-tax income and the
after-tax income, tests the two candidate signs of the tax against the identity ``pre - tax (+ equity-method
income) = after`` within ``max(1e6, 0.5% of |pre|)``. A unique sign that equals the SEC's ``companyfacts`` value is
``identity_right``; one that does not is ``IDENTITY_WRONG``. Read-only.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import (
    SecClient,
    cache_dir,
    cik_family,
    filings,
    open_ro,
    own_period_key,
    statements_from_db,
)
from sec_xcheck.signcheck import sec_values

DOLLAR = 0.5  # the match tolerance of a stored value against a filed one

PRETAX = (
    "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)
AFTER = (
    "us-gaap_IncomeLossFromContinuingOperations",
    "us-gaap_IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
    "us-gaap_ProfitLoss",
)


def row_value(stmts: Any, concept: str, key: str) -> float | None:
    for row in stmts.raw["income_statement"]:
        value = row.get(key)
        if (
            row.get("concept") == concept
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            return float(value)
    return None


def first_of(stmts: Any, concepts: tuple[str, ...], key: str) -> float | None:
    for concept in concepts:
        value = row_value(stmts, concept, key)
        if value is not None:
            return value
    return None


def identity_signs(pre: float, tax: float, after: float, equity_method: float) -> list[float]:
    """The signs of the tax (``+|tax|`` or ``-|tax|``) that satisfy the identity within tolerance."""
    tol = max(1e6, 0.005 * abs(pre))
    return [
        s
        for s in (abs(tax), -abs(tax))
        if abs(pre - s - after) <= tol or abs(pre - s + equity_method - after) <= tol
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--cache", help="SEC cache directory (SEC_XCHECK_CACHE)")
    args = parser.parse_args(argv)
    conn = open_ro(args.db)
    client = SecClient(cache_dir(args.cache), offline=True)
    res: collections.Counter[str] = collections.Counter()
    examples: list[tuple[Any, ...]] = []
    for f in filings(conn):
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            continue
        docs = [d for c in cik_family(f["cik"]) if (d := client.get("companyfacts", c))]
        truth = sec_values(
            docs, ("IncomeTaxExpenseBenefit",), f["accession_number"], f["period_end"], f["form"]
        )
        tax = row_value(stmts, "us-gaap_IncomeTaxExpenseBenefit", key)
        if tax is None or not truth:
            res["n/a"] += 1
            continue
        pre, after = first_of(stmts, PRETAX, key), first_of(stmts, AFTER, key)
        if pre is None or after is None:
            res["identity_inputs_missing"] += 1
            continue
        eqm = row_value(stmts, "us-gaap_IncomeLossFromEquityMethodInvestments", key) or 0.0
        signs = identity_signs(pre, tax, after, eqm)
        if len(signs) != 1:
            res["identity_ambiguous_or_fails"] += 1
            examples.append(
                (f["ticker"], f["form"], f["period_end"], pre, tax, after, eqm, truth[0])
            )
            continue
        res["identity_right" if abs(signs[0] - truth[0]) <= DOLLAR else "IDENTITY_WRONG"] += 1
    print(dict(res))
    for e in examples[:8]:
        print(" ", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
