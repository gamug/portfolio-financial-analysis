"""``python -m pricing_agent run [options]``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from kg_schema.cli import run_migrate
from pricing_agent.config import Settings
from pricing_agent.pipeline import DEFAULT_START_DATE, RunParams, run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pricing_agent",
        description="Collect S&P 500 daily-pricing window summaries since 2022.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="fetch prices and write price_window rows")
    run_cmd.add_argument("--db", help="override KG_FINANTIAL_DB path")
    run_cmd.add_argument(
        "--start", default=DEFAULT_START_DATE, help=f"start date (default {DEFAULT_START_DATE})"
    )
    run_cmd.add_argument("--end", help="end date, YYYY-MM-DD (default: today)")
    run_cmd.add_argument("--limit", type=int, help="cap the universe to the first N tickers")
    run_cmd.add_argument("--tickers", help="comma-separated tickers to restrict to")
    run_cmd.add_argument(
        "--by-year",
        action="store_true",
        help="also emit one window summary per calendar year",
    )
    run_cmd.add_argument(
        "--store-daily",
        action="store_true",
        help="also persist every daily OHLCV bar to price_daily",
    )
    run_cmd.add_argument(
        "--fresh", action="store_true", help="recompute windows even if already stored"
    )
    run_cmd.add_argument(
        "--refresh-universe",
        action="store_true",
        help="re-pull the tracked universe before running",
    )

    migrate_cmd = sub.add_parser(
        "migrate", help="apply pending shared-schema migrations (advances schema_version)"
    )
    migrate_cmd.add_argument("--db", help="override KG_FINANTIAL_DB path")
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
        start_date=args.start,
        end_date=args.end,
        limit=args.limit,
        tickers=_split(args.tickers),
        by_year=args.by_year,
        store_daily=args.store_daily,
        fresh=args.fresh,
        refresh_universe=args.refresh_universe,
    )
    report = run(settings, params)

    print(
        f"\nrun {report.run_id}: {report.completed} priced, "
        f"{report.skipped} skipped, {report.failed} failed "
        f"(of {report.planned} tickers)"
    )
    for line in report.errors[:20]:
        print(f"  ! {line}", file=sys.stderr)
    return 1 if report.failed and not report.completed else 0


if __name__ == "__main__":
    raise SystemExit(main())
