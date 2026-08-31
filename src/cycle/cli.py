"""``python -m cycle {select,monitor,backfill} --date YYYY-MM-DD``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from cycle.config import CycleSettings
from cycle.fundamental_hook import make_hook
from cycle.orchestrator import run_monitoring, run_selection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cycle", description="Selection / monitoring cycles.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, helptext in (
        ("select", "run a selection cycle (writes portfolio_position)"),
        ("monitor", "run a monitoring cycle (refreshes vetoes / ranking only)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--date", required=True, help="cycle date, YYYY-MM-DD")
        p.add_argument("--db", help="override KG_FINANTIAL_DB path")
        p.add_argument("--top-n", type=int, help="portfolio size (selection only)")
        p.add_argument("--dry-run", action="store_true", help="rank only, do not touch positions")

    bf = sub.add_parser("backfill", help="run selection cycles across a date range")
    bf.add_argument("--from", dest="date_from", required=True)
    bf.add_argument("--to", dest="date_to", required=True)
    bf.add_argument("--step-days", type=int, default=7)
    bf.add_argument("--db")
    return parser


def _settings(args: argparse.Namespace) -> CycleSettings:
    s = CycleSettings.load()
    updates: dict[str, object] = {}
    if getattr(args, "db", None):
        updates["db_path"] = Path(args.db)
    if getattr(args, "top_n", None):
        updates["top_n"] = args.top_n
    return s.model_copy(update=updates) if updates else s


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings(args)
    hook = make_hook(settings)

    if args.command == "monitor":
        r = run_monitoring(settings, args.date, fundamental_hook=hook)
        print(f"monitor {r.cycle_run_id} {r.cycle_date}: {r.vetoed} hard-vetoed")
        return 0
    if args.command == "select":
        if args.dry_run:
            settings = settings.model_copy(update={"top_n": 0})
        r = run_selection(settings, args.date, fundamental_hook=hook)
        print(
            f"select {r.cycle_run_id} {r.cycle_date}: {r.selected} selected, "
            f"{r.vetoed} hard-vetoed (steps: {'+'.join(r.steps_run) or 'all skipped'})"
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
