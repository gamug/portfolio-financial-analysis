"""``python -m cycle {select,monitor,backfill,undo-run}``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from cycle.config import CycleSettings
from cycle.db import ensure_schema
from cycle.fundamental_hook import make_hook
from cycle.orchestrator import run_monitoring, run_selection
from cycle.repair import NotBackdated, apply_undo, plan_undo
from cycle.state import ManifestMismatch
from cycle.writers import OutOfOrderCycle
from kg_schema import connect
from kg_schema.cli import resolve_db_path
from kg_schema.rundate import add_analysis_date_argument
from kg_schema.rundate import resolve as resolve_analysis_date
from kg_schema.versions import VersionError

_METRICS_VERSION_HELP = (
    "which fundamental_metrics engine version the cycle reads: a version (metrics-v1) or "
    "GROUP=VERSION pairs; default: the newest stored per group. A run of the same type and date "
    "built on other versions is refused, not mixed (T-090)"
)
_ALLOW_BACKDATED_HELP = (
    "override the out-of-order-cycle guard (T-097) and write the live portfolio_position book "
    "at a --analysis-date older than one already written -- never allowed to end a position "
    "opened after that date (T-104); for a deliberate historical run, not routine use"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cycle", description="Selection / monitoring cycles.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, helptext in (
        ("select", "run a selection cycle (writes portfolio_position)"),
        ("monitor", "run a monitoring cycle (refreshes vetoes / ranking only)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument(
            "--date", help="cycle date, YYYY-MM-DD (alias of --analysis-date; default: today)"
        )
        add_analysis_date_argument(p)
        p.add_argument("--db", help="override KG_FINANCIAL_DB path")
        p.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
        p.add_argument("--metrics-version", dest="metrics_version", help=_METRICS_VERSION_HELP)
        p.add_argument("--top-n", type=int, help="portfolio size (selection only)")
        p.add_argument("--dry-run", action="store_true", help="rank only, do not touch positions")
        if name == "select":
            # MONITORING never reaches the positions step (T-097), so the flag would be a
            # silent no-op there -- offered only where it can actually do something.
            p.add_argument("--allow-backdated", action="store_true", help=_ALLOW_BACKDATED_HELP)

    undo = sub.add_parser(
        "undo-run",
        help="revert a backdated select run's writes to the live book (T-104); dry run unless "
        "--apply",
    )
    undo.add_argument("--cycle-run", type=int, required=True, help="the backdated cycle_run id")
    undo.add_argument("--db", help="override KG_FINANCIAL_DB path")
    undo.add_argument("--apply", action="store_true", help="write the repair (default: print it)")

    bf = sub.add_parser("backfill", help="run selection cycles across a date range")
    bf.add_argument("--from", dest="date_from", required=True)
    bf.add_argument("--to", dest="date_to", required=True)
    bf.add_argument("--step-days", type=int, default=7)
    bf.add_argument("--db")
    bf.add_argument("--metrics-version", dest="metrics_version", help=_METRICS_VERSION_HELP)
    return parser


def _settings(args: argparse.Namespace) -> CycleSettings:
    s = CycleSettings.load()
    updates: dict[str, object] = {}
    if getattr(args, "db", None):
        updates["db_path"] = Path(args.db)
    if getattr(args, "universe_db", None):
        updates["universe_db_path"] = Path(args.universe_db)
    if getattr(args, "top_n", None):
        updates["top_n"] = args.top_n
    if getattr(args, "metrics_version", None):
        updates["metrics_version"] = args.metrics_version
    if getattr(args, "allow_backdated", False):
        updates["allow_backdated_positions"] = True
    return s.model_copy(update=updates) if updates else s


def _resolve_cycle_date(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """``--analysis-date`` (canonical) or its ``--date`` alias, defaulting to today.
    Passing both with different values is an error."""
    if args.analysis_date and args.date and args.analysis_date != args.date:
        parser.error(f"--date ({args.date}) and --analysis-date ({args.analysis_date}) disagree")
    return resolve_analysis_date(args.analysis_date or args.date)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(parser, args)
    except (VersionError, ManifestMismatch, OutOfOrderCycle, NotBackdated) as exc:
        print(f"cycle {args.command}: {exc}", file=sys.stderr)
        return 1


def _undo_run(args: argparse.Namespace) -> int:
    conn = connect(resolve_db_path(args.db))
    try:
        ensure_schema(conn)
        plan = plan_undo(conn, args.cycle_run)
        verb = "reverting" if args.apply else "would revert (dry run)"
        print(f"{verb} cycle_run {plan.cycle_run_id} ({plan.cycle_date}):")
        for sid, ticker, vf in plan.void:
            print(f"  void    stint {sid} {ticker} (opened {vf} by this run)")
        for sid, ticker, vf in plan.reopen:
            print(f"  reopen  stint {sid} {ticker} (opened {vf}, closed early by this run)")
        for sid, ticker, old, new in plan.reweight:
            print(f"  reweight stint {sid} {ticker}: {old} -> {new}")
        if plan.empty:
            print("  nothing to change")
        if args.apply:
            apply_undo(conn, plan)
            print(f"cycle_run {plan.cycle_run_id} marked reverted")
    finally:
        conn.close()
    return 0


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.command == "undo-run":  # needs no model settings
        return _undo_run(args)
    settings = _settings(args)
    hook = make_hook(settings)

    if args.command == "monitor":
        cycle_date = _resolve_cycle_date(parser, args)
        r = run_monitoring(settings, cycle_date, fundamental_hook=hook)
        print(
            f"monitor {r.cycle_run_id} {r.cycle_date}: {r.vetoed} hard-vetoed "
            f"(manifest {r.manifest_tag})"
        )
        return 0
    if args.command == "select":
        cycle_date = _resolve_cycle_date(parser, args)
        if args.dry_run:
            settings = settings.model_copy(update={"top_n": 0})
        r = run_selection(settings, cycle_date, fundamental_hook=hook)
        print(
            f"select {r.cycle_run_id} {r.cycle_date}: {r.selected} selected, "
            f"{r.vetoed} hard-vetoed (steps: {'+'.join(r.steps_run) or 'all skipped'}; "
            f"manifest {r.manifest_tag})"
        )
        if r.backdated_guard_bypassed is not None:
            print(
                f"  WARNING: --allow-backdated overrode the out-of-order-cycle guard "
                f"({r.backdated_guard_bypassed})",
                file=sys.stderr,
            )
        return 0
    # backfill
    d = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_to)
    while d <= end:
        r = run_selection(settings, d.isoformat(), fundamental_hook=hook)
        print(f"  {d.isoformat()}: {r.selected} selected")
        d += timedelta(days=args.step_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
