"""``python -m pricing_agent run [options]``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from kg_schema.cli import add_coverage_parser, coverage_from_args, run_migrate
from kg_schema.rundate import add_analysis_date_argument
from kg_schema.rundate import resolve as resolve_analysis_date
from pricing_agent import tiingo
from pricing_agent.config import Settings
from pricing_agent.pending import pending_line
from pricing_agent.pipeline import DEFAULT_START_DATE, RunParams, run
from pricing_agent.tiingo import DEFAULT_DATA_FILE, DEFAULT_MANIFEST_FILE, DEFAULT_TIINGO_DIR

_ALLOW_DIRTY_HELP = (
    "override the clean-tree guard (T-114) and write this run's code_version even "
    "though the working tree has uncommitted changes -- results would come from code "
    "HEAD alone can't reproduce; for a deliberate run from a work-in-progress checkout, "
    "not routine use"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pricing_agent",
        description="Collect S&P 500 daily-pricing window summaries since 2022.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="fetch prices and write price_window rows")
    run_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    run_cmd.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    add_analysis_date_argument(run_cmd)
    run_cmd.add_argument(
        "--start", default=DEFAULT_START_DATE, help=f"start date (default {DEFAULT_START_DATE})"
    )
    run_cmd.add_argument(
        "--end", help="end date, YYYY-MM-DD (clamped to --analysis-date; default: --analysis-date)"
    )
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
        "--observations",
        action="store_true",
        help="also derive per-day price analytics (ATR, rolling vol, drawdown) to "
        "price_observation; needs --store-daily (T-110), since an observation is an "
        "analytic over a specific stored price_daily bar",
    )
    run_cmd.add_argument(
        "--fresh", action="store_true", help="recompute windows even if already stored"
    )
    run_cmd.add_argument(
        "--refresh-universe",
        action="store_true",
        help="deprecated no-op (the universe is always read fresh from universe.db)",
    )
    run_cmd.add_argument("--allow-dirty", action="store_true", help=_ALLOW_DIRTY_HELP)
    run_cmd.add_argument(
        "--allow-split-jumps",
        action="store_true",
        help="store a series even though it has a split-shaped (x2, x0.5, x3 ...) close jump "
        "that a full re-fetch did not remove or that no recorded split explains (T-131) -- for a "
        "genuine one-day move of that size, which the guard cannot tell from a mis-adjusted split",
    )

    pending_cmd = sub.add_parser(
        "pending",
        help="read-only: does a member lack the last session's data? One line (T-017)",
    )
    pending_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    pending_cmd.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    add_analysis_date_argument(pending_cmd)

    migrate_cmd = sub.add_parser(
        "migrate", help="apply pending shared-schema migrations (advances schema_version)"
    )
    migrate_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")

    import_cmd = sub.add_parser(
        "import-tiingo",
        help="load the private Tiingo leaver-price file into price_daily/corporate_action "
        "(T-153 part 3d; refuses a file inside a git work tree or with a wrong sha256)",
    )
    import_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    import_cmd.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    import_cmd.add_argument(
        "--file",
        default=str(DEFAULT_TIINGO_DIR / DEFAULT_DATA_FILE),
        help=f"the private data file (default {DEFAULT_TIINGO_DIR / DEFAULT_DATA_FILE})",
    )
    import_cmd.add_argument(
        "--manifest",
        default=None,
        help="the fetch manifest (default: beside --file, named leaver_prices.manifest.json)",
    )
    import_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and report the plan; write nothing and no run row",
    )
    add_analysis_date_argument(import_cmd)

    remove_cmd = sub.add_parser(
        "remove-tiingo",
        help="delete every row the Tiingo import created (rule 5 / D9); prints what remains",
    )
    remove_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    remove_cmd.add_argument(
        "--dry-run", action="store_true", help="report the rows that would be deleted; delete none"
    )
    add_analysis_date_argument(remove_cmd)

    add_coverage_parser(sub)
    return parser


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.load()
    updates: dict[str, object] = {}
    if args.db:
        updates["db_path"] = Path(args.db)
    if args.universe_db:
        updates["universe_db_path"] = Path(args.universe_db)
    if getattr(args, "allow_dirty", False):
        updates["allow_dirty"] = True
    return settings.model_copy(update=updates) if updates else settings


def _run_import(args: argparse.Namespace) -> int:
    """``import-tiingo``: counts, dates and hashes on one line (never a value)."""
    data_file = Path(args.file)
    manifest = Path(args.manifest) if args.manifest else data_file.with_name(DEFAULT_MANIFEST_FILE)
    settings = _settings_from_args(args)
    analysis_date = resolve_analysis_date(args.analysis_date)
    try:
        report = tiingo.import_tiingo(
            settings,
            data_file=data_file,
            manifest_path=manifest,
            analysis_date=analysis_date,
            dry_run=args.dry_run,
        )
    except tiingo.TiingoImportError as exc:
        print(f"import-tiingo refused: {exc}")
        return 1
    verb = "would import" if args.dry_run else "imported"
    print(
        f"import-tiingo {verb} {report.lines_imported}/{report.lines} lines of "
        f"{report.first_date}..{report.last_date}: {report.price_rows} price rows, "
        f"{report.dividend_rows} dividends, {report.split_rows} splits "
        f"(sha256 {report.file_sha256[:12]}...)"
    )
    if report.gateway_only:
        named = ", ".join(
            f"{line.ticker} ({line.stored_rows} stored)" for line in report.gateway_only
        )
        print(f"import-tiingo gateway-only {len(report.gateway_only)}: {named}")
    else:
        print("import-tiingo gateway-only 0 lines")
    return 0


def _run_remove(args: argparse.Namespace) -> int:
    """``remove-tiingo``: the deleted counts and the postcondition (D9)."""
    settings = _settings_from_args(args)
    analysis_date = resolve_analysis_date(args.analysis_date)
    report = tiingo.remove_tiingo(settings, analysis_date=analysis_date, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    print(
        f"remove-tiingo {verb} {report.price_rows} price rows, {report.observation_rows} price "
        f"observations, {report.return_rows} return rows and {report.action_rows} corporate actions"
    )
    print(
        "remove-tiingo remaining: "
        + ", ".join(f"{name}={count}" for name, count in sorted(report.remaining.items()))
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "migrate":
        return run_migrate(args.db)
    if args.command == "coverage":
        return coverage_from_args(args)
    if args.command == "pending":
        print(pending_line(_settings_from_args(args), resolve_analysis_date(args.analysis_date)))
        return 0
    if args.command == "import-tiingo":
        return _run_import(args)
    if args.command == "remove-tiingo":
        return _run_remove(args)
    if args.observations and not args.store_daily:
        parser.error(
            "--observations needs --store-daily: price_observation rows are per-day "
            "analytics over a specific price_daily bar, and writing them without also "
            "storing that bar leaves an orphan observation date (T-110)"
        )
    settings = _settings_from_args(args)

    params = RunParams(
        start_date=args.start,
        end_date=args.end,
        limit=args.limit,
        tickers=_split(args.tickers),
        by_year=args.by_year,
        store_daily=args.store_daily,
        observations=args.observations,
        fresh=args.fresh,
        refresh_universe=args.refresh_universe,
        allow_split_jumps=args.allow_split_jumps,
        analysis_date=resolve_analysis_date(args.analysis_date),
    )
    report = run(settings, params)

    print(
        f"\nrun {report.run_id}: {report.completed} priced, "
        f"{report.skipped} skipped, {report.failed} failed "
        f"(of {report.planned} tickers)"
    )
    if report.full_refetches:
        print(
            f"  re-fetched in full after a split (T-131): {', '.join(report.full_refetches)} "
            "-- re-run `quant build-returns` to rebuild their return series"
        )
    if report.dirty_tree_bypassed is not None:
        print(
            f"  WARNING: --allow-dirty overrode the clean-tree guard "
            f"({report.dirty_tree_bypassed})",
            file=sys.stderr,
        )
    for line in report.errors[:20]:
        print(f"  ! {line}", file=sys.stderr)
    return 1 if report.failed and not report.completed else 0


if __name__ == "__main__":
    raise SystemExit(main())
