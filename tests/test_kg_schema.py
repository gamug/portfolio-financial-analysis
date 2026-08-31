"""Shared-schema DDL, migrations, views, and membership reconciliation."""

from __future__ import annotations

import sqlite3

import pytest

import kg_schema
from kg_schema import migrations, universe_membership, version

# Pre-kg_schema table shapes, as the two agents shipped them originally.
_LEGACY_SQL = """
CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE assets (
    id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, company_name TEXT, cik TEXT,
    sector_id INTEGER REFERENCES sectors(id), sub_industry TEXT
);
CREATE TABLE sec_filings (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
    form TEXT NOT NULL, fiscal_year INTEGER NOT NULL, fiscal_period TEXT NOT NULL,
    filing_date TEXT, accession_number TEXT, period_end TEXT, retrieved_at TEXT NOT NULL,
    UNIQUE (asset_id, form, fiscal_period)
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    statement TEXT NOT NULL, concept TEXT NOT NULL, standard_concept TEXT, label TEXT,
    period_key TEXT NOT NULL, value REAL,
    UNIQUE (filing_id, statement, concept, period_key)
);
CREATE TABLE fundamental_metrics (
    id INTEGER PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    metric_group TEXT NOT NULL, metric_name TEXT NOT NULL, value REAL, unit TEXT,
    inputs_json TEXT, computed_at TEXT NOT NULL,
    UNIQUE (filing_id, metric_group, metric_name)
);
CREATE TABLE fundamental_snapshot (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
    filing_id INTEGER NOT NULL REFERENCES sec_filings(id), form TEXT NOT NULL,
    fiscal_period TEXT NOT NULL, score REAL NOT NULL, rating TEXT NOT NULL,
    narrative TEXT NOT NULL, strengths_json TEXT, risks_json TEXT, model TEXT NOT NULL,
    metrics_json TEXT, created_at TEXT NOT NULL,
    UNIQUE (asset_id, form, fiscal_period)
);
"""


def _legacy_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_LEGACY_SQL)
    conn.execute("INSERT INTO assets (ticker, company_name) VALUES ('AAPL', 'Apple')")
    conn.execute("INSERT INTO assets (ticker, company_name) VALUES ('MSFT', 'Microsoft')")
    conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
        "accession_number, retrieved_at) VALUES (1, '10-K', 2023, 'FY2023', '2023-09-30', "
        "'0000320193-23-000106', '2024-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO financial_facts (filing_id, statement, concept, period_key, value) "
        "VALUES (1, 'income_statement', 'Revenues', '2023-09-30 (FY)', 383285000000.0)"
    )
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "computed_at) VALUES (1, 'profitability', 'net_margin', 0.25, 'ratio', "
        "'2024-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO fundamental_snapshot (asset_id, filing_id, form, fiscal_period, score, "
        "rating, narrative, model, created_at) VALUES (1, 1, '10-K', 'FY2023', 72.0, "
        "'bullish', 'n', 'deepseek-chat', '2024-01-02T00:00:00Z')"
    )
    conn.commit()
    return conn


@pytest.fixture
def migrated_db() -> sqlite3.Connection:
    conn = _legacy_conn()
    kg_schema.ensure(conn, run_migrations=True)
    return conn


def test_ensure_is_idempotent_and_additive() -> None:
    conn = _legacy_conn()
    kg_schema.ensure(conn)
    kg_schema.ensure(conn)  # second call must not raise
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"score_snapshot", "universe_membership", "veto", "price_observation"} <= tables
    # additive columns present
    ff_cols = {r[1] for r in conn.execute("PRAGMA table_info(financial_facts)")}
    assert {"event_time", "filing_version"} <= ff_cols
    # additive-only path never advances the version
    assert version.current_version(conn) == 0


def test_migrations_rebuild_and_preserve_rows(migrated_db: sqlite3.Connection) -> None:
    conn = migrated_db
    assert version.current_version(conn) == 4
    # financial_facts rebuilt with filing_version in the unique key, backfilled
    ff = conn.execute("SELECT filing_version, event_time FROM financial_facts").fetchone()
    assert ff["filing_version"] == "0000320193-23-000106"
    assert ff["event_time"] == "2023-09-30"
    # fundamental_metrics likewise
    fm = conn.execute("SELECT engine_version, event_time FROM fundamental_metrics").fetchone()
    assert fm["engine_version"] == "pre-v1"
    assert fm["event_time"] == "2023-09-30"
    # snapshot migrated into score_snapshot as FUNDAMENTAL, old name now a view
    row = conn.execute("SELECT score_type, raw_value, event_time FROM score_snapshot").fetchone()
    assert (row["score_type"], row["raw_value"], row["event_time"]) == (
        "FUNDAMENTAL",
        72.0,
        "2023-09-30",
    )
    kind = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'fundamental_snapshot'"
    ).fetchone()["type"]
    assert kind == "view"
    # the compatibility view still answers the README-style query
    compat = conn.execute(
        "SELECT a.ticker, s.form, s.fiscal_period, s.score, s.rating "
        "FROM fundamental_snapshot s JOIN assets a ON a.id = s.asset_id"
    ).fetchone()
    assert tuple(compat) == ("AAPL", "10-K", "FY2023", 72.0, "bullish")


def test_migrations_are_a_noop_second_time(migrated_db: sqlite3.Connection) -> None:
    assert migrations.apply_migrations(migrated_db) == []


def test_views_select_cleanly(migrated_db: sqlite3.Connection) -> None:
    for name in ("v_score_snapshot", "v_universe_membership", "v_price_observation", "v_veto"):
        migrated_db.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()  # noqa: S608 - fixed view names


def test_universe_membership_lifecycle() -> None:
    conn = _legacy_conn()
    kg_schema.ensure(conn)
    # AAPL(1), MSFT(2) present
    opened, closed = universe_membership.reconcile(
        conn, "SP500", {1, 2}, as_of="2026-01-01", source="wikipedia"
    )
    assert (opened, closed) == (2, 0)
    # MSFT leaves, NVDA(3) joins
    conn.execute("INSERT INTO assets (ticker) VALUES ('NVDA')")
    conn.commit()
    opened, closed = universe_membership.reconcile(
        conn, "SP500", {1, 3}, as_of="2026-04-01", source="wikipedia"
    )
    assert (opened, closed) == (1, 1)
    rows = {
        (r["asset_id"], r["valid_from"], r["valid_to"])
        for r in conn.execute("SELECT asset_id, valid_from, valid_to FROM universe_membership")
    }
    assert rows == {
        (1, "2026-01-01", None),
        (2, "2026-01-01", "2026-04-01"),
        (3, "2026-04-01", None),
    }
    # MSFT rejoins -> a fresh open stint, old one stays closed
    universe_membership.reconcile(conn, "SP500", {1, 2, 3}, as_of="2026-07-01", source="wikipedia")
    msft = conn.execute(
        "SELECT valid_from, valid_to FROM universe_membership WHERE asset_id = 2 "
        "ORDER BY valid_from"
    ).fetchall()
    assert [tuple(r) for r in msft] == [("2026-01-01", "2026-04-01"), ("2026-07-01", None)]
