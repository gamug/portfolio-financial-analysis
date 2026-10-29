"""m018: the backtest tables and the read-contract views over them (BACKTEST_SPEC §5)."""

from __future__ import annotations

import pytest
from conftest import _memory_database
from portfolio_common.db import Database

import kg_schema
from fundamental_agent import db as fundamental_db
from kg_schema import queries
from kg_schema.ddl import BACKTEST_DDL
from kg_schema.migrations import MIGRATIONS, apply_migrations
from kg_schema.views import VIEWS

TABLES = (
    "backtest_run",
    "strategy_book",
    "book_rebalance",
    "book_weight_daily",
    "guard_alert",
    "guard_action",
    "book_daily_performance",
    "backtest_summary",
    "backtest_test",
)
NEW_VIEWS = (
    "v_book",
    "v_book_rebalance",
    "v_guard_alert",
    "v_guard_action",
    "v_book_weight_daily",
    "v_book_daily_performance",
    "v_portfolio_period_summary",
    "v_backtest_test",
)


@pytest.fixture
def conn() -> Database:
    db = _memory_database()
    fundamental_db.ensure_schema(db)
    kg_schema.ensure(db, run_migrations=True)
    return db


def test_m018_is_recorded_and_the_tables_and_views_exist(conn: Database) -> None:
    assert 18 in {version for version, _d, _f in MIGRATIONS}
    assert queries.current_version(conn) == max(v for v, _, _ in MIGRATIONS)
    for table in TABLES:
        assert conn.relation_exists(table), table
    for view in NEW_VIEWS:
        assert conn.relation_kind(view) == "view", view
    assert apply_migrations(conn) == []  # idempotent


def test_m018_changes_nothing_on_a_database_that_already_has_the_tables(conn: Database) -> None:
    before = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
    conn.execute("DELETE FROM schema_version WHERE version >= 18")
    conn.commit()
    assert apply_migrations(conn) == [18]
    after = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
    assert after == before


def test_every_new_view_carries_knowledge_date(conn: Database) -> None:
    for view in NEW_VIEWS:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({view})")]
        assert "knowledge_date" in columns, view


def test_no_view_exposes_one_assets_price_or_return(conn: Database) -> None:
    forbidden = {
        "close",
        "close_split_adj",
        "adj_close",
        "price",
        "open",
        "high",
        "low",
        "volume",
        "log_return",
        "tr_log_return",
        "price_log_return",
        "total_return",
    }
    for view in NEW_VIEWS:
        assert view in VIEWS
        columns = {str(r[1]).lower() for r in conn.execute(f"PRAGMA table_info({view})")}
        assert not (columns & forbidden), (view, sorted(columns & forbidden))


