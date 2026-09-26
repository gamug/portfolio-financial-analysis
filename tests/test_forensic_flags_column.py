"""``score_snapshot.forensic_flags_json`` (T-041): a nullable column for Work item 8's
structured forensic flags (T-074 writes it), added through ``REQUIRED_COLUMNS`` and carried
through the two migrations that rebuild ``score_snapshot`` (m005, m006)."""

from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import seed_filing, seed_fundamental_score
from portfolio_common.db import Database

import kg_schema
from kg_schema import apply_migrations, queries

FLAGS = json.dumps(
    {
        "data_error_suspected": True,
        "negative_equity_buyback": False,
        "value_destroyer_sub_wacc": False,
        "severe_sbc_dilution": True,
    }
)


def _raw() -> Database:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _floor_4() -> Database:
    """A database at schema floor 4: score_snapshot before SECTOR / VALORIZATION existed and
    before T-041, holding one FUNDAMENTAL row."""
    conn = _raw()
    conn.executescript(
        """
        CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE,
                             sector_id INTEGER, sub_industry TEXT);
        CREATE TABLE sec_filings (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL,
                                  form TEXT NOT NULL, fiscal_year INTEGER NOT NULL,
                                  fiscal_period TEXT NOT NULL, filing_date TEXT,
                                  accession_number TEXT, period_end TEXT,
                                  retrieved_at TEXT NOT NULL);
        INSERT INTO assets (id, ticker) VALUES (1, 'AAPL');
        INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, filing_date,
                                 period_end, retrieved_at)
        VALUES (1, 1, '10-Q', 2023, '2023Q3', '2023-11-03', '2023-09-30', 'now');
        CREATE TABLE score_snapshot (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            score_type TEXT NOT NULL CHECK (score_type IN
                ('FUNDAMENTAL', 'QUANTITATIVE', 'TECHNICAL', 'SEMANTIC')),
            raw_value REAL, normalized_score REAL, event_time TEXT NOT NULL,
            computed_at TEXT NOT NULL, model TEXT, inputs_json TEXT, run_id INTEGER,
            run_kind TEXT, filing_id INTEGER REFERENCES sec_filings(id),
            rating TEXT, narrative TEXT, strengths_json TEXT, risks_json TEXT,
            UNIQUE (asset_id, score_type, event_time)
        );
        INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at,
                                    filing_id)
        VALUES (1, 'FUNDAMENTAL', 72.0, '2023-09-30', '2024-01-02T00:00:00Z', 1);
        """
    )
    queries.ensure(conn)
    queries.record(conn, 4, "pretend floor")
    conn.commit()
    return conn


def test_ensure_adds_the_column_nullable_and_is_idempotent(memory_db: Database) -> None:
    conn = memory_db  # an agent schema + kg_schema.ensure, as every package runs it
    kg_schema.ensure(conn)  # a second run adds nothing and does not fail
    assert "forensic_flags_json" in conn.table_columns("score_snapshot")
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAPL')")
    seed_fundamental_score(conn, 1, 70.0, event_time="2025-12-31")  # nullable: not written
    assert conn.execute("SELECT forensic_flags_json FROM score_snapshot").fetchone()[0] is None


def test_the_snapshot_key_is_unchanged(memory_db: Database) -> None:
    conn = memory_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAPL')")
    fid = seed_filing(conn, 1, period_end="2025-12-31", filing_date="2026-02-20")
    insert = (
        "INSERT INTO score_snapshot (asset_id, score_type, raw_value, event_time, computed_at, "
        "forensic_flags_json, filing_id, available_at) "
        "VALUES (1, 'FUNDAMENTAL', 70.0, '2025-12-31', 'now', ?, ?, '2026-02-23')"
    )
    conn.execute(insert, (FLAGS, fid))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(insert, (None, fid))


def test_flags_written_before_migrating_survive_the_score_snapshot_rebuilds() -> None:
    """ensure() adds the column; flags written then must survive m005 and m006, which
    rebuild the table from an explicit column list."""
    conn = _floor_4()
    kg_schema.ensure(conn)  # additive only: the column arrives, no migration yet
    conn.execute("UPDATE score_snapshot SET forensic_flags_json = ?", (FLAGS,))
    conn.commit()
    applied = kg_schema.ensure(conn, run_migrations=True)
    assert {5, 6} <= set(applied)
    row = conn.execute("SELECT score_type, forensic_flags_json FROM score_snapshot").fetchone()
    assert (row["score_type"], json.loads(row["forensic_flags_json"])) == (
        "FUNDAMENTAL",
        json.loads(FLAGS),
    )


def test_migrating_a_table_without_the_column_adds_it_first() -> None:
    """A direct apply_migrations call (no ensure) on a pre-T-041 table must not fail on the
    rebuild's column list."""
    conn = _floor_4()
    assert {5, 6} <= set(apply_migrations(conn))
    assert "forensic_flags_json" in conn.table_columns("score_snapshot")
    assert conn.execute("SELECT raw_value FROM score_snapshot").fetchone()[0] == 72.0
