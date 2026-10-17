"""The read contract for the private import's rows (T-153 part 3d, D4/D5).

The licence behind the private file is internal-use-only, so no ``v_*`` view that exposes a daily
price, or a value computed from one, may return a Tiingo-sourced row. The provenance lives per row
(``price_daily.source``, ``price_observation.source``, ``quant_return_daily.vendor``,
``corporate_action.source``), and every filter reads ``COALESCE(<mark>, '') <> 'tiingo'`` so the
NULL rows stored before the import existed stay visible.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from portfolio_common.db import Database

from kg_schema import queries
from kg_schema.db import connect
from pricing_agent.db import ensure_schema


@pytest.fixture
def db(tmp_path: Path) -> Database:
    conn = connect(tmp_path / "financial.db")
    ensure_schema(conn)  # creates `assets` and the shared DDL, as any collector's start-up does
    conn.execute(
        "INSERT INTO assets (id, ticker, cik) VALUES (1, 'WBD', '0001437107'), "
        "(2, 'AAPL', '0000320193')"
    )
    conn.commit()
    return conn


def _observation(conn: Database, asset_id: int, day: str, *, source: str | None) -> None:
    conn.execute(
        "INSERT INTO price_observation (asset_id, obs_date, close, source, event_time, "
        "computed_at, engine_version) VALUES (?, ?, 100.0, ?, ?, 'now', 'priceobs-v2')",
        (asset_id, day, source, day),
    )
    conn.commit()


def _return(conn: Database, asset_id: int, day: str, *, vendor: str | None) -> None:
    conn.execute(
        "INSERT INTO quant_return_daily (asset_id, obs_date, close_split_adj, adj_close, tr_index, "
        "source, engine_version, computed_at, vendor) VALUES (?, ?, 100.0, 100.0, 100.0, "
        "'quant-tr-v1', 'qret-v2', 'now', ?)",
        (asset_id, day, vendor),
    )
    conn.commit()


def _price(conn: Database, asset_id: int, day: str, *, source: str) -> None:
    conn.execute(
        "INSERT INTO price_daily (asset_id, date, close, source) VALUES (?, ?, 100.0, ?)",
        (asset_id, day, source),
    )
    conn.commit()


def _position(conn: Database, asset_id: int, *, cost_basis: float) -> None:
    conn.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, weight, cost_basis) "
        "VALUES (?, '2026-09-30', 0.05, ?)",
        (asset_id, cost_basis),
    )
    conn.commit()


def test_the_price_observation_view_drops_only_the_imports_rows(db: Database) -> None:
    _observation(db, 1, "2026-09-01", source="tiingo")
    _observation(db, 1, "2026-09-02", source="yfinance")
    _observation(db, 2, "2026-09-01", source=None)  # every row stored before the import existed
    rows = db.execute(
        "SELECT ticker, obs_date, source FROM v_price_observation ORDER BY ticker, obs_date"
    ).fetchall()
    assert [(r["ticker"], r["obs_date"]) for r in rows] == [
        ("AAPL", "2026-09-01"),
        ("WBD", "2026-09-02"),
    ]
    assert rows[0]["source"] is None  # a NULL mark stays visible


def test_the_return_view_drops_only_the_imports_rows_and_publishes_the_vendor(db: Database) -> None:
    _return(db, 1, "2026-09-01", vendor="tiingo")
    _return(db, 1, "2026-09-02", vendor=None)
    _return(db, 2, "2026-09-01", vendor=None)
    rows = db.execute(
        "SELECT ticker, obs_date, vendor FROM v_quant_return_daily ORDER BY ticker, obs_date"
    ).fetchall()
    assert [(r["ticker"], r["obs_date"], r["vendor"]) for r in rows] == [
        ("AAPL", "2026-09-01", None),
        ("WBD", "2026-09-02", None),
    ]
    # the marker is the last column, appended (the contract's versioning rule)
    columns = [r[1] for r in db.execute("PRAGMA table_info(v_quant_return_daily)").fetchall()]
    assert columns[-1] == "vendor"


def test_the_positions_view_nulls_a_tiingo_cost_basis_and_keeps_the_weight(db: Database) -> None:
    _price(db, 1, "2026-09-01", source="tiingo")
    _price(db, 2, "2026-09-01", source="yfinance")
    _position(db, 1, cost_basis=100.0)  # a close the import priced
    _position(db, 2, cost_basis=200.0)  # the gateway's
    rows = db.execute(
        "SELECT ticker, weight, cost_basis FROM v_portfolio_position ORDER BY ticker"
    ).fetchall()
    assert [(r["ticker"], r["weight"], r["cost_basis"]) for r in rows] == [
        ("AAPL", 0.05, 200.0),
        ("WBD", 0.05, None),
    ]


def test_the_corporate_action_view_keeps_the_imports_rows_and_their_source(db: Database) -> None:
    """The declared boundary: a dividend amount and a split ratio are neither a price nor a value
    computed from one, so the view keeps them -- with their vendor readable."""
    db.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (1, 'DIVIDEND', '2026-09-01', 0.25, 'tiingo', "
        "'corpact-tiingo-v1', 'now'), (1, 'DIVIDEND', '2026-09-02', 0.50, 'pricing-gateway', "
        "'corpact-v1', 'now')"
    )
    db.commit()
    rows = db.execute(
        "SELECT ex_date, source, engine_version FROM v_corporate_action ORDER BY ex_date"
    ).fetchall()
    assert [(r["source"], r["engine_version"]) for r in rows] == [
        ("tiingo", "corpact-tiingo-v1"),
        ("pricing-gateway", "corpact-v1"),
    ]


def test_the_line_marker_is_the_one_way_to_name_the_imported_lines(db: Database) -> None:
    """D8: `queries.tiingo_asset_ids` -- the list T-100 reports with-and-without around."""
    _price(db, 1, "2026-09-01", source="tiingo")
    _price(db, 2, "2026-09-01", source="yfinance")
    assert queries.tiingo_asset_ids(db) == [1]
    assert queries.tiingo_asset_ids(connect(":memory:")) == []  # no table yet: a fresh database
