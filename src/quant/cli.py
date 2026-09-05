"""``python -m quant {backfill-actions,build-returns,build-risk-model,optimize,benchmark,evaluate}``.

Subcommands are filled in milestone by milestone; an unimplemented one prints a
notice and exits 0.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from kg_schema import connect
from kg_schema.cli import add_coverage_parser, coverage_from_args
from kg_schema.rundate import add_analysis_date_argument
from kg_schema.rundate import resolve as resolve_analysis_date
from quant.actions import backfill_corporate_actions
from quant.benchmark import build_internal_benchmark
from quant.config import QuantSettings
from quant.db import ensure_schema
from quant.evaluate import run_evaluate
from quant.persist import run_build_risk_model, run_optimize
from quant.returns import run_build_returns

_TODAY_HELP = "date, YYYY-MM-DD"


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--db", help="override KG_FINANCIAL_DB path")
    sub.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    add_analysis_date_argument(sub)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant", description="Markowitz benchmark portfolio.")
    sub = parser.add_subparsers(dest="command", required=True)

    _AS_OF_HELP = f"{_TODAY_HELP} (clamped to --analysis-date; default: --analysis-date)"

    ba = sub.add_parser("backfill-actions", help="fetch dividends/splits into corporate_action")
    _add_common(ba)
    ba.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    ba.add_argument("--to", dest="date_to", help=_AS_OF_HELP)
    ba.add_argument("--source", choices=("gateway", "derive"), default="derive")

    br = sub.add_parser("build-returns", help="derive the total-return daily series")
    _add_common(br)
    br.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    br.add_argument("--to", dest="date_to", help=_AS_OF_HELP)

    rm = sub.add_parser("build-risk-model", help="estimate mu / covariance for an as-of date")
    _add_common(rm)
    rm.add_argument(
        "--as-of", dest="as_of", help="alias of --analysis-date (default: --analysis-date)"
    )
    rm.add_argument("--lookback", type=int, help="return-window length in trading days")
    rm.add_argument("--min-history", dest="min_history", type=int)
    rm.add_argument(
        "--cov", dest="cov_estimator", choices=("ledoit_wolf_cc", "ledoit_wolf_diag", "sample")
    )
    rm.add_argument("--model-version", dest="model_version")
    rm.add_argument("--no-store-cov", dest="store_cov", action="store_false")

    op = sub.add_parser("optimize", help="run the objective family and persist the benchmark books")
    _add_common(op)
    op.add_argument(
        "--as-of", dest="as_of", help="alias of --analysis-date (default: --analysis-date)"
    )
    op.add_argument("--objectives", help="comma-separated: min_var,tangency,target_vol,frontier")
    op.add_argument("--frontier-k", dest="frontier_k", type=int)
    op.add_argument("--target-vol", dest="target_vol", type=float)
    op.add_argument("--max-name-weight", dest="max_name_weight", type=float)
    op.add_argument("--max-sector-weight", dest="max_sector_weight", type=float)
    op.add_argument("--turnover-cap", dest="turnover_cap", type=float)
    op.add_argument(
        "--mu", dest="ret_estimator", choices=("equilibrium", "james_stein", "hist_mean")
    )
    op.add_argument("--solver")
    op.add_argument("--model-version", dest="model_version")

    bm = sub.add_parser("benchmark", help="build the internal equal-weight benchmark series")
    _add_common(bm)
    bm.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    bm.add_argument("--to", dest="date_to", help=_AS_OF_HELP)

    ev = sub.add_parser("evaluate", help="forward realized returns: each book vs the live book")
    _add_common(ev)
    ev.add_argument("--from", dest="date_from", required=True, help=_TODAY_HELP)
    ev.add_argument("--to", dest="date_to", help=_AS_OF_HELP)
    ev.add_argument("--benchmark", default="SP500_EW_INTERNAL")

    add_coverage_parser(sub)
    return parser


_FLAG_TO_FIELD: dict[str, tuple[str, object]] = {
    "db": ("db_path", Path),
    "universe_db": ("universe_db_path", Path),
    "lookback": ("lookback_days", int),
    "min_history": ("min_history_days", int),
    "cov_estimator": ("cov_estimator", str),
    "ret_estimator": ("ret_estimator", str),
    "model_version": ("risk_model_version", str),
    "frontier_k": ("frontier_k", int),
    "target_vol": ("target_volatility", float),
    "max_name_weight": ("max_name_weight", float),
    "max_sector_weight": ("max_sector_weight", float),
    "turnover_cap": ("turnover_cap", float),
    "solver": ("solver", str),
}


def _settings(args: argparse.Namespace) -> QuantSettings:
    s = QuantSettings.load()
    updates: dict[str, object] = {}
    for flag, (field, cast) in _FLAG_TO_FIELD.items():
        val = getattr(args, flag, None)
        if val is not None:
            updates[field] = cast(val)  # type: ignore[operator]
    objectives = getattr(args, "objectives", None)
    if objectives:
        updates["objectives"] = [o.strip() for o in objectives.split(",") if o.strip()]
    return s.model_copy(update=updates) if updates else s


def _analysis_date(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """``--analysis-date`` (default today), with ``--as-of`` accepted as an alias.
    Passing both with different values is an error."""
    given = getattr(args, "analysis_date", None)
    as_of = getattr(args, "as_of", None)
    if given and as_of and given != as_of:
        parser.error(f"--as-of ({as_of}) and --analysis-date ({given}) disagree")
    return resolve_analysis_date(given or as_of)


def _date_to(analysis_date: str, args: argparse.Namespace) -> str:
    """The range end: ``--to`` clamped to the analysis date, else the analysis date."""
    dt = getattr(args, "date_to", None)
    return min(dt, analysis_date) if dt else analysis_date


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911 - one branch per subcommand
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "coverage":
        return coverage_from_args(args)

    settings = _settings(args)
    analysis_date = _analysis_date(parser, args)

    if args.command == "backfill-actions":
        report = backfill_corporate_actions(
            settings,
            date_from=args.date_from,
            date_to=_date_to(analysis_date, args),
            source=args.source,
        )
        probe = " (gateway probe failed -> derived)" if report.gateway_probe_failed else ""
        print(
            f"backfill-actions [{report.source}{probe}]: {report.assets_seen} assets, "
            f"{report.dividends} dividends + {report.splits} splits, "
            f"{report.inserted} new rows ({report.engine_version})"
        )
        if report.errors:
            print(f"  {len(report.errors)} asset(s) errored; first: {report.errors[0]}")
        return 0

    if args.command == "build-returns":
        rep = run_build_returns(
            settings, date_from=args.date_from, date_to=_date_to(analysis_date, args)
        )
        print(
            f"build-returns [{rep.engine_version}]: {rep.assets} assets, "
            f"{rep.rows_written} new rows, {rep.assets_with_dividends} with dividends"
        )
        return 0

    if args.command == "build-risk-model":
        res = run_build_risk_model(settings, as_of=analysis_date, store_cov=args.store_cov)
        shr = f"{res.cov_shrinkage:.3f}" if res.cov_shrinkage is not None else "n/a"
        print(
            f"build-risk-model {res.model_id} @ {res.as_of}: {res.n_assets} assets, "
            f"cov={res.cov_estimator} (shrink {shr}), {res.cov_rows} cov rows"
        )
        return 0

    if args.command == "optimize":
        opt = run_optimize(settings, as_of=analysis_date)
        books = ", ".join(f"{k}#{v}" for k, v in opt.books.items())
        print(
            f"optimize @ {opt.as_of} (model {opt.model_id}): books [{books}], "
            f"{opt.frontier_points} frontier points"
        )
        return 0

    if args.command == "benchmark":
        conn = connect(settings.db_path)
        try:
            ensure_schema(conn)
            n = build_internal_benchmark(
                conn,
                date_from=args.date_from,
                date_to=_date_to(analysis_date, args),
                return_engine_version=settings.return_engine_version,
                engine_version=settings.benchmark_engine_version,
            )
        finally:
            conn.close()
        print(f"benchmark SP500_EW_INTERNAL: {n} rows")
        return 0

    if args.command == "evaluate":
        date_to = _date_to(analysis_date, args)
        ev = run_evaluate(
            settings,
            date_from=args.date_from,
            date_to=date_to,
            benchmark=args.benchmark,
        )
        print(
            f"evaluate {args.date_from}..{date_to}: {ev.benchmark_rows} "
            f"benchmark rows, {ev.books_evaluated} books, {ev.perf_rows} perf rows"
            + (f", live_book #{ev.live_book_id}" if ev.live_book_id else "")
        )
        return 0

    print(f"quant {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
