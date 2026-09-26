"""Command-line entry point: ``python -m fundamental_agent
run|quality|repair-accessions|migrate|coverage``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from fundamental_agent import db, quality, repair
from fundamental_agent.config import DEFAULT_EDGAR_BASE_URL, Settings
from fundamental_agent.edgar_client import EdgarClient
from fundamental_agent.pipeline import DEFAULT_FORMS, DEFAULT_SINCE_YEAR, RunParams, run
from kg_schema import connect
from kg_schema.cli import add_coverage_parser, coverage_from_args, resolve_db_path, run_migrate
from kg_schema.rundate import add_analysis_date_argument
from kg_schema.rundate import resolve as resolve_analysis_date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fundamental_agent",
        description="Fundamental economic analysis of S&P 500 SEC filings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="analyze filings and write snapshots")
    run_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    run_cmd.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    add_analysis_date_argument(run_cmd)
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
        help="deprecated no-op (the universe is always read fresh from universe.db)",
    )
    run_cmd.add_argument(
        "--sections",
        action="store_true",
        help="also fetch each filing's primary document and extract MD&A / risk-factor text",
    )

    quality_cmd = sub.add_parser(
        "quality",
        help="run the Ring-1 data-quality gates over every stored filing's metrics (backfill)",
    )
    quality_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    quality_cmd.add_argument(
        "--metrics-version",
        help="gate only this stored metrics engine version (default: every version stored)",
    )

    repair_cmd = sub.add_parser(
        "repair-accessions",
        help="replace legacy quarters that share one 10-Q's accession (T-120; dry run by default)",
    )
    repair_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")
    repair_cmd.add_argument(
        "--apply", action="store_true", help="write the repair (default: report the plan only)"
    )
    repair_cmd.add_argument(
        "--drop-unresolved",
        action="store_true",
        help="with --apply, also delete stale rows whose own filing the gateway cannot find",
    )

    migrate_cmd = sub.add_parser(
        "migrate", help="apply pending shared-schema migrations (advances schema_version)"
    )
    migrate_cmd.add_argument("--db", help="override KG_FINANCIAL_DB path")

    add_coverage_parser(sub)
    return parser


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _run_quality(args: argparse.Namespace) -> int:
    # No LLM involved, so no Settings.load() (which insists on the LLM variables).
    conn = connect(resolve_db_path(args.db))
    try:
        db.ensure_schema(conn)
        reports = quality.gate_all(conn, engine_version=args.metrics_version)
    finally:
        conn.close()
    if not reports:
        which = args.metrics_version or "any metrics engine version"
        print(f"no fundamental_metrics stored for {which}", file=sys.stderr)
        return 1
    print(f"data-quality gates {quality.GATE_VERSION}")
    for version, report in reports.items():
        print(f"\n{version}: {report.filings} filings gated, {report.inserted} new issue rows")
        for rule_id in quality.RULE_IDS:
            hit = report.filings_by_rule.get(rule_id, 0)
            hard = report.hard_by_rule.get(rule_id, 0)
            print(f"  {rule_id:<17} {hit:>6} filings  ({hard} HARD)")
    return 0


def _run_repair(args: argparse.Namespace) -> int:
    # Gateway only, no LLM: no Settings.load().
    conn = connect(resolve_db_path(args.db))
    try:
        db.ensure_schema(conn)
        try:
            groups = repair.shared_groups(conn)
        except repair.RepairRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        if not groups:
            print("no shared accession numbers: nothing to repair")
            return 0
        base_url = os.environ.get("EDGAR_BASE_URL", DEFAULT_EDGAR_BASE_URL)
        with EdgarClient(base_url) as edgar:
            outcomes = repair.repair(
                conn, edgar, apply=args.apply, drop_unresolved=args.drop_unresolved, groups=groups
            )
    finally:
        conn.close()
    verb = "replaced" if args.apply else "would replace (dry run)"
    for o in outcomes:
        g = o.group
        print(f"{g.ticker} {g.accession} (filed {g.filing_date}): keep {g.keep_period}")
        for r in o.replaced:
            s = r.stale
            print(
                f"  {verb:<24} {s.fiscal_period} (filing {s.filing_id}, {s.facts} facts, "
                f"{s.sections} sections) -> {r.accession} filed {r.filing_date}"
            )
        for s in o.unresolved:
            state = "dropped" if o.dropped else "unresolved, left as is"
            print(f"  {state:<24} {s.fiscal_period} (filing {s.filing_id})")
        if o.error:
            print(f"  ! gateway: {o.error}", file=sys.stderr)
    pending = sum(1 for o in outcomes if o.unresolved and not o.dropped)
    print(
        f"\n{len(outcomes)} shared accessions; "
        f"{sum(len(o.replaced) for o in outcomes)} quarters {'replaced' if args.apply else 'found'}; "
        f"{pending} accessions left unresolved"
    )
    return 1 if pending else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return run_migrate(args.db)
    if args.command == "coverage":
        return coverage_from_args(args)
    if args.command == "quality":
        return _run_quality(args)
    if args.command == "repair-accessions":
        return _run_repair(args)
    settings = Settings.load()
    updates: dict[str, Path] = {}
    if args.db:
        updates["db_path"] = Path(args.db)
    if args.universe_db:
        updates["universe_db_path"] = Path(args.universe_db)
    if updates:
        settings = settings.model_copy(update=updates)

    params = RunParams(
        forms=_split(args.forms) or list(DEFAULT_FORMS),
        since_year=args.since_year,
        until_year=args.until_year,
        limit=args.limit,
        tickers=_split(args.tickers),
        fresh=args.fresh,
        refresh_universe=args.refresh_universe,
        sections=args.sections,
        analysis_date=resolve_analysis_date(args.analysis_date),
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
