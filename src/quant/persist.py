"""Orchestrate gate -> panel -> risk model -> DB, one ``quant_run`` per command."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import numpy as np

from quant.config import QuantSettings
from quant.db import (
    RiskModelMeta,
    connect,
    ensure_schema,
    insert_covariance,
    insert_expected_returns,
    insert_risk_model,
    load_market_caps,
)
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
            conn, "build-risk-model", as_of=as_of, params=settings.model_dump(mode="json")
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
