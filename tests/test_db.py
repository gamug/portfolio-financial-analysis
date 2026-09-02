"""Schema safety, universe upserts, resume bookkeeping."""

from __future__ import annotations

import sqlite3

import pytest

from fundamental_agent import db
from fundamental_agent.db import FilingKey, FilingMeta, SnapshotRow
from kg_schema.universe_source import UniverseMember


def _company(symbol: str, name: str, sector: str = "Technology") -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        security=name,
        cik="0000000001",
        gics_sector=sector,
        gics_sub_industry="Sub",
        hq_location=None,
        date_added=None,
        founded=None,
        valid_from="2020-01-01",
        valid_to=None,
    )


def test_ensure_schema_is_idempotent(memory_db: sqlite3.Connection) -> None:
    db.ensure_schema(memory_db)  # second call must be a no-op
    tables = {
        r["name"] for r in memory_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"assets", "sectors", "sec_filings", "fundamental_snapshot"} <= tables


def test_ensure_schema_preserves_existing_assets() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, "
        "company_name TEXT, cik TEXT, sector_id INTEGER, sub_industry TEXT)"
    )
    conn.execute("INSERT INTO assets (ticker, company_name) VALUES ('AAPL', 'Apple')")
    conn.commit()

    db.ensure_schema(conn)

    row = conn.execute("SELECT company_name FROM assets WHERE ticker = 'AAPL'").fetchone()
    assert row["company_name"] == "Apple"


def test_ensure_schema_rejects_incompatible_assets() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.commit()
    with pytest.raises(RuntimeError, match="missing columns"):
        db.ensure_schema(conn)


def test_sync_universe_upserts_without_duplicating(memory_db: sqlite3.Connection) -> None:
    db.sync_universe(memory_db, [_company("AAPL", "Apple"), _company("MSFT", "Microsoft")])
    db.sync_universe(memory_db, [_company("AAPL", "Apple Inc.")])  # name change

    rows = db.load_universe(memory_db)
    assert {r["ticker"] for r in rows} == {"AAPL", "MSFT"}
    apple = next(r for r in rows if r["ticker"] == "AAPL")
    assert apple["company_name"] == "Apple Inc."


def test_sync_universe_only_touches_assets_not_membership(memory_db: sqlite3.Connection) -> None:
    """Point-in-time membership now lives in universe.db; sync_universe is the
    identity write path only and never writes financial.db universe_membership."""
    db.sync_universe(memory_db, [_company("AAPL", "Apple"), _company("MSFT", "Microsoft")])
    assert memory_db.execute("SELECT COUNT(*) FROM universe_membership").fetchone()[0] == 0
    assert {r["ticker"] for r in db.load_universe(memory_db)} == {"AAPL", "MSFT"}


def test_load_universe_limit_and_ticker_filter(memory_db: sqlite3.Connection) -> None:
    db.sync_universe(
        memory_db,
        [_company("AAPL", "Apple"), _company("MSFT", "Microsoft"), _company("NVDA", "Nvidia")],
    )
    assert len(db.load_universe(memory_db, limit=2)) == 2
    only = db.load_universe(memory_db, tickers=["nvda"])
    assert [r["ticker"] for r in only] == ["NVDA"]


def test_snapshot_is_append_only_and_drives_resume(memory_db: sqlite3.Connection) -> None:
    db.sync_universe(memory_db, [_company("AAPL", "Apple")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    filing_id = db.upsert_filing(
        memory_db,
        FilingKey(asset_id, "10-K", 2023, "FY2023"),
        FilingMeta(filing_date="2023-11-03", period_end="2023-09-30"),
    )

    def snap(score: float) -> SnapshotRow:
        return SnapshotRow(
            asset_id=asset_id,
            filing_id=filing_id,
            form="10-K",
            fiscal_period="FY2023",
            score=score,
            rating="bullish",
            narrative="n",
            strengths=["s"],
            risks=["r"],
            model="deepseek-chat",
            metrics={"profitability.net_margin": 0.25},
            event_time="2023-09-30",
        )

    db.insert_snapshot(memory_db, snap(80.0))
    db.insert_snapshot(memory_db, snap(10.0))  # conflict -> ignored

    rows = list(
        memory_db.execute("SELECT raw_value FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'")
    )
    assert [r["raw_value"] for r in rows] == [80.0]
    assert db.completed_units(memory_db) == {("AAPL", "10-K", "FY2023")}


def test_bump_run_counter_rejects_unknown_column(memory_db: sqlite3.Connection) -> None:
    run_id = db.start_run(memory_db, params={})
    with pytest.raises(ValueError, match="counter column"):
        db.bump_run_counter(memory_db, run_id, "score; DROP TABLE assets")
