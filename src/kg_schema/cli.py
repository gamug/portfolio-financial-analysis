"""Shared ``migrate`` / ``coverage`` implementations for the agents' command lines.

``migrate`` runs the additive schema *and* the non-additive rebuilds in
:mod:`kg_schema.migrations` -- the only entrypoint that advances ``schema_version``;
quiesce the other repos first. ``coverage`` is read-only: it reports which members
of the dated universe have core data and records it in ``universe_coverage``.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import kg_schema
from kg_schema.coverage import (
    DEFAULT_MIN_OBSERVATION_DAYS,
    CoverageReport,
    check_coverage,
    persist_coverage,
)
from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path
from kg_schema.rundate import resolve as resolve_analysis_date
from kg_schema.universe_source import connect_ro

_HEAD = 12  # symbols shown inline per missing-check line before eliding


def resolve_db_path(explicit: str | None) -> Path:
    path = database_path(explicit)
    if not path:
        raise RuntimeError(f"no database: pass --db or set {DB_ENV_VAR}")
    return Path(path).expanduser()


def run_migrate(db_path: str | None) -> int:
    """Apply pending migrations to *db_path*; print the resulting version table."""
    path = resolve_db_path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        applied = kg_schema.ensure(conn, run_migrations=True)
        rows = conn.execute(
            "SELECT version, applied_at, description FROM schema_version ORDER BY version"
        ).fetchall()
    finally:
        conn.close()

    if applied:
        print(f"applied migrations: {', '.join(map(str, applied))}")
    else:
        print("schema already up to date")
    for r in rows:
        print(f"  v{r['version']}  {r['applied_at']}  {r['description']}")
    return 0


def run_coverage(  # noqa: PLR0913 - flat CLI knobs, all keyword-only
    db_path: str | None,
    *,
    universe_db_path: str | None = None,
    analysis_date: str | None = None,
    universe: str = "SP500",
    min_observation_days: int = DEFAULT_MIN_OBSERVATION_DAYS,
    strict: bool = False,
    min_fraction: float = 1.0,
    print_fill_commands: bool = False,
) -> int:
    """Report + persist core-data coverage for *universe* as of *analysis_date*.

    Exit 0 by default (``warn``); with *strict*, exit 1 when the covered fraction
    is below *min_fraction*."""
    as_of = resolve_analysis_date(analysis_date)
    fin_path = resolve_db_path(db_path)
    uni_path = universe_database_path(universe_db_path)

    conn = sqlite3.connect(fin_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        kg_schema.ensure(conn)  # additive only -- makes `universe_coverage` exist
        with connect_ro(uni_path) as uconn:
            report = check_coverage(
                conn, uconn, as_of, universe=universe, min_observation_days=min_observation_days
            )
        written = persist_coverage(conn, report)
    finally:
        conn.close()

    print(
        f"coverage {report.universe} as of {report.as_of}: "
        f"{report.covered}/{report.total} covered ({report.fraction:.1%}); "
        f"{written} rows -> universe_coverage"
    )
    for check in ("assets", "fundamental", "metrics", "pricing", "observations", "returns"):
        miss = report.missing_for(check)
        if miss:
            head = ", ".join(miss[:_HEAD]) + (
                f" … (+{len(miss) - _HEAD})" if len(miss) > _HEAD else ""
            )
            print(f"  missing {check:<12} {len(miss):>3}: {head}")

    if print_fill_commands:
        _print_fill_commands(report, as_of)

    if strict and report.fraction < min_fraction:
        print(
            f"FAIL: coverage {report.fraction:.1%} < required {min_fraction:.1%} "
            f"({len(report.uncovered)} members missing a required check)"
        )
        return 1
    return 0


def _print_fill_commands(report: CoverageReport, as_of: str) -> None:
    need_fund = sorted({*report.missing_for("fundamental"), *report.missing_for("metrics")})
    need_price = sorted({*report.missing_for("pricing"), *report.missing_for("observations")})
    print("\n# backfill commands (delisted names may still fail -- see *_run_error):")
    if need_fund:
        print(
            f"python -m fundamental_agent run --analysis-date {as_of} "
            f"--tickers {','.join(need_fund)}"
        )
    if need_price:
        print(
            f"python -m pricing_agent run --analysis-date {as_of} --observations "
            f"--tickers {','.join(need_price)}"
        )
    if not (need_fund or need_price):
        print("# (nothing to backfill)")


def add_coverage_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the shared ``coverage`` subcommand on an agent's argparse tree."""
    p = subparsers.add_parser("coverage", help="report core-data coverage for the dated universe")
    p.add_argument("--db", help="override KG_FINANCIAL_DB path")
    p.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    p.add_argument("--analysis-date", dest="analysis_date", metavar="YYYY-MM-DD")
    p.add_argument("--universe", default="SP500")
    p.add_argument("--min-observation-days", type=int, default=DEFAULT_MIN_OBSERVATION_DAYS)
    p.add_argument("--strict", action="store_true", help="exit 1 when coverage < --min-fraction")
    p.add_argument("--min-fraction", type=float, default=1.0)
    p.add_argument(
        "--print-fill-commands",
        action="store_true",
        help="emit the fundamental_agent / pricing_agent runs that would close the gaps",
    )


def coverage_from_args(args: argparse.Namespace) -> int:
    """Run ``coverage`` from a namespace produced by :func:`add_coverage_parser`."""
    return run_coverage(
        args.db,
        universe_db_path=args.universe_db,
        analysis_date=args.analysis_date,
        universe=args.universe,
        min_observation_days=args.min_observation_days,
        strict=args.strict,
        min_fraction=args.min_fraction,
        print_fill_commands=args.print_fill_commands,
    )
