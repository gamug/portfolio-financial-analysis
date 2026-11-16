"""The read contract for the private import's rows (T-153 part 3d, D4/D5).

The licence behind the private file is internal-use-only, so no ``v_*`` view that exposes a daily
price, or a value computed from one, may return a Tiingo-sourced row. The provenance lives per row
(``price_daily.source``, ``price_observation.source``, ``quant_return_daily.vendor``,
``corporate_action.source``), and every filter reads ``COALESCE(<mark>, '') <> 'tiingo'`` so the
NULL rows stored before the import existed stay visible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from portfolio_common.db import Database

from fundamental_agent.db import ensure_schema as ensure_fundamental_schema
from kg_schema import queries
from kg_schema.db import connect
from pricing_agent.db import ensure_schema


@pytest.fixture
def db(tmp_path: Path) -> Database:
    conn = connect(tmp_path / "financial.db")
    ensure_schema(conn)  # creates `assets` and the shared DDL, as any collector's start-up does
    ensure_fundamental_schema(conn)  # sec_filings / fundamental_metrics, for the two score tests
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


def test_the_corporate_action_view_does_not_return_the_imports_rows(db: Database) -> None:
    """Reviewer B's fix 2: the section 4 rule names a dividend value, so the licence's
    "never a value" applies here too. ``quant`` reads the table, not this view, so returns
    do not change."""
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
    assert [(r["ex_date"], r["source"]) for r in rows] == [("2026-09-02", "pricing-gateway")]


def test_the_line_marker_is_the_one_way_to_name_the_imported_lines(db: Database) -> None:
    """D8: `queries.tiingo_asset_ids` -- the list T-100 reports with-and-without around."""
    _price(db, 1, "2026-09-01", source="tiingo")
    _price(db, 2, "2026-09-01", source="yfinance")
    assert queries.tiingo_asset_ids(db) == [1]
    assert queries.tiingo_asset_ids(connect(":memory:")) == []  # no table yet: a fresh database


# -- review B's fix 1: the two places that publish a value computed from the close ---------------

VALUATION_METRICS = (
    "market_capitalization",
    "enterprise_value",
    "free_cash_flow_yield",
    "sbc_adjusted_fcf_yield",
    "enterprise_fcf_yield",
)


def _filing(conn: Database, asset_id: int) -> None:
    """A filing of its own id, dated -- the FUNDAMENTAL rows' `available_at` comes from it."""
    conn.execute(
        "INSERT OR IGNORE INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, filing_date, "
        "retrieved_at, available_at) VALUES (?, ?, '10-K', 2026, 'FY', '2026-02-01', 'now', "
        "'2026-02-02')",
        (asset_id, asset_id),
    )
    conn.commit()


def _metric(conn: Database, asset_id: int, group: str, name: str, value: float) -> None:
    _filing(conn, asset_id)
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, unit, value, "
        "engine_version, computed_at, event_time, available_at) VALUES (?, ?, ?, 'usd', ?, "
        "'metrics-v1', 'now', '2026-01-31', '2026-02-02')",
        (asset_id, group, name, value),
    )
    conn.commit()


def test_the_metric_view_nulls_the_five_valuation_values_of_an_imported_line(db: Database) -> None:
    """A market cap is the public cover count times the close: cap / count gives the close back."""
    _price(db, 1, "2026-09-01", source="tiingo")
    _price(db, 2, "2026-09-01", source="yfinance")
    for asset_id in (1, 2):
        for name in VALUATION_METRICS:
            _metric(db, asset_id, "valuation", name, 1_000.0)
        _metric(db, asset_id, "profitability", "net_margin", 0.25)
    rows = db.execute(
        "SELECT ticker, metric_group, metric_name, value FROM v_fundamental_metric "
        "WHERE metric_group IN ('valuation', 'profitability') ORDER BY ticker, metric_name"
    ).fetchall()
    imported = {(r["metric_name"], r["value"]) for r in rows if r["ticker"] == "WBD"}
    gateway = {(r["metric_name"], r["value"]) for r in rows if r["ticker"] == "AAPL"}
    assert len(imported) == len(gateway) == 6  # the rows stay, only the values move
    assert {name for name, value in imported if value is None} == set(VALUATION_METRICS)
    assert dict(imported)["net_margin"] == 0.25  # another group is untouched
    assert {value for name, value in gateway if name in VALUATION_METRICS} == {1_000.0}


def _score(conn: Database, asset_id: int, score_type: str, inputs: dict | None) -> None:
    """One score row; a FUNDAMENTAL row carries its filing's `available_at` (T-107's trigger)."""
    filing_id = asset_id if score_type == "FUNDAMENTAL" else None
    available = "2026-02-02" if score_type == "FUNDAMENTAL" else "2026-09-01"
    conn.execute(
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
        "event_time, computed_at, model, inputs_json, filing_id, available_at) VALUES (?, ?, 42.0, "
        "42.0, '2026-09-01', 'now', 'test', ?, ?, ?)",
        (
            asset_id,
            score_type,
            None if inputs is None else json.dumps(inputs),
            filing_id,
            available,
        ),
    )
    conn.commit()


def test_the_score_block_hides_the_imports_cap_and_valuation_keys(db: Database) -> None:
    """Rule 3 keeps scores: raw_value, normalized_score and the rows stay. What goes is what a
    close can be recovered from -- a VALORIZATION block wholesale (it is built on the cap) and a
    FUNDAMENTAL block's five valuation keys (its pillars and other metrics stay)."""
    _price(db, 1, "2026-09-01", source="tiingo")
    _price(db, 2, "2026-09-01", source="yfinance")
    fundamental = {
        "valuation.market_capitalization": 2.7e12,
        "valuation.free_cash_flow_yield": 0.031,
        "profitability.net_margin": 0.25,
        "pillars": {"profitability": {"rating": 62}},
    }
    for asset_id in (1, 2):
        _filing(db, asset_id)
        _score(db, asset_id, "VALORIZATION", {"quality": 50.0, "size": 50.0, "value": 60.0})
        _score(db, asset_id, "FUNDAMENTAL", fundamental)
    rows = db.execute(
        "SELECT ticker, score_type, raw_value, normalized_score, inputs_json FROM v_score_snapshot "
        "ORDER BY ticker, score_type"
    ).fetchall()
    by_ticker: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], {})[row["score_type"]] = {
            "raw": row["raw_value"],
            "norm": row["normalized_score"],
            "inputs": None if row["inputs_json"] is None else json.loads(row["inputs_json"]),
        }
    assert set(by_ticker["WBD"]) == {"VALORIZATION", "FUNDAMENTAL"}  # the rows stay
    assert by_ticker["WBD"]["VALORIZATION"]["inputs"] is None
    assert by_ticker["WBD"]["VALORIZATION"]["raw"] == 42.0  # the score itself stays
    assert by_ticker["WBD"]["FUNDAMENTAL"]["inputs"] == {
        "profitability.net_margin": 0.25,
        "pillars": {"profitability": {"rating": 62}},
    }
    assert by_ticker["AAPL"]["VALORIZATION"]["inputs"] == {
        "quality": 50.0,
        "size": 50.0,
        "value": 60.0,
    }
    assert by_ticker["AAPL"]["FUNDAMENTAL"]["inputs"] == fundamental
