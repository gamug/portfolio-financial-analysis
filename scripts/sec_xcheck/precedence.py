"""Precedence check (D2): would "exact concept first, in the registry's own order" change a resolved value?

    uv run python scripts/sec_xcheck/precedence.py [--db PATH] [--tickers A,B]

Re-resolves every registry line item of every stored filing twice over the stored ``financial_facts``: with
today's ``Statements._first_component_match`` (document order over concepts, standard concepts and labels) and
with a concept-first variant (the registry's concept order wins, then the old rule). Prints, per item, how many
filings differ. Read-only; the resolver is patched in memory only.
"""

from __future__ import annotations

import argparse
import collections
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fundamental_agent import statements as S
from fundamental_agent.statements import REGISTRY, Statements

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import filings, open_ro, own_period_key, statements_from_db


def concept_first_factory(original: Callable[..., Any]) -> Callable[..., Any]:
    def concept_first(self: Any, spec: Any, column: str) -> Any:
        rows = list(self._rows_for(spec))
        for concept in spec.concepts:  # exact concept, in the spec's own priority order
            for row in rows:
                if row.get("concept") == concept:
                    value = S._numeric(row.get(column))
                    if value is not None:
                        return value
        return original(self, spec, column)  # then the old standard/label rule

    return concept_first


@contextmanager
def patched(replacement: Callable[..., Any]) -> Iterator[None]:
    original = Statements._first_component_match
    Statements._first_component_match = replacement  # type: ignore[method-assign]
    try:
        yield
    finally:
        Statements._first_component_match = original  # type: ignore[method-assign]


def resolve_both(stmts: Any, key: str) -> dict[str, tuple[Any, Any]]:
    """``{item: (document_order_value, concept_first_value)}`` for every registry item at *key*."""
    original = Statements._first_component_match
    out: dict[str, tuple[Any, Any]] = {}
    for item in REGISTRY:
        before = stmts.get(item, key)
        with patched(concept_first_factory(original)):
            after = stmts.get(item, key)
        out[item] = (before, after)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--tickers")
    args = parser.parse_args(argv)
    conn = open_ro(args.db)
    tickers = set(args.tickers.split(",")) if args.tickers else None
    diff: dict[str, list[tuple[Any, ...]]] = collections.defaultdict(list)
    total: collections.Counter[str] = collections.Counter()
    for f in filings(conn):
        if tickers and f["ticker"] not in tickers:
            continue
        stmts = statements_from_db(conn, f["id"])
        key = own_period_key(stmts, f["form"], f["period_end"])
        if key is None:
            continue
        for item, (a, b) in resolve_both(stmts, key).items():
            total[item] += 1
            if a != b:
                diff[item].append((f["ticker"], f["form"], f["period_end"], a, b))
    for item, rows in sorted(diff.items(), key=lambda x: -len(x[1])):
        by_ticker = dict(collections.Counter(r[0] for r in rows))
        print(f"{item}: {len(rows)}/{total[item]} differ; tickers {by_ticker}")
        for r in rows[:3]:
            print("    ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
