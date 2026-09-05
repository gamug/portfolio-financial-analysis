"""run_build_risk_model persists metadata + mu + covariance, reproducibly."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
from portfolio_common.db import Database

from quant.actions import backfill_corporate_actions
from quant.config import QuantSettings
from quant.db import load_covariance
from quant.persist import run_build_risk_model
from quant.returns import run_build_returns


def _prep(conn: Database) -> QuantSettings:
    s = QuantSettings(
        db_path=Path(":memory:"),
        lookback_days=220,
        min_history_days=150,
        liquidity_min_dollar_volume=0.0,
    )
    backfill_corporate_actions(
        s, date_from="2000-01-01", date_to="2100-01-01", source="derive", conn=conn
    )
    run_build_returns(s, date_from="2000-01-01", date_to="2100-01-01", conn=conn)
    return s


def test_risk_model_round_trip(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn)
    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]

    res = run_build_risk_model(settings, as_of=as_of, conn=conn)
    n = res.n_assets
    assert n == 6
    assert 0.0 <= (res.cov_shrinkage or 0.0) <= 1.0

    assert conn.execute("SELECT COUNT(*) FROM quant_risk_model").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM quant_covariance").fetchone()[0] == n * (n + 1) // 2
    assert res.cov_rows == n * (n + 1) // 2
    # three mu models, one row per asset each
    per_model = dict(
        conn.execute("SELECT mu_model, COUNT(*) FROM quant_expected_return GROUP BY mu_model")
    )
    assert per_model == {"hist_mean": n, "james_stein": n, "equilibrium": n}

    ids, sigma = load_covariance(conn, res.model_id)
    assert len(ids) == n
    assert np.allclose(sigma, sigma.T)
    assert np.linalg.eigvalsh(sigma).min() > -1e-8

    # v_quant_risk_model exposes the metadata; panel spec is reproducible
    row = conn.execute("SELECT * FROM v_quant_risk_model").fetchone()
    assert row["n_assets"] == n
    assert '"sha256"' in row["panel_spec_json"]

    # re-run upserts, does not duplicate
    res2 = run_build_risk_model(settings, as_of=as_of, conn=conn)
    assert res2.model_id == res.model_id
    assert conn.execute("SELECT COUNT(*) FROM quant_risk_model").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM quant_covariance").fetchone()[0] == n * (n + 1) // 2


def test_no_store_cov_skips_the_matrix(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=4, n_days=260, with_dividends=False)
    settings = _prep(conn)
    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    res = run_build_risk_model(settings, as_of=as_of, conn=conn, store_cov=False)
    assert res.cov_rows == 0
    assert conn.execute("SELECT COUNT(*) FROM quant_covariance").fetchone()[0] == 0
    # metadata + mu still land
    assert conn.execute("SELECT COUNT(*) FROM quant_risk_model").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM quant_expected_return").fetchone()[0] == 3 * 4
