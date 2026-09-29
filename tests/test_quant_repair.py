"""Voiding a stale ``live_book`` snapshot and its dependents (T-121)."""

from __future__ import annotations

import pytest
from portfolio_common.db import Database

from quant.db import PortfolioRow, insert_frontier_points, insert_portfolio, sync_positions
from quant.repair import (
    NotALiveBookSnapshot,
    PortfolioNotFound,
    apply_void,
    plan_void,
)


def _live_book(conn: Database, weights: dict[int, float], as_of: str = "2026-06-30") -> int:
    pid = insert_portfolio(
        conn,
        PortfolioRow(
            as_of=as_of,
            kind="live_book",
            objective="live",
            solver="n/a",
            status="snapshot",
            expected_return=None,
            expected_vol=None,
            sharpe=None,
            rf_annual=None,
            n_positions=len(weights),
            engine_version="opt-v1",
        ),
    )
    sync_positions(conn, pid, as_of, weights)
    return pid


@pytest.fixture
def stale_snapshot(memory_quant_db: Database) -> Database:
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker, company_name) VALUES (1, 'BF.B', 'Brown-Forman')")
    pid = _live_book(conn, {1: 0.10})
    for i, d in enumerate(("2026-07-01", "2026-07-02"), start=1):
        conn.execute(
            "INSERT INTO quant_benchmark_performance (portfolio_id, date, realized_return, "
            "cumulative_return, engine_version, computed_at) VALUES (?, ?, ?, ?, 'perf-v1', 'now')",
            (pid, d, 0.001 * i, 0.001 * i),
        )
    conn.commit()
    assert pid == 1  # the only portfolio inserted in this fixture
    return conn


def test_plan_void_reports_the_snapshot_s_positions_and_performance_rows(
    stale_snapshot: Database,
) -> None:
    plan = plan_void(stale_snapshot, 1)
    assert plan.as_of == "2026-06-30"
    assert plan.status == "snapshot"
    assert plan.positions == [("BF.B", 0.10, "2026-06-30", None)]
    assert plan.performance_rows == 2
    assert plan.frontier_points == 0


def test_plan_void_refuses_an_unknown_portfolio_id(memory_quant_db: Database) -> None:
    with pytest.raises(PortfolioNotFound):
        plan_void(memory_quant_db, 999)


def test_plan_void_refuses_a_real_optimized_book(memory_quant_db: Database) -> None:
    """Never lets the tool delete a min_var/tangency/frontier book -- only a live_book
    snapshot, which nothing downstream treats as ground truth."""
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker, company_name) VALUES (1, 'AA', 'A')")
    pid = insert_portfolio(
        conn,
        PortfolioRow(
            as_of="2026-06-30",
            kind="min_var",
            objective="min_var",
            solver="CLARABEL",
            status="optimal",
            expected_return=0.05,
            expected_vol=0.1,
            sharpe=0.5,
            rf_annual=0.04,
            n_positions=1,
            engine_version="opt-v1+abcd1234",
        ),
    )
    with pytest.raises(NotALiveBookSnapshot):
        plan_void(conn, pid)


def test_apply_void_deletes_the_portfolio_and_cascades_to_positions_and_performance(
    stale_snapshot: Database,
) -> None:
    conn = stale_snapshot
    apply_void(conn, plan_void(conn, 1))
    assert conn.execute("SELECT COUNT(*) FROM quant_portfolio WHERE id = 1").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM quant_position WHERE portfolio_id = 1").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM quant_benchmark_performance WHERE portfolio_id = 1"
        ).fetchone()[0]
        == 0
    )


def test_apply_void_deletes_frontier_points_too(memory_quant_db: Database) -> None:
    """Not exercised by a real live_book (frontier points are never attached to one in
    practice), but the delete must not leave an orphan if one somehow existed."""
    conn = memory_quant_db
    conn.execute("INSERT INTO assets (id, ticker, company_name) VALUES (1, 'AA', 'A')")
    pid = _live_book(conn, {1: 1.0})
    model_id = conn.execute(
        "INSERT INTO quant_risk_model (as_of, model_version, lookback_days, min_history_days, "
        "n_assets, cov_estimator, ret_estimator, panel_engine_version, panel_spec_json, "
        "computed_at) VALUES ('2026-06-30', 'rm-v1+abcd1234', 756, 504, 1, 'ledoit_wolf_cc', "
        "'equilibrium', 'qret-v2', '{}', '2026-06-30T00:00:00Z') RETURNING id"
    ).fetchone()["id"]
    insert_frontier_points(conn, model_id, [(0, 0.05, 0.05, 0.1, 0.5, "optimal", "{}")])
    conn.execute(
        "UPDATE quant_frontier_point SET portfolio_id = ? WHERE model_id = ?", (pid, model_id)
    )
    conn.commit()
    plan = plan_void(conn, pid)
    assert plan.frontier_points == 1
    apply_void(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM quant_frontier_point").fetchone()[0] == 0


def test_a_second_void_of_the_same_id_finds_nothing_left(stale_snapshot: Database) -> None:
    conn = stale_snapshot
    apply_void(conn, plan_void(conn, 1))
    with pytest.raises(PortfolioNotFound):
        plan_void(conn, 1)
