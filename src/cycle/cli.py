"""``python -m cycle {select,monitor,backfill,undo-run}``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from cycle.config import CycleSettings
from cycle.construction import SCHEMES, BookInputError, BookResult
from cycle.data import TooManyUnscored
from cycle.db import ensure_schema
from cycle.fundamental_hook import make_hook
from cycle.orchestrator import (
    CycleReport,
    DryRunBook,
    NoStoredRanking,
    PreferencesNeedDryRun,
    dry_run_book,
    run_monitoring,
    run_replay,
    run_selection,
)
from cycle.repair import NotBackdated, apply_undo, plan_undo
from cycle.replay import reset_replay_range
from cycle.state import ConstructionMismatch, ManifestMismatch, ScoreWeightsMismatch
from cycle.writers import OutOfOrderCycle
from kg_schema import connect
from kg_schema.cli import resolve_db_path
from kg_schema.env import DB_ENV_VAR, database_path
from kg_schema.provenance import DirtyTree
from kg_schema.queries import StaleAsOf, StaleGateVersion, VetoSchemaStale
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
_ALLOW_STALE_PRICES_HELP = (
    "override the price-spine guard (T-110) and run at a --analysis-date past price_daily's "
    "last stored date -- TECHNICAL/veto would silently score against prices that are, at "
    "best, weeks stale; for a deliberate run ahead of the spine, not routine use"
)
_ALLOW_DIRTY_HELP = (
    "override the clean-tree guard (T-114) and write this run's code_version even though the "
    "working tree has uncommitted changes -- results would come from code HEAD alone can't "
    "reproduce; for a deliberate run from a work-in-progress checkout, not routine use"
)
_ALLOW_STALE_DQ_GATE_HELP = (
    "override the Ring-1 gate-version guard (T-116) and run even though data_quality_issue "
    "holds rows under an older gate version than the current one -- every quarantine and HARD "
    "DQ_*/DATA_QUALITY veto would silently read as clean, not because filings got cleaner; for "
    "a deliberate run before re-gating, not routine use"
)
_ALLOW_BACKDATED_VETO_HELP = (
    "override the veto out-of-order guard (T-125) and write veto transitions at a "
    "--analysis-date older than the latest one already recorded -- the shared veto stints "
    "table, not just the live positions book; for a deliberate historical re-run, not "
    "routine use"
)
_FORCE_HELP = (
    "reset the replay book (T-115) from --from through its end before replaying: deletes/"
    "reopens portfolio_position_replay stints and cycle_run rows on or after --from, with no "
    "upper bound -- a replay is path-dependent, so leaving a later stint in place would trip "
    "the out-of-order-replay guard against it; for redoing a backfill after a code fix, not "
    "routine use"
)
_TOP_N_HELP = "portfolio size N, the number of names the book holds (default 30)"
_SCHEME_HELP = (
    "weight scheme (default score_tilt: an equal-weight core with a bounded score tilt, each "
    "weight in [0.5/N, 1.5/N] proportional to its score inside that band). equal, "
    "score_proportional and inverse_vol stay selectable, now through the same exact projection "
    "(their old cap breaches are gone, so a stored run is not bit-reproducible) (T-134)"
)
_NAME_CAP_HELP = (
    "explicit per-name weight cap; default is derived: 1.5/N for score_tilt (no 0.10 floor), 0.10 "
    "for the legacy schemes. A cap below 1/N cannot sum to 1 and is relaxed to 1/N, recorded"
)
_SECTOR_CAP_HELP = (
    "per-sector weight cap (default 0.30). A sector is skipped once it holds "
    "max(1, floor(cap x N)) names; if the cap still cannot hold (too few sectors) it is relaxed "
    "to the smallest feasible value and the relaxation is recorded -- never silent"
)
_PREFERENCES_NOTE = (
    "A book built with preferences is decision support, never the thesis book: select accepts "
    "them only with --dry-run (it prints the book and writes no positions), backfill accepts "
    "them (it writes only portfolio_position_replay)."
)
_PIN_HELP = (
    "comma-separated tickers held first (they count toward N and obey the weight band and the "
    "caps). A HARD-vetoed pin is refused with its reason; a SOFT-vetoed pin is held and flagged. "
    + _PREFERENCES_NOTE
)
_EXCLUDE_HELP = "comma-separated tickers never held. Excluding a pinned ticker is an error"
_EXCLUDE_SECTORS_HELP = "comma-separated sector names never held (case-insensitive)"
_ONLY_SECTORS_HELP = (
    "comma-separated sector names: hold only these; the sector cap relaxes to what is feasible "
    "(one sector -> 1.0), recorded"
)
_DRY_RUN_HELP = (
    "select: build and print the book (ticker, sector, weight, the effective caps, relaxations "
    "and pin notes) from the ranking already stored for the date -- the SELECTION run's, else "
    "the MONITORING run's; with none, run `cycle monitor` first. Strictly read-only: it opens no "
    "cycle_run and writes nothing. monitor: ignored"
)
_BACKFILL_DB_HELP = (
    "path to a throwaway copy of the database -- required. backfill's REPLAY steps write "
    "score_snapshot / veto / sector_aggregate_snapshot / cycle_ranking, all still shared with "
    "the live system even though `positions` itself is isolated (portfolio_position_replay); "
    f"refused against the production database ({DB_ENV_VAR}). Copy it first, e.g. "
    f'cp "${DB_ENV_VAR}" /tmp/backfill.db, then pass --db /tmp/backfill.db'
)


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False everywhere: `--pin` must not silently match a longer flag (PR #119 review)
    parser = argparse.ArgumentParser(
        prog="cycle", description="Selection / monitoring cycles.", allow_abbrev=False
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, helptext in (
        ("select", "run a selection cycle (writes portfolio_position)"),
        ("monitor", "run a monitoring cycle (refreshes vetoes / ranking only)"),
    ):
        p = sub.add_parser(name, help=helptext, allow_abbrev=False)
        p.add_argument(
            "--date", help="cycle date, YYYY-MM-DD (alias of --analysis-date; default: today)"
        )
        add_analysis_date_argument(p)
        p.add_argument("--db", help="override KG_FINANCIAL_DB path")
        p.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
        p.add_argument("--metrics-version", dest="metrics_version", help=_METRICS_VERSION_HELP)
        p.add_argument("--top-n", type=int, help=_TOP_N_HELP + " (selection only)")
        p.add_argument("--dry-run", action="store_true", help=_DRY_RUN_HELP)
        p.add_argument("--allow-stale-prices", action="store_true", help=_ALLOW_STALE_PRICES_HELP)
        p.add_argument("--allow-dirty", action="store_true", help=_ALLOW_DIRTY_HELP)
        p.add_argument("--allow-stale-dq-gate", action="store_true", help=_ALLOW_STALE_DQ_GATE_HELP)
        p.add_argument(
            "--allow-backdated-veto", action="store_true", help=_ALLOW_BACKDATED_VETO_HELP
        )
        if name == "select":
            # MONITORING never reaches the positions step (T-097), so the flag would be a
            # silent no-op there -- offered only where it can actually do something.
            p.add_argument("--allow-backdated", action="store_true", help=_ALLOW_BACKDATED_HELP)
            _add_construction_args(p)

    undo = sub.add_parser(
        "undo-run",
        help="revert a backdated select run's writes to the live book (T-104); dry run unless "
        "--apply",
        allow_abbrev=False,
    )
    undo.add_argument("--cycle-run", type=int, required=True, help="the backdated cycle_run id")
    undo.add_argument("--db", help="override KG_FINANCIAL_DB path")
    undo.add_argument("--apply", action="store_true", help="write the repair (default: print it)")

    bf = sub.add_parser(
        "backfill",
        help="replay selection cycles across a date range into an isolated simulated "
        "book (T-115) -- never the live portfolio_position",
        allow_abbrev=False,
    )
    bf.add_argument("--from", dest="date_from", required=True)
    bf.add_argument("--to", dest="date_to", required=True)
    bf.add_argument("--step-days", type=int, default=7)
    bf.add_argument("--db", help=_BACKFILL_DB_HELP)
    bf.add_argument("--metrics-version", dest="metrics_version", help=_METRICS_VERSION_HELP)
    bf.add_argument("--allow-stale-prices", action="store_true", help=_ALLOW_STALE_PRICES_HELP)
    bf.add_argument("--allow-dirty", action="store_true", help=_ALLOW_DIRTY_HELP)
    bf.add_argument("--allow-stale-dq-gate", action="store_true", help=_ALLOW_STALE_DQ_GATE_HELP)
    bf.add_argument("--force", action="store_true", help=_FORCE_HELP)
    bf.add_argument("--top-n", type=int, help=_TOP_N_HELP)
    _add_construction_args(bf)
    return parser


def _csv(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _add_construction_args(p: argparse.ArgumentParser) -> None:
    """The book-construction flags shared by `select` and `backfill` (T-136; --top-n is separate)."""
    p.add_argument("--weight-scheme", choices=SCHEMES, help=_SCHEME_HELP)
    p.add_argument("--max-name-weight", type=float, help=_NAME_CAP_HELP)
    p.add_argument("--max-sector-weight", type=float, help=_SECTOR_CAP_HELP)
    p.add_argument("--pin", dest="pins", type=_csv, metavar="TICKERS", help=_PIN_HELP)
    p.add_argument("--exclude", type=_csv, metavar="TICKERS", help=_EXCLUDE_HELP)
    p.add_argument("--exclude-sectors", type=_csv, metavar="SECTORS", help=_EXCLUDE_SECTORS_HELP)
    p.add_argument("--only-sectors", type=_csv, metavar="SECTORS", help=_ONLY_SECTORS_HELP)


# CLI flag (argparse dest) -> CycleSettings field, for flags that carry a value / that switch one on.
_VALUE_FLAGS = {
    "db": "db_path",
    "universe_db": "universe_db_path",
    "top_n": "top_n",
    "metrics_version": "metrics_version",
    "weight_scheme": "weight_scheme",
    "max_name_weight": "max_name_weight",
    "max_sector_weight": "max_sector_weight",
    "pins": "pins",
    "exclude": "exclude",
    "exclude_sectors": "exclude_sectors",
    "only_sectors": "only_sectors",
}
_PATH_FIELDS = {"db_path", "universe_db_path"}
_SWITCH_FLAGS = {
    "allow_backdated": "allow_backdated_positions",
    "allow_stale_prices": "allow_stale_prices",
    "allow_dirty": "allow_dirty",
    "allow_stale_dq_gate": "allow_stale_dq_gate",
    "allow_backdated_veto": "allow_backdated_veto",
}


def _settings(args: argparse.Namespace) -> CycleSettings:
    s = CycleSettings.load()
    updates: dict[str, object] = {}
    for flag, field in _VALUE_FLAGS.items():
        value = getattr(args, flag, None)
        if value is not None and value != "":  # 0 passes through: validation rejects it, loudly
            updates[field] = Path(value) if field in _PATH_FIELDS else value
    for flag, field in _SWITCH_FLAGS.items():
        if getattr(args, flag, False):
            updates[field] = True
    return s.model_copy(update=updates) if updates else s


def _same_database(a: str, b: str) -> bool:
    """True when *a* and *b* name the same file on disk.

    A string/path comparison alone (the first pass) let a relative path, a `..`-laden one, or
    a symlink all name the production database while comparing unequal to its canonical path
    (T-115 review) -- exactly the cases ``--db`` is meant to catch, since a separate database
    is now the only isolation boundary ``backfill`` has. ``os.path.samefile`` compares the
    actual inode when both paths exist (so it sees through a symlink or a relative alias);
    when one doesn't exist yet (a throwaway copy not yet created, or a typo'd production path),
    it falls back to comparing each side's resolved, symlink-following absolute path.
    """
    pa, pb = Path(a).expanduser(), Path(b).expanduser()
    try:
        return os.path.samefile(pa, pb)
    except OSError:
        return pa.resolve() == pb.resolve()


def _refuse_production_backfill(args: argparse.Namespace) -> str | None:
    """T-115 review: only the ``positions`` step is isolated (``portfolio_position_replay``)
    -- every other REPLAY step (score_snapshot, veto, sector_aggregate_snapshot,
    cycle_ranking) writes the same shared tables the live cycle and quant's universe gate
    read. The only full isolation is a separate database, so ``--db`` is mandatory and is
    refused outright when it names the configured production path."""
    prod = database_path(None)  # env only, ignoring args.db, to name the production path
    if not args.db:
        return (
            f"refuses to run without --db (see --help); it never touches the production "
            f"database ({DB_ENV_VAR}) even for a copy's sake -- copy it yourself first"
        )
    if prod and _same_database(args.db, prod):
        return (
            f"refuses to run against the production database ({DB_ENV_VAR}); copy it first "
            "and pass --db pointing at the copy"
        )
    return None


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
    except (
        VersionError,
        ManifestMismatch,
        ConstructionMismatch,
        ScoreWeightsMismatch,
        PreferencesNeedDryRun,
        NoStoredRanking,
        BookInputError,
        OutOfOrderCycle,
        NotBackdated,
        StaleAsOf,
        StaleGateVersion,
        VetoSchemaStale,
        DirtyTree,
        TooManyUnscored,
    ) as exc:
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


def _print_unscored(r: CycleReport) -> None:
    if r.unscored:
        print(f"  {r.unscored} unscored (ineligible): {', '.join(r.unscored_tickers)}")


def _book_line(book: BookResult) -> str:
    """One line: the effective caps, then every relaxation, shortfall and pin note (T-136)."""
    parts = [
        f"book: {book.n_held} of {book.requested_n} names, {book.scheme}, "
        f"name cap {book.max_name_weight:.4g}, sector cap {book.max_sector_weight:.4g}"
    ]
    parts += [
        f"RELAXED {r.cap} cap {r.requested:.4g} -> {r.effective:.4g}" for r in book.relaxations
    ]
    if book.shortfall:
        parts.append(f"SHORTFALL {book.shortfall} (fewer eligible names than N; not padded)")
    parts += [f"REFUSED pin {p.ticker} ({p.reason})" for p in book.refused_pins]
    parts += [f"flagged pin {p.ticker} ({p.reason})" for p in book.flagged_pins]
    if book.overflow_tickers:
        parts.append(f"held past a full sector: {', '.join(book.overflow_tickers)}")
    return "; ".join(parts)


def _print_book(r: CycleReport) -> None:
    if r.book is not None:
        print(f"  {_book_line(r.book)}")


def _print_dry_run_book(d: DryRunBook) -> None:
    """`select --dry-run`: the book itself, then the effective caps, relaxations and pin notes."""
    print(
        f"dry run for {d.cycle_date}: book built on the stored ranking of the "
        f"{d.source_cycle_type} run {d.source_cycle_run_id} (read-only: nothing written)"
    )
    width = max((len(t) for t, _, _ in d.rows), default=6)
    print(f"  {'ticker':<{width}}  {'sector':<28}  weight")
    for ticker, sector, weight in d.rows:
        print(f"  {ticker:<{width}}  {sector or '-':<28}  {weight:.6f}")
    print(f"  total {sum(w for _, _, w in d.rows):.6f}")
    print(f"  {_book_line(d.book)}")
    for rel in d.book.relaxations:
        print(f"    relaxed {rel.cap}: {rel.reason}")


def _print_bypass_warnings(r: CycleReport) -> None:
    if r.stale_price_bypassed is not None:
        print(
            f"  WARNING: --allow-stale-prices overrode the price-spine guard "
            f"({r.stale_price_bypassed})",
            file=sys.stderr,
        )
    if r.backdated_guard_bypassed is not None:
        print(
            f"  WARNING: --allow-backdated overrode the out-of-order-cycle guard "
            f"({r.backdated_guard_bypassed})",
            file=sys.stderr,
        )
    if r.dirty_tree_bypassed is not None:
        print(
            f"  WARNING: --allow-dirty overrode the clean-tree guard ({r.dirty_tree_bypassed})",
            file=sys.stderr,
        )
    if r.stale_dq_gate_bypassed is not None:
        print(
            f"  WARNING: --allow-stale-dq-gate overrode the Ring-1 gate-version guard "
            f"({r.stale_dq_gate_bypassed})",
            file=sys.stderr,
        )
    if r.veto_backdated_bypassed is not None:
        print(
            f"  WARNING: --allow-backdated-veto overrode the veto out-of-order guard "
            f"({r.veto_backdated_bypassed})",
            file=sys.stderr,
        )


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.command == "undo-run":  # needs no model settings
        return _undo_run(args)
    settings = _settings(args)
    if args.command == "select" and args.dry_run:
        # read-only preview from the stored ranking: no cycle step, no hook, no cycle_run
        _print_dry_run_book(dry_run_book(settings, _resolve_cycle_date(parser, args)))
        return 0
    hook = make_hook(settings)

    if args.command == "monitor":
        cycle_date = _resolve_cycle_date(parser, args)
        r = run_monitoring(settings, cycle_date, fundamental_hook=hook)
        print(
            f"monitor {r.cycle_run_id} {r.cycle_date}: {r.vetoed} hard-vetoed "
            f"(manifest {r.manifest_tag})"
        )
        _print_unscored(r)
        _print_bypass_warnings(r)
        return 0
    if args.command == "select":
        cycle_date = _resolve_cycle_date(parser, args)
        r = run_selection(settings, cycle_date, fundamental_hook=hook)
        print(
            f"select {r.cycle_run_id} {r.cycle_date}: {r.selected} selected, "
            f"{r.vetoed} hard-vetoed (steps: {'+'.join(r.steps_run) or 'all skipped'}; "
            f"manifest {r.manifest_tag})"
        )
        _print_book(r)
        _print_unscored(r)
        _print_bypass_warnings(r)
        return 0
    # backfill (T-115: replays into portfolio_position_replay, never the live book)
    refusal = _refuse_production_backfill(args)
    if refusal is not None:
        print(f"cycle backfill: {refusal}", file=sys.stderr)
        return 1
    if args.force:
        conn = connect(resolve_db_path(args.db))
        try:
            ensure_schema(conn)
            reset_replay_range(conn, args.date_from)
        finally:
            conn.close()
        print(f"  --force: reset the replay book from {args.date_from} onward")
    d = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_to)
    while d <= end:
        r = run_replay(settings, d.isoformat(), fundamental_hook=hook)
        print(f"  {d.isoformat()}: {r.selected} selected")
        _print_book(r)
        _print_unscored(r)
        _print_bypass_warnings(r)
        d += timedelta(days=args.step_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
