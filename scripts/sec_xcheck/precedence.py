"""Precedence check (D2, ID-02a): does the resolver read the exact concept first, in the spec's own order?

    uv run python scripts/sec_xcheck/precedence.py [--db PATH] [--tickers A,B]

Re-resolves every plain registry line item of every stored filing three ways over the stored ``financial_facts``:

* **document order** -- the rule before T-148: the first row, in the statement's own order, whose concept, standard
  concept or label matches the item;
* **concept first** -- the spec's order: the item's concepts one after the other, then its standard concepts, then its
  label hints (ID-02a: a label or a standard match never outranks an exact concept);
* the resolver itself (``Statements.get``).

It prints, per item, how many filings document order would have read differently from the resolver (the rule's effect)
and how many the resolver reads differently from concept-first (0 once T-148 is in). The items with a composite rule
(net income, income tax, the debt lines), a filed total, a sum or fallback tiers are not plain and are skipped here.
Read-only.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path
from typing import Any

from fundamental_agent.statements import REGISTRY, LineItem, Statements, filed_value

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import filings, open_ro, own_period_key, statements_from_db


def is_plain(spec: LineItem) -> bool:
    """A line item the precedence alone decides: no composite resolver, total, sum or fallback tier."""
    return not (
        spec.resolver
        or spec.total_concepts
        or spec.sum_components
        or spec.fallback_concepts
        or spec.label_fallback
    )


def _matches(row: dict[str, Any], spec: LineItem) -> bool:
    label = str(row.get("label") or "").lower()
    return (
        row.get("concept") in spec.concepts
        or bool(spec.standard and row.get("standard_concept") in spec.standard)
        or any(hint in label for hint in spec.label_contains)
    )


def _rows(stmts: Statements, spec: LineItem) -> list[dict[str, Any]]:
    return [
        row
        for statement in spec.statements
        for row in stmts.raw.get(statement, [])
        if not row.get("abstract") and not row.get("dimension")
    ]


def document_order(stmts: Statements, spec: LineItem, column: str) -> float | None:
    """The rule before T-148: the first matching row of the statement, whatever the kind of match."""
    for row in _rows(stmts, spec):
        if _matches(row, spec) and (value := filed_value(row, column)) is not None:
            return value
    return None


def concept_first(stmts: Statements, spec: LineItem, column: str) -> float | None:
    """ID-02a: exact concepts in the spec's order, then standard concepts, then label hints."""
    rows = _rows(stmts, spec)
    exact = [r for c in spec.concepts for r in rows if r.get("concept") == c]
    standard = [
        r
        for r in rows
        if spec.standard
        and r.get("concept") not in spec.concepts
        and r.get("standard_concept") in spec.standard
    ]
    labelled = [
        r
        for r in rows
        if r not in exact
        and r not in standard
        and any(h in str(r.get("label") or "").lower() for h in spec.label_contains)
    ]
    for row in (*exact, *standard, *labelled):
        if (value := filed_value(row, column)) is not None:
            return value
    return None


def resolve_both(stmts: Statements, key: str) -> dict[str, tuple[float | None, ...]]:
    """``{item: (document_order, concept_first, resolver)}`` for every plain registry item at *key*."""
    out: dict[str, tuple[float | None, ...]] = {}
    for item, spec in REGISTRY.items():
        if not is_plain(spec):
            continue
        column = stmts.resolve_column(item, key)
        if column is None:
            continue
        out[item] = (
            document_order(stmts, spec, column),
            concept_first(stmts, spec, column),
            stmts.get(item, key),
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--tickers")
    args = parser.parse_args(argv)
    conn = open_ro(args.db)
    tickers = set(args.tickers.split(",")) if args.tickers else None
    would_differ: dict[str, list[tuple[Any, ...]]] = collections.defaultdict(list)
    broken: dict[str, list[tuple[Any, ...]]] = collections.defaultdict(list)
    total: collections.Counter[str] = collections.Counter()
    for f in filings(conn):
        if tickers and f["ticker"] not in tickers:
            continue
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            continue
        for item, (old, first, got) in resolve_both(stmts, key).items():
            total[item] += 1
            if old != got:
                would_differ[item].append((f["ticker"], f["form"], f["period_end"], old, got))
            if first != got:
                broken[item].append((f["ticker"], f["form"], f["period_end"], first, got))
    for item in sorted(total):
        a, b = len(would_differ[item]), len(broken[item])
        by_ticker = dict(collections.Counter(r[0] for r in would_differ[item]))
        print(
            f"{item}: document order would differ from the resolver in {a}/{total[item]} filings "
            f"{by_ticker if a else ''}; resolver != concept-first in {b}"
        )
        for r in would_differ[item][:3]:
            print("    would differ:", r)
        for r in broken[item][:3]:
            print("    NOT concept-first:", r)
    print("resolver == concept-first everywhere:", not any(broken.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
