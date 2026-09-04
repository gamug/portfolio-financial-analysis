"""Orchestrate gate -> panel -> risk model -> DB, one ``quant_run`` per command."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import numpy as np
from portfolio_common.kg_schema.provenance import code_version

from quant.config import QuantSettings
from quant.db import (
    PortfolioRow,
    RiskModelMeta,
    connect,
    ensure_schema,
    insert_covariance,
    insert_expected_returns,
    insert_frontier_points,
    insert_portfolio,
    insert_risk_model,
    load_covariance,
    load_expected_returns,
    load_market_caps,
    load_risk_model,
    load_sector_of,
    sync_positions,
)
from quant.objective import ObjectiveContext, objective_param, resolve_objectives
from quant.optimize import Constraints, efficient_frontier, min_variance
from quant.panel import ReturnPanel, build_return_panel
from quant.rates import load_risk_free
from quant.risk import (
    CovTarget,
    equilibrium_returns,
    historical_mean,
    james_stein_mean,
    ledoit_wolf_covariance,
    sample_covariance,
)
from quant.state import fail_run, finish_run, open_run
from quant.universe import liquidity_data_gate

_W_EPS = 1e-6  # sparsify: drop near-zero weights from the stored book


@dataclass
class RiskModelResult:
    model_id: int
    as_of: str
    n_assets: int
    cov_estimator: str
    cov_shrinkage: float | None
    cov_rows: int
    stored_cov: bool


def _covariance(settings: QuantSettings, panel: ReturnPanel) -> tuple[np.ndarray, float | None]:
    ppy = settings.periods_per_year
    if settings.cov_estimator == "sample":
        return sample_covariance(panel.returns, periods_per_year=ppy), None
    target: CovTarget = (
        "diagonal" if settings.cov_estimator == "ledoit_wolf_diag" else "constant_correlation"
    )
    sigma, delta = ledoit_wolf_covariance(panel.returns, target=target, periods_per_year=ppy)
    return sigma, delta


def _expected_returns(
    settings: QuantSettings,
    panel: ReturnPanel,
    sigma: np.ndarray,
    conn: sqlite3.Connection,
) -> dict[str, dict[int, float]]:
    ppy = settings.periods_per_year
    hist = historical_mean(panel.returns, periods_per_year=ppy)
    js = james_stein_mean(panel.returns, periods_per_year=ppy)
    caps_by_id = load_market_caps(conn, panel.asset_ids)
    caps = np.array([caps_by_id.get(a, 0.0) for a in panel.asset_ids], dtype=np.float64)
    if caps.sum() <= 0:
        caps = np.ones(panel.n_assets)
    eq = equilibrium_returns(sigma, caps, risk_aversion=settings.equilibrium_risk_aversion)
    return {
        "hist_mean": dict(zip(panel.asset_ids, hist.tolist(), strict=True)),
        "james_stein": dict(zip(panel.asset_ids, js.tolist(), strict=True)),
        "equilibrium": dict(zip(panel.asset_ids, eq.tolist(), strict=True)),
    }


def run_build_risk_model(
    settings: QuantSettings,
    *,
    as_of: str,
    conn: sqlite3.Connection | None = None,
    store_cov: bool = True,
) -> RiskModelResult:
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        run_id = open_run(
            conn,
            "build-risk-model",
            as_of=as_of,
            params=settings.model_dump(mode="json"),
            code_version=code_version(),
        )
        try:
            gate = liquidity_data_gate(
                conn,
                as_of=as_of,
                universe=settings.universe,
                min_history_days=settings.min_history_days,
                min_dollar_volume=settings.liquidity_min_dollar_volume,
                liquidity_lookback_days=settings.liquidity_lookback_days,
                exclude_hard_vetoed=settings.exclude_hard_vetoed,
                universe_db_path=settings.universe_db_path,
            )
            if not gate.asset_ids:
                raise RuntimeError(f"universe gate is empty as of {as_of}")  # noqa: TRY301
            panel = build_return_panel(
                conn,
                as_of=as_of,
                lookback_days=settings.lookback_days,
                min_history_days=settings.min_history_days,
                universe_asset_ids=gate.asset_ids,
                return_engine_version=settings.return_engine_version,
            )
            sigma, delta = _covariance(settings, panel)
            rf = load_risk_free(settings, as_of=as_of, conn=conn)
            mu_by_model = _expected_returns(settings, panel, sigma, conn)

            spec = {
                "asset_ids": panel.asset_ids,
                "date_start": panel.dates[0],
                "date_end": panel.dates[-1],
                "n_dates": len(panel.dates),
                "sha256": panel.spec_sha256,
            }
            model_id = insert_risk_model(
                conn,
                RiskModelMeta(
                    as_of=as_of,
                    model_version=settings.risk_model_version,
                    lookback_days=settings.lookback_days,
                    min_history_days=settings.min_history_days,
                    n_assets=panel.n_assets,
                    cov_estimator=settings.cov_estimator,
                    cov_shrinkage=delta,
                    ret_estimator=settings.ret_estimator,
                    periods_per_year=settings.periods_per_year,
                    panel_engine_version=settings.return_engine_version,
                    panel_spec_json=json.dumps(spec, separators=(",", ":")),
                    rf_annual=rf.annualized_rate,
                    params_json=json.dumps(settings.model_dump(mode="json"), default=str),
                    quant_run_id=run_id,
                ),
            )
            insert_expected_returns(conn, model_id, mu_by_model)
            cov_rows = insert_covariance(conn, model_id, panel.asset_ids, sigma) if store_cov else 0
            finish_run(conn, run_id)
        except Exception as exc:
            fail_run(conn, run_id, str(exc))
            raise
        return RiskModelResult(
            model_id=model_id,
            as_of=as_of,
            n_assets=panel.n_assets,
            cov_estimator=settings.cov_estimator,
            cov_shrinkage=delta,
            cov_rows=cov_rows,
            stored_cov=store_cov,
        )
    finally:
        if owns:
            conn.close()


# -- optimize ---------------------------------------------------------------


@dataclass
class OptimizeRunResult:
    model_id: int
    as_of: str
    books: dict[str, int]  # kind -> quant_portfolio.id
    frontier_points: int


def _weights_json(ids: list[int], w: np.ndarray) -> str:
    return json.dumps(
        {str(ids[i]): round(float(v), 8) for i, v in enumerate(w) if abs(float(v)) > _W_EPS},
        separators=(",", ":"),
    )


def _resolve_model_id(
    settings: QuantSettings, as_of: str, conn: sqlite3.Connection, run_id: int
) -> int:
    row = load_risk_model(conn, as_of=as_of, model_version=settings.risk_model_version)
    if row is not None:
        return int(row["id"])
    build = run_build_risk_model(settings, as_of=as_of, conn=conn)
    conn.execute("UPDATE quant_run SET status = 'running' WHERE id = ?", (run_id,))
    return build.model_id


def _target_vol(
    settings: QuantSettings,
    sigma: np.ndarray,
    mu: np.ndarray,
    cons: Constraints,
) -> float:
    if settings.target_volatility is not None:
        return settings.target_volatility
    mv = min_variance(sigma, constraints=cons, mu=mu, solver=settings.solver)
    return mv.expected_vol * 1.25


def run_optimize(
    settings: QuantSettings,
    *,
    as_of: str,
    conn: sqlite3.Connection | None = None,
) -> OptimizeRunResult:
    owns = conn is None
    conn = conn or connect(settings.db_path)
    try:
        ensure_schema(conn)
        run_id = open_run(
            conn,
            "optimize",
            as_of=as_of,
            params=settings.model_dump(mode="json"),
            code_version=code_version(),
        )
        try:
            model_id = _resolve_model_id(settings, as_of, conn, run_id)
            ids, sigma = load_covariance(conn, model_id)
            if not ids:
                raise RuntimeError(  # noqa: TRY301
                    "no stored covariance for this risk model; "
                    "re-run build-risk-model without --no-store-cov"
                )
            mu_map = load_expected_returns(conn, model_id, settings.ret_estimator)
            mu = np.array([mu_map[a] for a in ids], dtype=np.float64)
            rm = load_risk_model(conn, as_of=as_of, model_version=settings.risk_model_version)
            rf = (
                float(rm["rf_annual"])
                if rm and rm["rf_annual"] is not None
                else (settings.risk_free_rate)
            )
            cons = Constraints(
                max_name_weight=settings.max_name_weight,
                min_name_weight=settings.min_name_weight,
                max_sector_weight=settings.max_sector_weight,
                sector_of=load_sector_of(conn, ids),
                turnover_cap=settings.turnover_cap,
                asset_ids=ids,
            )
            tv = _target_vol(settings, sigma, mu, cons)
            ctx = ObjectiveContext(
                sigma=sigma,
                mu=mu,
                rf=rf,
                constraints=cons,
                target_volatility=tv,
                solver=settings.solver,
            )

            books: dict[str, int] = {}
            for name, build in resolve_objectives(settings.objectives):
                res = build(ctx)
                pid = insert_portfolio(
                    conn,
                    PortfolioRow(
                        as_of=as_of,
                        kind=name,
                        objective=res.objective,
                        solver=res.solver,
                        status=res.status,
                        expected_return=res.expected_return,
                        expected_vol=res.expected_vol,
                        sharpe=res.sharpe,
                        rf_annual=rf,
                        n_positions=int((np.abs(res.weights) > _W_EPS).sum()),
                        engine_version=settings.optimizer_engine_version,
                        target_param=objective_param(name, ctx),
                        model_id=model_id,
                        quant_run_id=run_id,
                    ),
                )
                sync_positions(
                    conn,
                    pid,
                    as_of,
                    {ids[i]: float(v) for i, v in enumerate(res.weights) if abs(float(v)) > _W_EPS},
                )
                books[name] = pid

            frontier_points = 0
            if "frontier" in settings.objectives:
                pts = efficient_frontier(
                    mu,
                    sigma,
                    k=settings.frontier_k,
                    constraints=cons,
                    rf=rf,
                    solver=settings.solver,
                )
                frontier_points = insert_frontier_points(
                    conn,
                    model_id,
                    [
                        (
                            p.k,
                            p.target_return,
                            p.expected_return,
                            p.expected_vol,
                            p.sharpe,
                            p.status,
                            _weights_json(ids, p.weights),
                        )
                        for p in pts
                    ],
                )
            finish_run(conn, run_id)
        except Exception as exc:
            fail_run(conn, run_id, str(exc))
            raise
        return OptimizeRunResult(
            model_id=model_id, as_of=as_of, books=books, frontier_points=frontier_points
        )
    finally:
        if owns:
            conn.close()
