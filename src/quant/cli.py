"""``python -m quant {backfill-actions,build-returns,build-risk-model,optimize,benchmark,evaluate,versions}``.

Subcommands are filled in milestone by milestone; an unimplemented one prints a
notice and exits 0.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from kg_schema import connect
from kg_schema.cli import add_coverage_parser, coverage_from_args
from kg_schema.rundate import add_analysis_date_argument
from kg_schema.rundate import resolve as resolve_analysis_date
from kg_schema.versions import VersionError
from quant.actions import DividendsNotReady, GatewayUnavailable, backfill_corporate_actions
from quant.benchmark import build_internal_benchmark
from quant.config import QuantSettings
from quant.db import ActionsReport, ensure_schema
from quant.evaluate import run_evaluate
from quant.persist import (
    DryRunPlan,
    plan_build_risk_model,
    plan_optimize,
    run_build_risk_model,
    run_optimize,
)
from quant.profiles import load_profile
from quant.returns import run_build_returns
from quant.versions_report import versions_report

_TODAY_HELP = "date, YYYY-MM-DD"
_METRICS_VERSION_HELP = (
    "which fundamental_metrics engine version the risk model reads: a version (metrics-v1), "
    "GROUP=VERSION pairs (valuation=metrics-v1), or a constraint (>=metrics-v2, !=metrics-v1, "
    "combinations, latest; T-093); default: the newest stored. Runs over different versions "
    "write parallel books (T-090)"
)
_RETURNS_VERSION_HELP = (
    "constraint on the return series read (qret-v2, >=qret-v2, !=qret-v1, latest); resolved "
    "strictly against what is stored. Default: the configured return engine"
)
_RISK_MODEL_VERSION_HELP = (
    "constraint on the stored risk model optimize reads (rm-v1, >=rm-v1, latest), matched "
    "among models for this as-of over the same inputs. Default: --model-version (loaded, or "
    "built when missing)"
)
_CORPACT_VERSION_HELP = (
    "pin one corporate-action engine (corpact-v1, >=corpact-v1, latest) instead of the "
    "per-asset priority. The series is append-only per return engine, so a different choice "
    "only takes effect under a new return engine version"
)


def _add_profile(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--version-profile",
        dest="version_profile",
        metavar="FILE",
        help="TOML file of named version-constraint profiles; flags win over it (T-093)",
    )
    sub.add_argument(
        "--profile",
        dest="profile_name",
        metavar="NAME",
        help="which profile in --version-profile (optional when the file holds one)",
    )


def _add_dry_run(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved manifest and the rows it would key; write nothing",
    )


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--db", help="override KG_FINANCIAL_DB path")
    sub.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    add_analysis_date_argument(sub)


def _add_build_risk_model_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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
    rm.add_argument("--metrics-version", dest="metrics_version", help=_METRICS_VERSION_HELP)
    rm.add_argument("--returns-version", dest="returns_version", help=_RETURNS_VERSION_HELP)
    rm.add_argument("--no-store-cov", dest="store_cov", action="store_false")
    _add_profile(rm)
    _add_dry_run(rm)


def _add_optimize_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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
    op.add_argument("--metrics-version", dest="metrics_version", help=_METRICS_VERSION_HELP)
    op.add_argument("--returns-version", dest="returns_version", help=_RETURNS_VERSION_HELP)
    op.add_argument("--risk-model-version", dest="risk_model_select", help=_RISK_MODEL_VERSION_HELP)
    _add_profile(op)
    _add_dry_run(op)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant", description="Markowitz benchmark portfolio.")
    sub = parser.add_subparsers(dest="command", required=True)

    _AS_OF_HELP = f"{_TODAY_HELP} (clamped to --analysis-date; default: --analysis-date)"

    ba = sub.add_parser("backfill-actions", help="fetch dividends/splits into corporate_action")
    _add_common(ba)
    ba.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    ba.add_argument("--to", dest="date_to", help=_AS_OF_HELP)

    br = sub.add_parser("build-returns", help="derive the total-return daily series")
    _add_common(br)
    br.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    br.add_argument("--to", dest="date_to", help=_AS_OF_HELP)
    br.add_argument(
        "--allow-no-dividends",
        action="store_true",
        help="build even though no clean gateway backfill-actions covers the window: the "
        "series is price-only and locks in under the return engine version (T-086)",
    )
    br.add_argument("--corpact-version", dest="corpact_version", help=_CORPACT_VERSION_HELP)
    _add_profile(br)

    _add_build_risk_model_parser(sub)
    _add_optimize_parser(sub)

    bm = sub.add_parser("benchmark", help="build the internal equal-weight benchmark series")
    _add_common(bm)
    bm.add_argument("--from", dest="date_from", default="2022-01-01", help=_TODAY_HELP)
    bm.add_argument("--to", dest="date_to", help=_AS_OF_HELP)

    ev = sub.add_parser("evaluate", help="forward realized returns: each book vs the live book")
    _add_common(ev)
    ev.add_argument(
        "--from",
        dest="date_from",
        help=f"{_TODAY_HELP} (default: the earliest optimized book's as-of)",
    )
    ev.add_argument("--to", dest="date_to", help=_AS_OF_HELP)
    ev.add_argument("--benchmark", default="SP500_EW_INTERNAL")

    vs = sub.add_parser(
        "versions", help="list the stored versions of every input quant can be constrained on"
    )
    vs.add_argument("--db", help="override KG_FINANCIAL_DB path")

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
    "metrics_version": ("metrics_version", str),
    "returns_version": ("returns_version", str),
    "risk_model_select": ("risk_model_select", str),
    "corpact_version": ("corpact_version", str),
    "frontier_k": ("frontier_k", int),
    "target_vol": ("target_volatility", float),
    "max_name_weight": ("max_name_weight", float),
    "max_sector_weight": ("max_sector_weight", float),
    "turnover_cap": ("turnover_cap", float),
    "solver": ("solver", str),
}


def _settings(args: argparse.Namespace) -> QuantSettings:
    """Env, then the named version profile (if any), then the flags -- a flag wins."""
    s = QuantSettings.load()
    updates: dict[str, object] = {}
    profile_path = getattr(args, "version_profile", None)
    if profile_path:
        profile = load_profile(profile_path, getattr(args, "profile_name", None))
        updates.update(profile.settings_updates())
        updates["version_profile"] = profile.label
    elif getattr(args, "profile_name", None):
        raise VersionError("--profile needs --version-profile FILE")
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


def _print_plan(plan: DryRunPlan) -> None:
    m = plan.manifest
    state = "stored, would be reused" if plan.model_stored else "not stored, would be built"
    print(f"{plan.command} --dry-run @ {plan.as_of} (nothing written)")
    print(f"  manifest: {m.json()}")
    print(f"  manifest tag: {m.tag}" + (f", book tag: {m.book_tag}" if plan.book_version else ""))
    print(f"  risk model: {plan.model_version} ({state})")
    if plan.book_version:
        print(f"  books: engine_version {plan.book_version}")


def _dry_run(settings: QuantSettings, command: str, as_of: str) -> int:
    conn = connect(settings.db_path, read_only=True)
    try:
        planner = plan_optimize if command == "optimize" else plan_build_risk_model
        _print_plan(planner(settings, as_of=as_of, conn=conn))
    except VersionError as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _run_versions(settings: QuantSettings) -> int:
    conn = connect(settings.db_path, read_only=True)
    try:
        report = versions_report(conn, settings)
    finally:
        conn.close()
    print(report)
    return 0


def _run_build_risk_model(settings: QuantSettings, as_of: str, *, store_cov: bool) -> int:
    try:
        res = run_build_risk_model(settings, as_of=as_of, store_cov=store_cov)
    except VersionError as exc:
        print(f"build-risk-model: {exc}", file=sys.stderr)
        return 1
    shr = f"{res.cov_shrinkage:.3f}" if res.cov_shrinkage is not None else "n/a"
    print(
        f"build-risk-model {res.model_id} @ {res.as_of}: {res.n_assets} assets, "
        f"cov={res.cov_estimator} (shrink {shr}), {res.cov_rows} cov rows, "
        f"manifest {res.manifest_tag}"
    )
    return 0


def _run_optimize(settings: QuantSettings, as_of: str) -> int:
    try:
        opt = run_optimize(settings, as_of=as_of)
    except VersionError as exc:
        print(f"optimize: {exc}", file=sys.stderr)
        return 1
    books = ", ".join(f"{k}#{v}" for k, v in opt.books.items())
    print(
        f"optimize @ {opt.as_of} (model {opt.model_id}): books [{books}], "
        f"{opt.frontier_points} frontier points, manifest {opt.manifest_tag}"
    )
    return 0


def _print_actions_report(report: ActionsReport) -> None:
    print(
        f"backfill-actions [gateway]: {report.assets_fetched} of {report.assets_seen} assets "
        f"fetched, {report.dividends} dividends + {report.splits} splits, "
        f"{report.inserted} new rows ({report.engine_version})"
    )
    if report.errors:
        print(
            f"  {len(report.errors)} asset(s) got no data from the gateway and have no rows "
            f"written; first: {report.errors[0]}",
            file=sys.stderr,
        )


def _run_backfill_actions(settings: QuantSettings, args: argparse.Namespace, date_to: str) -> int:
    """The gateway is the only source: exit 1 when it cannot serve at all, and also when
    it left any asset without data (the dividend series would be incomplete)."""
    try:
        report = backfill_corporate_actions(settings, date_from=args.date_from, date_to=date_to)
    except GatewayUnavailable as exc:
        print(f"backfill-actions: {exc}", file=sys.stderr)
        return 1
    _print_actions_report(report)
    return 1 if report.errors else 0


def _run_build_returns(settings: QuantSettings, args: argparse.Namespace, date_to: str) -> int:
    """Exit 1 when the dividends guard refuses (T-086); a bypass is loudly reported."""
    try:
        rep = run_build_returns(
            settings,
            date_from=args.date_from,
            date_to=date_to,
            allow_no_dividends=args.allow_no_dividends,
        )
    except VersionError as exc:
        print(f"build-returns: {exc}", file=sys.stderr)
        return 1
    except DividendsNotReady as exc:
        print(
            f"build-returns: refusing to build a total-return series -- {exc}.\n"
            "  Run `python -m quant backfill-actions` first (it must finish with no errored "
            "assets), or pass --allow-no-dividends to build a price-only series knowingly.",
            file=sys.stderr,
        )
        return 1
    print(
        f"build-returns [{rep.engine_version}]: {rep.assets} assets, "
        f"{rep.rows_written} new rows, {rep.assets_with_dividends} with dividends"
        + (f", corporate actions from {rep.corpact_engine}" if rep.corpact_engine else "")
    )
    if rep.corpact_engine and rep.assets and not rep.rows_written:
        print(
            f"  NOTE: no new rows -- {rep.engine_version} is append-only, so rows already stored "
            f"there were kept; a different corporate-action choice only takes effect under a "
            f"new return engine version",
            file=sys.stderr,
        )
    if rep.dividends_guard_bypassed is not None:
        print(
            f"  WARNING: --allow-no-dividends overrode the dividends guard "
            f"({rep.dividends_guard_bypassed}); the series is price-only where dividends are "
            f"missing and is locked in under {rep.engine_version}",
            file=sys.stderr,
        )
    return 0


def _prepare(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> int | tuple[QuantSettings, str]:
    """Settings and the analysis date -- or an exit code when the command is already done:
    a bad profile (1), ``versions``, or a ``--dry-run`` (T-093)."""
    try:
        settings = _settings(args)
    except VersionError as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 1
    if args.command == "versions":
        return _run_versions(settings)
    if getattr(args, "risk_model_select", None) and getattr(args, "model_version", None):
        parser.error("--risk-model-version and --model-version both pick the risk model; use one")
    analysis_date = _analysis_date(parser, args)
    if getattr(args, "dry_run", False):
        return _dry_run(settings, args.command, analysis_date)
    return settings, analysis_date


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911 - one branch per subcommand
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "coverage":
        return coverage_from_args(args)

    prepared = _prepare(parser, args)
    if isinstance(prepared, int):
        return prepared
    settings, analysis_date = prepared

    if args.command == "backfill-actions":
        return _run_backfill_actions(settings, args, _date_to(analysis_date, args))

    if args.command == "build-returns":
        return _run_build_returns(settings, args, _date_to(analysis_date, args))

    if args.command == "build-risk-model":
        return _run_build_risk_model(settings, analysis_date, store_cov=args.store_cov)

    if args.command == "optimize":
        return _run_optimize(settings, analysis_date)

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
            f"evaluate {ev.date_from}..{date_to}: {ev.benchmark_rows} "
            f"benchmark rows, {ev.books_evaluated} books, {ev.perf_rows} perf rows"
            + (f", live_book #{ev.live_book_id}" if ev.live_book_id else "")
        )
        return 0

    print(f"quant {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
