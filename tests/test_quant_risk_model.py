"""run_build_risk_model persists metadata + mu + covariance, reproducibly."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
from portfolio_common.db import Database

import quant.persist as quant_persist
from kg_schema.provenance import DirtyTree
from kg_schema.queries import StaleAsOf
from quant.config import QuantSettings
from quant.db import load_covariance, load_expected_returns
from quant.persist import run_build_risk_model, run_optimize
from quant.returns import run_build_returns


def _prep(conn: Database) -> QuantSettings:
    s = QuantSettings(
        db_path=Path(":memory:"),
        lookback_days=220,
        min_history_days=150,
        liquidity_min_dollar_volume=0.0,
    )
    # These tests seed price-only history on purpose (with_dividends=False).
    run_build_returns(
        s, date_from="2000-01-01", date_to="2100-01-01", conn=conn, allow_no_dividends=True
    )
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


def test_equilibrium_mu_is_a_total_return_like_the_other_two_estimators(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """T-109: ``equilibrium`` must add back *rf*, the same total-return convention
    ``hist_mean``/``james_stein`` already follow -- otherwise every downstream Sharpe/tangency
    (which subtracts *rf* once, expecting a total-return mu) subtracts it twice."""
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn)
    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]

    res_lo = run_build_risk_model(settings, as_of=as_of, conn=conn)
    hist_lo = load_expected_returns(conn, res_lo.model_id, "hist_mean")
    js_lo = load_expected_returns(conn, res_lo.model_id, "james_stein")
    eq_lo = load_expected_returns(conn, res_lo.model_id, "equilibrium")

    hi_rf = settings.risk_free_rate + 0.05
    res_hi = run_build_risk_model(
        settings.model_copy(update={"risk_free_rate": hi_rf}), as_of=as_of, conn=conn
    )
    assert res_hi.model_id == res_lo.model_id  # same (as_of, version): upserted in place
    hist_hi = load_expected_returns(conn, res_hi.model_id, "hist_mean")
    js_hi = load_expected_returns(conn, res_hi.model_id, "james_stein")
    eq_hi = load_expected_returns(conn, res_hi.model_id, "equilibrium")

    # hist_mean/james_stein never touch rf
    for aid in hist_lo:
        assert hist_hi[aid] == pytest.approx(hist_lo[aid], abs=1e-12)
        assert js_hi[aid] == pytest.approx(js_lo[aid], abs=1e-12)
    # equilibrium shifts by exactly the rf delta, uniformly across every asset
    for aid in eq_lo:
        assert eq_hi[aid] == pytest.approx(eq_lo[aid] + 0.05, abs=1e-9)


def test_book_sharpe_is_invariant_to_rf_once_mu_is_a_genuine_total_return(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """T-109: for a book whose weights don't depend on mu (``min_var``), the reported Sharpe
    is ``w . (rf + excess) - rf) / vol == w . excess / vol`` -- unchanged by *rf* -- while
    ``expected_return`` shifts by exactly the *rf* delta. Under the pre-fix bug (equilibrium mu
    missing *rf* entirely), ``expected_return`` would have stayed flat across the two runs
    below and ``sharpe`` would have moved instead: this test fails on that bug."""
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn).model_copy(
        update={"objectives": ["min_var"], "max_name_weight": None, "max_sector_weight": None}
    )
    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]

    def _book(s: QuantSettings) -> tuple[float, float, float]:
        run_build_risk_model(s, as_of=as_of, conn=conn)
        opt = run_optimize(s, as_of=as_of, conn=conn)
        row = conn.execute(
            "SELECT expected_return, expected_vol, sharpe FROM quant_portfolio WHERE id = ?",
            (opt.books["min_var"],),
        ).fetchone()
        return float(row["expected_return"]), float(row["expected_vol"]), float(row["sharpe"])

    er_lo, vol_lo, sharpe_lo = _book(settings)
    er_hi, vol_hi, sharpe_hi = _book(
        settings.model_copy(update={"risk_free_rate": settings.risk_free_rate + 0.05})
    )

    assert vol_hi == pytest.approx(vol_lo, abs=1e-9)  # min_var's weights never depend on mu
    assert er_hi == pytest.approx(er_lo + 0.05, abs=1e-6)
    assert sharpe_hi == pytest.approx(sharpe_lo, abs=1e-6)


# -- T-110: the price-spine guard --------------------------------------------------------


def _stale_as_of(conn: Database) -> tuple[str, str]:
    """``(stale_as_of, last_price_date)`` -- a date 5 days past price_daily's last bar."""
    last = str(conn.execute("SELECT MAX(date) FROM price_daily").fetchone()[0])
    stale = (date.fromisoformat(last) + timedelta(days=5)).isoformat()
    return stale, last


