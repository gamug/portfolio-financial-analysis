"""``python -m entity_resolution build [options]``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from portfolio_common.kg_schema.rundate import add_analysis_date_argument
from portfolio_common.kg_schema.rundate import resolve as resolve_analysis_date

from entity_resolution.config import Settings
from entity_resolution.pipeline import RunParams, run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="entity_resolution",
        description="Build sharedExecutiveWith edges from news PER co-occurrence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="recompute shared_executive_edge")
    b.add_argument("--db", help="override KG_FINANCIAL_DB path")
    b.add_argument("--news-db", help="override KG_NEWS_DB (urls.db) path")
    b.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    add_analysis_date_argument(b)
    b.add_argument("--min-weight", type=float, default=3.0)
    b.add_argument("--max-tickers", type=int, default=15)
    b.add_argument("--min-articles", type=int, default=3)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    updates: dict[str, Path] = {}
    if args.db:
        updates["db_path"] = Path(args.db)
    if args.news_db:
        updates["news_db_path"] = Path(args.news_db)
    if args.universe_db:
        updates["universe_db_path"] = Path(args.universe_db)
    if updates:
        settings = settings.model_copy(update=updates)

    report = run(
        settings,
        RunParams(
            min_weight=args.min_weight,
            max_tickers=args.max_tickers,
            min_articles=args.min_articles,
            analysis_date=resolve_analysis_date(args.analysis_date),
        ),
    )
    print(
        f"cycle {report.cycle_run_id}: {report.edges} shared-executive edges "
        f"over {report.tickers} tickers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
