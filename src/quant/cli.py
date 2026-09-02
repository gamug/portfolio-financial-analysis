"""``python -m quant {backfill-actions,build-returns,build-risk-model,optimize,benchmark,evaluate}``.

Subcommands are filled in milestone by milestone; an unimplemented one prints a
notice and exits 0.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from quant.actions import backfill_corporate_actions
from quant.config import QuantSettings

_TODAY_HELP = "date, YYYY-MM-DD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant", description="Markowitz benchmark portfolio.")
    sub = parser.add_subparsers(dest="command", required=True)

    ba = sub.add_parser("backfill-actions", help="fetch dividends/splits into corporate_action")
    ba.add_argument("--db", help="override KG_FINANCIAL_DB path")
    ba.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    ba.add_argument("--to", dest="date_to", help=f"{_TODAY_HELP} (default: today)")
    ba.add_argument("--source", choices=("gateway", "derive"), default="derive")

    for name, helptext in (
        ("build-returns", "derive the total-return daily series (quant_return_daily)"),
        ("build-risk-model", "estimate mu / covariance for an as-of date"),
        ("optimize", "run the objective family and persist the benchmark books"),
        ("benchmark", "build the internal equal-/cap-weight benchmark series"),
        ("evaluate", "forward realized returns: each book vs the live portfolio_position"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--db", help="override KG_FINANCIAL_DB path")
    return parser


def _settings(args: argparse.Namespace) -> QuantSettings:
    s = QuantSettings.load()
    updates: dict[str, object] = {}
    if getattr(args, "db", None):
        updates["db_path"] = Path(args.db)
    return s.model_copy(update=updates) if updates else s


def _today() -> str:
    return date.today().isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings(args)

    if args.command == "backfill-actions":
        report = backfill_corporate_actions(
            settings,
            date_from=args.date_from,
            date_to=args.date_to or _today(),
            source=args.source,
        )
        probe = " (gateway probe failed -> derived)" if report.gateway_probe_failed else ""
        print(
            f"backfill-actions [{report.source}{probe}]: {report.assets_seen} assets, "
            f"{report.dividends} dividends + {report.splits} splits, "
            f"{report.inserted} new rows ({report.engine_version})"
        )
        if report.errors:
            print(f"  {len(report.errors)} asset(s) errored; first: {report.errors[0]}")
        return 0

    print(f"quant {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
