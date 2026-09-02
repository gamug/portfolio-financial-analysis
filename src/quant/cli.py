"""``python -m quant {backfill-actions,build-returns,build-risk-model,optimize,benchmark,evaluate}``.

Subcommands are filled in milestone by milestone; an unimplemented one prints a
notice and exits 0.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from quant.actions import backfill_corporate_actions
from quant.benchmark import build_internal_benchmark
from quant.config import QuantSettings
from quant.db import connect, ensure_schema
from quant.evaluate import run_evaluate
from quant.persist import run_build_risk_model, run_optimize
from quant.returns import run_build_returns

_TODAY_HELP = "date, YYYY-MM-DD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant", description="Markowitz benchmark portfolio.")
    sub = parser.add_subparsers(dest="command", required=True)

    ba = sub.add_parser("backfill-actions", help="fetch dividends/splits into corporate_action")
    ba.add_argument("--db", help="override KG_FINANCIAL_DB path")
    ba.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    ba.add_argument("--to", dest="date_to", help=f"{_TODAY_HELP} (default: today)")
    ba.add_argument("--source", choices=("gateway", "derive"), default="derive")

    br = sub.add_parser("build-returns", help="derive the total-return daily series")
    br.add_argument("--db", help="override KG_FINANCIAL_DB path")
    br.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    br.add_argument("--to", dest="date_to", help=f"{_TODAY_HELP} (default: today)")

    rm = sub.add_parser("build-risk-model", help="estimate mu / covariance for an as-of date")
    rm.add_argument("--db", help="override KG_FINANCIAL_DB path")
    rm.add_argument("--as-of", dest="as_of", required=True, help=_TODAY_HELP)
    rm.add_argument("--lookback", type=int, help="return-window length in trading days")
    rm.add_argument("--min-history", dest="min_history", type=int)
    rm.add_argument(
        "--cov", dest="cov_estimator", choices=("ledoit_wolf_cc", "ledoit_wolf_diag", "sample")
    )
    rm.add_argument("--model-version", dest="model_version")
    rm.add_argument("--no-store-cov", dest="store_cov", action="store_false")

    op = sub.add_parser("optimize", help="run the objective family and persist the benchmark books")
    op.add_argument("--db", help="override KG_FINANCIAL_DB path")
    op.add_argument("--as-of", dest="as_of", required=True, help=_TODAY_HELP)
    op.add_argument("--objectives", help="comma-separated: min_var,tangency,target_vol,frontier")
    op.add_argument("--frontier-k", dest="frontier_k", type=int)
    op.add_argument("--target-vol", dest="target_vol", type=float)
    op.add_argument("--max-name-weight", dest="max_name_weight", type=float)
    op.add_argument("--max-sector-weight", dest="max_sector_weight", type=float)
    op.add_argument("--turnover-cap", dest="turnover_cap", type=float)
    op.add_argument("--solver")
    op.add_argument("--model-version", dest="model_version")

    bm = sub.add_parser("benchmark", help="build the internal equal-weight benchmark series")
    bm.add_argument("--db", help="override KG_FINANCIAL_DB path")
    bm.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    bm.add_argument("--to", dest="date_to", help=f"{_TODAY_HELP} (default: today)")

    ev = sub.add_parser("evaluate", help="forward realized returns: each book vs the live book")
    ev.add_argument("--db", help="override KG_FINANCIAL_DB path")
    ev.add_argument("--from", dest="date_from", required=True, help=_TODAY_HELP)
    ev.add_argument("--to", dest="date_to", help=f"{_TODAY_HELP} (default: today)")
    ev.add_argument("--benchmark", default="SP500_EW_INTERNAL")
    return parser


_FLAG_TO_FIELD: dict[str, tuple[str, object]] = {
    "db": ("db_path", Path),
    "lookback": ("lookback_days", int),
    "min_history": ("min_history_days", int),
    "cov_estimator": ("cov_estimator", str),
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


def _today() -> str:
    return date.today().isoformat()


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911 - one branch per subcommand
    args = build_parser().parse_args(argv)
    settings = _settings(args)

    if args.command == "backfill-actions":
        report = backfill_corporate_actions(
            settings,
            date_from=args.date_from,
            date_to=args.date_to or _today(),
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
            settings, date_from=args.date_from, date_to=args.date_to or _today()
        )
        print(
            f"build-returns [{rep.engine_version}]: {rep.assets} assets, "
            f"{rep.rows_written} new rows, {rep.assets_with_dividends} with dividends"
        )
        return 0

    if args.command == "build-risk-model":
        res = run_build_risk_model(settings, as_of=args.as_of, store_cov=args.store_cov)
        shr = f"{res.cov_shrinkage:.3f}" if res.cov_shrinkage is not None else "n/a"
        print(
            f"build-risk-model {res.model_id} @ {res.as_of}: {res.n_assets} assets, "
            f"cov={res.cov_estimator} (shrink {shr}), {res.cov_rows} cov rows"
        )
        return 0

    if args.command == "optimize":
        opt = run_optimize(settings, as_of=args.as_of)
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
                date_to=args.date_to or _today(),
                return_engine_version=settings.return_engine_version,
                engine_version=settings.benchmark_engine_version,
            )
        finally:
            conn.close()
        print(f"benchmark SP500_EW_INTERNAL: {n} rows")
        return 0

    if args.command == "evaluate":
        ev = run_evaluate(
            settings,
            date_from=args.date_from,
            date_to=args.date_to or _today(),
            benchmark=args.benchmark,
        )
        print(
            f"evaluate {args.date_from}..{args.date_to or _today()}: {ev.benchmark_rows} "
            f"benchmark rows, {ev.books_evaluated} books, {ev.perf_rows} perf rows"
            + (f", live_book #{ev.live_book_id}" if ev.live_book_id else "")
        )
        return 0

    print(f"quant {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