def test_the_views_read_a_persisted_book(conn: Database) -> None:
    conn.execute("INSERT INTO assets (id, ticker, company_name) VALUES (1, 'AAA', 'Alpha')")
    conn.execute(
        "INSERT INTO backtest_run (id, window_id, d_start, d_end, formation_date, "
        "warmup_steps, params_json, ips_json, status, created_at) VALUES "
        "(1, 'P1', '2022-01-03', '2022-10-12', '2022-01-03', 12, '{}', '{}', 'completed', "
        "'2026-10-29T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO strategy_book (id, backtest_run_id, arm_code, objective, "
        "exclude_hard_vetoed, guard_layers) VALUES (1, 1, 'TOPN_MV_GUARD', 'min_var', 0, "
        "'[1, 3, 4]')"
    )
    conn.execute(
        "INSERT INTO book_rebalance (book_id, decision_date, execution_date, status) "
        "VALUES (1, '2022-01-03', '2022-01-04', 'executed')"
    )
    conn.execute(
        "INSERT INTO book_weight_daily (book_id, date, holding_key, asset_id, "
        "markowitz_target_weight, drifted_weight, pre_guard_weight, final_target_weight) "
        "VALUES (1, '2022-01-04', 'AAA', 1, 0.5, 0.5, 0.5, 0.5), "
        "(1, '2022-01-04', 'CASH', NULL, 0.5, 0.5, 0.5, 0.5), "
        "(1, '2022-01-05', 'AAA', 1, 0.4, 0.4, 0.4, 0.4)"
    )
    conn.execute(
        "INSERT INTO book_daily_performance (book_id, date, gross_return, turnover, "
        "cost_return, net_return, nav, drawdown, cash_weight, n_equity, benchmark_return) "
        "VALUES (1, '2022-01-04', 0.001, 1.0, 0.001, 0.0, 0.999, 0.001, 0.5, 1, 0.002)"
    )
    conn.execute(
        "INSERT INTO guard_alert (id, backtest_run_id, date, layer, asset_id, rule_id, "
        "value, threshold, state) VALUES "
        "(1, 1, '2022-01-04', 1, 1, 'L1_STOP_LOSS', -0.11, -0.10, 'ENTER'), "
        "(2, 1, '2022-01-04', 3, NULL, 'L3_VOL', 0.8, 1.0, 'SCALE')"
    )
    conn.execute(
        "INSERT INTO guard_action (book_id, alert_id, decision_date, effective_date, "
        "asset_id, status, weight_before, weight_after) VALUES "
        "(1, 1, '2022-01-04', '2022-01-05', 1, 'APPLIED', 0.5, 0.0), "
        "(1, 2, '2022-01-04', '2022-01-05', NULL, 'APPLIED', 1.0, 0.8)"
    )
    conn.execute(
        "INSERT INTO backtest_summary (book_id, segment, date_from, date_to, "
        "cumulative_return, annualized_return, annualized_vol, ex_post_sharpe, "
        "max_drawdown, total_turnover, total_cost_bps, n_rebalances, n_guard_actions) "
        "VALUES (1, 'P1', '2022-01-03', '2022-10-12', 0.1, 0.2, 0.15, 0.7, 0.05, 2.0, "
        "20.0, 10, 1)"
    )
    conn.execute(
        "INSERT INTO backtest_test (backtest_run_id, segment, arm_a, arm_b, test, "
        "statistic, p_value, n_obs, params_json) VALUES "
        "(1, 'P1', 'TOPN_MV_GUARD', 'TOPN_MV_NOGUARD', 'memmel_jk', 0.4, 0.7, 190, NULL)"
    )
    conn.commit()

    book = conn.execute("SELECT * FROM v_book").fetchone()
    assert (book["arm_code"], book["knowledge_date"]) == ("TOPN_MV_GUARD", "2026-10-29")
    weight = conn.execute(
        "SELECT * FROM v_book_weight_daily WHERE holding_key = 'AAA' AND date = '2022-01-04'"
    ).fetchone()
    assert weight["final_target_weight"] == 0.5
    assert weight["markowitz_target_weight"] == 0.5
    assert weight["knowledge_date"] == "2022-01-04"
    # A daily weight the plan never exposed: no target on a non-decision/non-execution date.
    ordinary = conn.execute(
        "SELECT * FROM v_book_weight_daily WHERE holding_key = 'AAA' AND date = '2022-01-05'"
    ).fetchone()
    assert ordinary["markowitz_target_weight"] is None
    assert ordinary["final_target_weight"] is None

    layer1 = conn.execute("SELECT * FROM v_guard_alert WHERE layer = 1").fetchone()
    assert (layer1["ticker"], layer1["value"], layer1["threshold"], layer1["state"]) == (
        "AAA",
        None,
        -0.10,
        "ENTER",
    )
    layer3 = conn.execute("SELECT * FROM v_guard_alert WHERE layer = 3").fetchone()
    assert (layer3["asset_id"], layer3["value"]) == (None, 0.8)

    asset_action = conn.execute(
        "SELECT * FROM v_guard_action WHERE asset_id IS NOT NULL"
    ).fetchone()
    assert asset_action["status"] == "APPLIED"
    assert asset_action["weight_before"] is None and asset_action["weight_after"] is None
    scale_action = conn.execute("SELECT * FROM v_guard_action WHERE asset_id IS NULL").fetchone()
    assert (scale_action["weight_before"], scale_action["weight_after"]) == (1.0, 0.8)
    assert conn.execute("SELECT * FROM v_portfolio_period_summary").fetchone()["segment"] == "P1"
    assert conn.execute("SELECT * FROM v_backtest_test").fetchone()["test"] == "memmel_jk"
    assert conn.execute("SELECT * FROM v_book_daily_performance").fetchone()["nav"] == 0.999
    assert conn.execute("SELECT * FROM v_book_rebalance").fetchone()["status"] == "executed"


def test_backtest_ddl_is_additive_and_idempotent() -> None:
    db = _memory_database()
    db.create_schema(BACKTEST_DDL)
    db.create_schema(BACKTEST_DDL)  # a second time is a no-op
    for table in TABLES:
        assert db.relation_exists(table)
