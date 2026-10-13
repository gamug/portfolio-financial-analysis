"""``python -m entity_resolution build [options]``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from entity_resolution.config import Settings
from entity_resolution.pending import pending_line
from entity_resolution.pipeline import RunParams, run
from kg_schema.rundate import add_analysis_date_argument
from kg_schema.rundate import resolve as resolve_analysis_date

_ALLOW_DIRTY_HELP = (
    "override the clean-tree guard (T-114) and write this run's code_version even "
    "though the working tree has uncommitted changes -- results would come from code "
    "HEAD alone can't reproduce; for a deliberate run from a work-in-progress checkout, "
    "not routine use"
)


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
    b.add_argument("--allow-dirty", action="store_true", help=_ALLOW_DIRTY_HELP)
    p = sub.add_parser(
        "pending",
        help="read-only: is the stored edge set stale against the news? One line (T-017)",
    )
    p.add_argument("--db", help="override KG_FINANCIAL_DB path")
    p.add_argument("--news-db", help="override KG_NEWS_DB (urls.db) path")
    p.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    add_analysis_date_argument(p)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    updates: dict[str, object] = {}
    if args.db:
        updates["db_path"] = Path(args.db)
    if args.news_db:
        updates["news_db_path"] = Path(args.news_db)
    if args.universe_db:
        updates["universe_db_path"] = Path(args.universe_db)
    if getattr(args, "allow_dirty", False):
        updates["allow_dirty"] = True
    if updates:
        settings = settings.model_copy(update=updates)

    if args.command == "pending":
        print(pending_line(settings, resolve_analysis_date(args.analysis_date)))
        return 0

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
    if report.dirty_tree_bypassed is not None:
        print(
            f"  WARNING: --allow-dirty overrode the clean-tree guard "
            f"({report.dirty_tree_bypassed})",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
