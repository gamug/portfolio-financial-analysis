"""Command-line entry point: ``python -m fundamental_agent run [options]``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from fundamental_agent.config import Settings
from fundamental_agent.pipeline import DEFAULT_FORMS, DEFAULT_SINCE_YEAR, RunParams, run
from kg_schema.cli import run_migrate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fundamental_agent",
        description="Fundamental economic analysis of S&P 500 SEC filings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="analyze filings and write snapshots")
    run_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    run_cmd.add_argument(
        "--limit", type=int, help="cap the universe to the first N tickers (dev aid)"
    )
    run_cmd.add_argument("--tickers", help="comma-separated tickers to restrict the run to")
    run_cmd.add_argument(
        "--forms",
        default=",".join(DEFAULT_FORMS),
        help=f"comma-separated filing forms (default: {','.join(DEFAULT_FORMS)})",
    )
    run_cmd.add_argument(
        "--since-year",
        type=int,
        default=DEFAULT_SINCE_YEAR,
        help=f"earliest filing year to analyze (default: {DEFAULT_SINCE_YEAR})",
    )
    run_cmd.add_argument(
        "--until-year", type=int, help="latest filing year (default: current year)"
    )
    run_cmd.add_argument(
        "--fresh",
        action="store_true",
        help="re-analyze filings even if a snapshot already exists",
    )
    run_cmd.add_argument(
        "--refresh-universe",
        action="store_true",
        help="re-scrape the S&P 500 list before running",
    )
    run_cmd.add_argument(
        "--sections",
        action="store_true",
        help="also fetch each filing's primary document and extract MD&A / risk-factor text",
    )

    migrate_cmd = sub.add_parser(
        "migrate", help="apply pending shared-schema migrations (advances schema_version)"
    )
    migrate_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    return parser


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return run_migrate(args.db)
    settings = Settings.load()
    if args.db:
        settings = settings.model_copy(update={"db_path": Path(args.db)})

    params = RunParams(
        forms=_split(args.forms) or list(DEFAULT_FORMS),
        since_year=args.since_year,
        until_year=args.until_year,
        limit=args.limit,
        tickers=_split(args.tickers),
        fresh=args.fresh,
        refresh_universe=args.refresh_universe,
        sections=args.sections,
    )
    report = run(settings, params)

    print(
        f"\nrun {report.run_id}: {report.completed} analyzed, "
        f"{report.skipped} skipped, {report.failed} failed "
        f"(of {report.planned} planned)"
    )
    for line in report.errors[:20]:
        print(f"  ! {line}", file=sys.stderr)
    return 1 if report.failed and not report.completed else 0


if __name__ == "__main__":
    raise SystemExit(main())
