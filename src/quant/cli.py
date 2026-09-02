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
from quant.config import QuantSettings
from quant.persist import run_build_risk_model
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

    for name, helptext in (
        ("optimize", "run the objective family and persist the benchmark books"),
        ("benchmark", "build the internal equal-/cap-weight benchmark series"),
        ("evaluate", "forward realized returns: each book vs the live portfolio_position"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--db", help="override KG_FINANCIAL_DB path")
    return parser


_FLAG_TO_FIELD = {
    "db": ("db_path", Path),
    "lookback": ("lookback_days", int),
    "min_history": ("min_history_days", int),
    "cov_estimator": ("cov_estimator", str),
    "model_version": ("risk_model_version", str),
}


def _settings(args: argparse.Namespace) -> QuantSettings:
    s = QuantSettings.load()
    updates: dict[str, object] = {}
    for flag, (field, cast) in _FLAG_TO_FIELD.items():
        val = getattr(args, flag, None)
        if val is not None:
            updates[field] = cast(val)
    return s.model_copy(update=updates) if updates else s


def _today() -> str:
    return date.today().isoformat()


def main(argv: Sequence[str] | None = None) -> int:
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

    print(f"quant {args.command}: not yet implemented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