def test_build_risk_model_refuses_an_as_of_past_the_price_spine(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn)
    stale_as_of, last = _stale_as_of(conn)
    before = conn.execute("SELECT COUNT(*) FROM quant_run").fetchone()[0]

    with pytest.raises(StaleAsOf, match=f"{stale_as_of}.*{last}"):
        run_build_risk_model(settings, as_of=stale_as_of, conn=conn)

    # a quant_run row is written and marked failed, not silently swallowed
    row = conn.execute(
        "SELECT status FROM quant_run WHERE command = 'build-risk-model' ORDER BY id DESC"
    ).fetchone()
    assert row["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM quant_run").fetchone()[0] == before + 1


def test_an_as_of_at_or_before_the_price_spine_is_safe(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn)
    as_of = conn.execute("SELECT MAX(date) FROM price_daily").fetchone()[0]
    res = run_build_risk_model(settings, as_of=as_of, conn=conn)
    assert res.stale_prices_bypassed is None


def test_allow_stale_prices_overrides_the_guard_and_records_it(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn)
    stale_as_of, last = _stale_as_of(conn)

    res = run_build_risk_model(
        settings.model_copy(update={"allow_stale_prices": True}), as_of=stale_as_of, conn=conn
    )

    assert res.stale_prices_bypassed is not None
    assert stale_as_of in res.stale_prices_bypassed
    assert last in res.stale_prices_bypassed
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM quant_run WHERE command = 'build-risk-model' ORDER BY id DESC"
        ).fetchone()[0]
    )
    assert params["stale_as_of_bypassed"] == res.stale_prices_bypassed


def test_optimize_refuses_a_stale_as_of_when_it_must_build_the_model(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn).model_copy(
        update={"objectives": ["min_var"], "max_name_weight": None, "max_sector_weight": None}
    )
    stale_as_of, _ = _stale_as_of(conn)

    with pytest.raises(StaleAsOf):
        run_optimize(settings, as_of=stale_as_of, conn=conn)


def test_optimize_propagates_the_bypass_reason_when_it_auto_builds(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn).model_copy(
        update={
            "objectives": ["min_var"],
            "max_name_weight": None,
            "max_sector_weight": None,
            "allow_stale_prices": True,
        }
    )
    stale_as_of, _ = _stale_as_of(conn)

    opt = run_optimize(settings, as_of=stale_as_of, conn=conn)
    assert opt.stale_prices_bypassed is not None


def test_optimize_re_checks_staleness_even_when_reusing_a_stored_model(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """T-110 follow-up (PR #87 review): a stale risk model built once with
    ``--allow-stale-prices`` must not let a later ``optimize`` at the same stale ``as_of``
    slip through unchecked just because it reuses that stored model instead of building one --
    the guard is ``optimize``'s own, not only triggered by ``_resolve_model_id``'s build path."""
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn).model_copy(
        update={"objectives": ["min_var"], "max_name_weight": None, "max_sector_weight": None}
    )
    stale_as_of, _ = _stale_as_of(conn)
    run_build_risk_model(
        settings.model_copy(update={"allow_stale_prices": True}), as_of=stale_as_of, conn=conn
    )

    with pytest.raises(StaleAsOf):
        run_optimize(settings, as_of=stale_as_of, conn=conn)


def test_build_risk_model_refuses_a_dirty_code_version(
    memory_quant_db: Database, quant_seed: Callable[..., Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn)
    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    monkeypatch.setattr(quant_persist, "code_version", lambda: "deadbee-dirty")
    before = conn.execute("SELECT COUNT(*) FROM quant_run").fetchone()[0]

    with pytest.raises(DirtyTree, match="deadbee-dirty"):
        run_build_risk_model(settings, as_of=as_of, conn=conn)

    row = conn.execute(
        "SELECT status FROM quant_run WHERE command = 'build-risk-model' ORDER BY id DESC"
    ).fetchone()
    assert row["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM quant_run").fetchone()[0] == before + 1


def test_allow_dirty_overrides_the_guard_and_records_it(
    memory_quant_db: Database, quant_seed: Callable[..., Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn)
    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    monkeypatch.setattr(quant_persist, "code_version", lambda: "deadbee-dirty")

    res = run_build_risk_model(
        settings.model_copy(update={"allow_dirty": True}), as_of=as_of, conn=conn
    )

    assert res.dirty_tree_bypassed is not None
    assert "deadbee-dirty" in res.dirty_tree_bypassed
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM quant_run WHERE command = 'build-risk-model' ORDER BY id DESC"
        ).fetchone()[0]
    )
    assert params["dirty_tree_bypassed"] == res.dirty_tree_bypassed


def test_optimize_also_refuses_a_dirty_code_version(
    memory_quant_db: Database, quant_seed: Callable[..., Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=300, with_dividends=False)
    settings = _prep(conn).model_copy(
        update={"objectives": ["min_var"], "max_name_weight": None, "max_sector_weight": None}
    )
    as_of = conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0]
    monkeypatch.setattr(quant_persist, "code_version", lambda: "deadbee-dirty")

    with pytest.raises(DirtyTree):
        run_optimize(settings, as_of=as_of, conn=conn)


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
