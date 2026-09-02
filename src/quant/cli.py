"""``python -m quant {backfill-actions,build-returns,build-risk-model,optimize,benchmark,evaluate}``.

Skeleton: the parser is wired for every planned subcommand; each is filled in by a
later milestone. Running one today prints a not-implemented notice and exits 0.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("backfill-actions", "fetch dividends/splits into corporate_action"),
    ("build-returns", "derive the total-return daily series (quant_return_daily)"),
    ("build-risk-model", "estimate mu / covariance for an as-of date"),
    ("optimize", "run the objective family and persist the benchmark books"),
    ("benchmark", "build the internal equal-/cap-weight benchmark series"),
    ("evaluate", "forward realized returns: each book vs the live portfolio_position"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant", description="Markowitz benchmark portfolio.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in _SUBCOMMANDS:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--db", help="override KG_FINANCIAL_DB path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"quant {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
