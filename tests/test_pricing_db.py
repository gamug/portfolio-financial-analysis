"""Pricing collector persistence: schema safety, universe, resume."""

from __future__ import annotations

import sqlite3

import pytest
from portfolio_common.kg_schema.universe_source import UniverseMember

from pricing_agent import db
from pricing_agent.db import PriceWindowRow
from pricing_agent.stats import WindowStats


def _member(
    symbol: str, *, name: str | None = None, cik: str | None = "0000000001"
) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        security=name or f"{symbol} Inc.",
        cik=cik,
        gics_sector="Technology",
        gics_sub_industry="Sub",
        hq_location=None,
        date_added=None,
        founded=None,
        valid_from="2020-01-01",
        valid_to=None,
    )


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


def _company(symbol: str) -> UniverseMember:
    return _member(symbol)


def _stats() -> WindowStats:
    return WindowStats(
        first_date="2022-01-03",
        last_date="2026-08-27",
        first_close=100.0,
        last_close=180.0,
        period_return=0.8,
        trading_days=1100,
        daily_return_std=0.015,
        annualized_volatility=0.238,
        min_close=90.0,
        max_close=200.0,
        avg_volume=5_000_000.0,
    )


def test_ensure_schema_idempotent_and_owns_price_tables(memory_db: sqlite3.Connection) -> None:
    db.ensure_schema(memory_db)
    tables = {
        r["name"] for r in memory_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"price_window", "price_daily", "pricing_run", "assets"} <= tables


def test_ensure_schema_rejects_incompatible_assets() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.commit()
    with pytest.raises(RuntimeError, match="missing columns"):
        db.ensure_schema(conn)


def test_sync_universe_does_not_wipe_existing_cik(memory_db: sqlite3.Connection) -> None:
    memory_db.execute("INSERT INTO assets (ticker, cik) VALUES ('AAPL', '0000320193')")
    memory_db.commit()
    blank = _member("AAPL", name="Apple", cik="")
    db.sync_universe(memory_db, [blank])
    row = memory_db.execute("SELECT cik, company_name FROM assets WHERE ticker='AAPL'").fetchone()
    assert row["cik"] == "0000320193"  # COALESCE kept the real value
    assert row["company_name"] == "Apple"


def test_price_window_upsert_and_resume(memory_db: sqlite3.Connection) -> None:
    db.sync_universe(memory_db, [_company("AAPL")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    row = PriceWindowRow(
        asset_id=asset_id,
        start_date="2022-01-01",
        end_date="2026-08-28",
        label="full",
        stats=_stats(),
        source="yfinance",
        warning=None,
    )
    db.upsert_price_window(memory_db, row)
    db.upsert_price_window(memory_db, row)  # conflict -> update, not duplicate

    count = memory_db.execute("SELECT COUNT(*) FROM price_window").fetchone()[0]
    assert count == 1
    assert db.completed_windows(memory_db) == {("AAPL", "2022-01-01", "2026-08-28", "full")}


def test_bump_run_counter_rejects_injection(memory_db: sqlite3.Connection) -> None:
    run_id = db.start_run(memory_db, params_json="{}")
    with pytest.raises(ValueError, match="counter column"):
        db.bump_run_counter(memory_db, run_id, "completed_units; DROP TABLE assets")
