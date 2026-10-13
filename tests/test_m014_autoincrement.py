"""Tests for migration m014: non-reusable IDs with AUTOINCREMENT (T-145)."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from portfolio_common.db import Database

import kg_schema
from fundamental_agent import db as fundamental_db
from kg_schema import _availability, _identity
from kg_schema.ddl import (
    ALLOWED_CYCLE_TYPES,
    M014_TABLE_REBUILD_STATEMENTS,
    MAX_ID_QUERIES,
    MAX_REFERENCE_QUERIES,
    RUN_ID_EXCLUSIONS,
    RUN_REFERENCE_MAP,
    SQLITE_SEQUENCE_QUERIES,
    TARGET_RUN_TABLES,
    UNRESOLVED_REFERENCE_QUERIES,
    UNRESOLVED_REFERENCE_SAMPLE_QUERIES,
    WRONG_CYCLE_TYPE_QUERIES,
    WRONG_CYCLE_TYPE_SAMPLE_QUERIES,
)
from kg_schema.migrations import _m014_autoincrement_run_ids, apply_migrations
from kg_schema.queries import (
    current_version,
    invalid_sec_filings_accessions,
    unresolved_run_references,
)
from kg_schema.views import ensure_views
from pricing_agent import db as pricing_db
from quant import db as quant_db


def _full_schema_db() -> Database:
    db = kg_schema.connect(":memory:", create_parents=False)
    fundamental_db.ensure_schema(db)
    pricing_db.ensure_schema(db)
    quant_db.ensure_schema(db)
    kg_schema.ensure(db, run_migrations=True)
    return db


def _make_v13_db() -> Database:
    db = kg_schema.connect(":memory:", create_parents=False)
    db.create_schema(
        """
        CREATE TABLE assets (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL UNIQUE,
            company_name TEXT,
            cik TEXT
        );
        CREATE TABLE cycle_run (
            id INTEGER PRIMARY KEY,
            cycle_type TEXT NOT NULL,
            cycle_date TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            params_json TEXT,
            code_version TEXT,
            UNIQUE (cycle_type, cycle_date)
        );
        CREATE TABLE cycle_ranking (
            id INTEGER PRIMARY KEY,
            cycle_run_id INTEGER NOT NULL REFERENCES cycle_run(id) ON DELETE CASCADE,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            rank INTEGER NOT NULL,
            blended_score REAL NOT NULL,
            UNIQUE (cycle_run_id, asset_id)
        );
        CREATE TABLE cycle_checkpoint (
            id INTEGER PRIMARY KEY,
            cycle_run_id INTEGER NOT NULL REFERENCES cycle_run(id) ON DELETE CASCADE,
            step TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT
        );
        CREATE TABLE analysis_run (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            universe_size INTEGER,
            planned_units INTEGER,
            completed_units INTEGER DEFAULT 0,
            skipped_units INTEGER DEFAULT 0,
            failed_units INTEGER DEFAULT 0,
            params_json TEXT,
            status TEXT NOT NULL,
            as_of TEXT,
            code_version TEXT,
            fallback_units INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE pricing_run (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            universe_size INTEGER,
            planned_units INTEGER,
            completed_units INTEGER DEFAULT 0,
            skipped_units INTEGER DEFAULT 0,
            failed_units INTEGER DEFAULT 0,
            params_json TEXT,
            status TEXT NOT NULL,
            as_of TEXT,
            code_version TEXT
        );
        CREATE TABLE quant_run (
            id INTEGER PRIMARY KEY,
            command TEXT NOT NULL,
            as_of TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            params_json TEXT,
            error TEXT,
            code_version TEXT
        );
        CREATE TABLE quant_risk_model (
            id INTEGER PRIMARY KEY,
            as_of TEXT NOT NULL,
            model_version TEXT NOT NULL,
            rf_annual REAL,
            computed_at TEXT NOT NULL,
            quant_run_id INTEGER,
            manifest_json TEXT
        );
        CREATE TABLE quant_portfolio (
            id INTEGER PRIMARY KEY,
            quant_run_id INTEGER,
            as_of TEXT NOT NULL,
            kind TEXT NOT NULL,
            objective TEXT NOT NULL,
            solver TEXT NOT NULL,
            status TEXT NOT NULL,
            n_positions INTEGER NOT NULL,
            engine_version TEXT NOT NULL,
            computed_at TEXT NOT NULL
        );
        CREATE TABLE price_daily (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            date TEXT NOT NULL,
            close REAL,
            run_id INTEGER,
            UNIQUE (asset_id, date)
        );
        CREATE TABLE portfolio_position (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            valid_from TEXT NOT NULL,
            opened_by_cycle INTEGER,
            run_id INTEGER
        );
        CREATE TABLE sec_filings (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            form TEXT NOT NULL,
            fiscal_year INTEGER NOT NULL,
            fiscal_period TEXT NOT NULL,
            filing_date TEXT,
            accession_number TEXT,
            period_end TEXT,
            retrieved_at TEXT NOT NULL,
            run_id INTEGER,
            available_at TEXT,
            cik TEXT,
            UNIQUE (asset_id, form, fiscal_period)
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
            statement TEXT NOT NULL,
            concept TEXT NOT NULL,
            standard_concept TEXT,
            label TEXT,
            period_key TEXT NOT NULL,
            value REAL,
            run_id INTEGER,
            UNIQUE (filing_id, statement, concept, period_key)
        );
        CREATE TABLE sec_filing_section (
            id INTEGER PRIMARY KEY,
            filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
            section_name TEXT NOT NULL,
            content_raw TEXT,
            content_clean TEXT,
            word_count INTEGER,
            run_id INTEGER,
            UNIQUE (filing_id, section_name)
        );
        CREATE TABLE filing_cover_shares (
            id INTEGER PRIMARY KEY,
            filing_id INTEGER NOT NULL REFERENCES sec_filings(id),
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            class_name TEXT NOT NULL,
            shares_outstanding REAL NOT NULL,
            as_of_date TEXT,
            run_id INTEGER,
            UNIQUE (filing_id, class_name)
        );
        CREATE TABLE fundamental_metrics (
            id INTEGER PRIMARY KEY,
            filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
            metric_group TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_engine_version TEXT NOT NULL DEFAULT 'v1',
            value REAL,
            unit TEXT,
            inputs_json TEXT,
            computed_at TEXT NOT NULL,
            run_id INTEGER,
            available_at TEXT,
            UNIQUE (filing_id, metric_group, metric_name, metric_engine_version)
        );
        CREATE TABLE score_snapshot (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            score_type TEXT NOT NULL,
            event_time TEXT NOT NULL,
            raw_value REAL NOT NULL,
            scaled_value REAL NOT NULL,
            run_id INTEGER,
            run_kind TEXT,
            filing_id INTEGER REFERENCES sec_filings(id),
            model TEXT,
            prompt_hash TEXT,
            inputs_json TEXT,
            computed_at TEXT NOT NULL,
            available_at TEXT,
            rating TEXT,
            narrative TEXT,
            strengths_json TEXT,
            risks_json TEXT,
            UNIQUE (asset_id, score_type, event_time)
        );
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        );
        INSERT INTO schema_version (version, applied_at, description) VALUES (13, '2026-10-11', 'v13');
        """
    )
    ensure_views(db)
    _availability.ensure_triggers(db)
    _identity.ensure_triggers(db)
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_sf_accession_insert BEFORE INSERT ON sec_filings
WHEN NEW.accession_number IS NOT NULL AND EXISTS (
    SELECT 1 FROM sec_filings o
    WHERE o.asset_id = NEW.asset_id AND o.accession_number = NEW.accession_number
      AND NOT (o.form = NEW.form AND o.fiscal_period = NEW.fiscal_period)
)
BEGIN SELECT RAISE(ABORT, 'sec_filings: accession_number already used by another filing of this asset'); END;"""
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_sf_accession_update
BEFORE UPDATE OF asset_id, accession_number ON sec_filings
WHEN NEW.accession_number IS NOT NULL AND EXISTS (
    SELECT 1 FROM sec_filings o
    WHERE o.asset_id = NEW.asset_id AND o.accession_number = NEW.accession_number
      AND o.id <> NEW.id
)
BEGIN SELECT RAISE(ABORT, 'sec_filings: accession_number already used by another filing of this asset'); END;"""
    )
    return db


def test_fresh_database_has_autoincrement_on_all_five_tables() -> None:
    db = _full_schema_db()
    for table in TARGET_RUN_TABLES:
        ddl = db.relation_ddl(table)
        assert ddl is not None, f"table {table} missing from schema"
        assert "AUTOINCREMENT" in ddl.upper(), f"table {table} missing AUTOINCREMENT in DDL: {ddl}"


def test_run_reference_map_completeness_and_validity() -> None:
    db = _full_schema_db()
    tables = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]

    candidate_columns: list[tuple[str, str]] = []
    for t in tables:
        for c in db.table_columns(t):
            if (
                c.endswith("run_id")
                or c == "opened_by_cycle"
                or c.endswith("filing_id")
                or "run_id" in c
                or "filing_id" in c
            ):
                candidate_columns.append((t, c))

    map_table_cols = {(k[0], k[1]) for k in RUN_REFERENCE_MAP}
    for t, c in candidate_columns:
        in_map = (t, c) in map_table_cols
        in_exclusions = (t, c) in RUN_ID_EXCLUSIONS
        assert in_map or in_exclusions, (
            f"candidate column {t}.{c} not in RUN_REFERENCE_MAP or RUN_ID_EXCLUSIONS"
        )

    # Every target in map is one of the five target tables
    for key, target in RUN_REFERENCE_MAP.items():
        assert target in TARGET_RUN_TABLES, f"{key} points to unknown target {target}"
        assert key in MAX_REFERENCE_QUERIES, f"{key} missing from MAX_REFERENCE_QUERIES"
        assert key in UNRESOLVED_REFERENCE_QUERIES, (
            f"{key} missing from UNRESOLVED_REFERENCE_QUERIES"
        )

    for target in TARGET_RUN_TABLES:
        assert target in MAX_ID_QUERIES, f"{target} missing from MAX_ID_QUERIES"


def test_query_dicts_keys_equal_map_keys() -> None:
    """The query dicts' keys EQUAL the map's keys (both directions), and no column

    is in both the map and the exclusions.
    """
    assert set(MAX_REFERENCE_QUERIES.keys()) == set(RUN_REFERENCE_MAP.keys())
    assert set(UNRESOLVED_REFERENCE_QUERIES.keys()) == set(RUN_REFERENCE_MAP.keys())
    assert set(UNRESOLVED_REFERENCE_SAMPLE_QUERIES.keys()) == set(RUN_REFERENCE_MAP.keys())

    cycle_run_map_keys = {k for k, target in RUN_REFERENCE_MAP.items() if target == "cycle_run"}
    assert set(ALLOWED_CYCLE_TYPES.keys()) == cycle_run_map_keys
    assert set(WRONG_CYCLE_TYPE_QUERIES.keys()) == cycle_run_map_keys
    assert set(WRONG_CYCLE_TYPE_SAMPLE_QUERIES.keys()) == cycle_run_map_keys

    map_cols = {(t, c) for (t, c, _) in RUN_REFERENCE_MAP}
    excl_cols = set(RUN_ID_EXCLUSIONS.keys())
    assert map_cols.isdisjoint(excl_cols), (
        f"overlap between map and exclusions: {map_cols & excl_cols}"
    )


def test_m014_sec_filings_triggers_match_fundamental_agent_schema() -> None:
    """The accession triggers restored by m014 must be identical to fundamental_agent.db.SCHEMA."""
    stmts = M014_TABLE_REBUILD_STATEMENTS["sec_filings"]
    assert len(stmts) == 6
    assert stmts[4] in fundamental_db.SCHEMA
    assert stmts[5] in fundamental_db.SCHEMA


def test_migration_m014_from_v13() -> None:  # noqa: PLR0915
    """Old-shape test with child rows in sec_filings' FK children (financial_facts and

    sec_filing_section with ON DELETE CASCADE, plus filing_cover_shares, fundamental_metrics,
    score_snapshot), the T-107/T-149 triggers, and v_* views.
    After migrate: child row counts and contents unchanged (proving foreign_keys really was OFF),
    every row of the five tables equal, triggers and views present, integrity_check ok,
    foreign_key_check equal to before.
    """
    db = _make_v13_db()

    # Insert test data
    db.execute(
        "INSERT INTO assets (id, ticker, company_name, cik) VALUES (1, 'AAPL', 'Apple Inc', '0000320193')"
    )

    # cycle_run: insert 1, 2, 3, then delete 3 (max id = 2)
    db.execute(
        "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status) VALUES (1, 'SELECTION', '2026-01-01', 'now', 'completed')"
    )
    db.execute(
        "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status) VALUES (2, 'MONITORING', '2026-01-02', 'now', 'completed')"
    )
    db.execute(
        "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status) VALUES (3, 'REPLAY', '2026-01-03', 'now', 'completed')"
    )
    db.execute("DELETE FROM cycle_run WHERE id = 3")
    db.execute(
        "INSERT INTO cycle_ranking (id, cycle_run_id, asset_id, rank, blended_score) VALUES (1, 2, 1, 1, 0.9)"
    )
    db.execute(
        "INSERT INTO cycle_checkpoint (id, cycle_run_id, step, status, started_at) VALUES (1, 2, 'ranking', 'completed', 'now')"
    )
    # portfolio_position references opened_by_cycle = 15 (higher than max id 2)
    db.execute(
        "INSERT INTO portfolio_position (id, asset_id, valid_from, opened_by_cycle) VALUES (1, 1, '2026-01-01', 15)"
    )

    # analysis_run: max id = 10
    db.execute("INSERT INTO analysis_run (id, started_at, status) VALUES (10, 'now', 'completed')")

    # sec_filings: max id = 50; child rows in financial_facts, sec_filing_section, etc.
    db.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, filing_date, available_at, retrieved_at, accession_number, cik) "
        "VALUES (50, 1, '10-K', 2025, 'FY', '2025-01-02', '2025-01-03', 'now', '0000320193-25-000100', '0000320193')"
    )
    db.execute(
        "INSERT INTO financial_facts (id, filing_id, statement, concept, period_key, value, run_id) VALUES (1, 50, 'IS', 'Rev', '2025', 100.0, 25)"
    )
    db.execute(
        "INSERT INTO sec_filing_section (id, filing_id, section_name, content_raw, word_count, run_id) VALUES (1, 50, 'Item1', 'content', 100, 25)"
    )
    db.execute(
        "INSERT INTO filing_cover_shares (id, filing_id, asset_id, class_name, shares_outstanding, run_id) VALUES (1, 50, 1, 'Common', 1000.0, 25)"
    )
    db.execute(
        "INSERT INTO fundamental_metrics (id, filing_id, metric_group, metric_name, value, computed_at, available_at, run_id) "
        "VALUES (1, 50, 'mg', 'mn', 1.0, 'now', '2025-01-03', 25)"
    )
    db.execute(
        "INSERT INTO score_snapshot (id, asset_id, score_type, event_time, raw_value, scaled_value, run_id, run_kind, filing_id, computed_at, available_at) "
        "VALUES (1, 1, 'FUNDAMENTAL', '2025-12-31', 1.0, 1.0, 25, 'analysis', 50, 'now', '2025-01-03')"
    )

    # pricing_run: max id = 5; price_daily references run_id = 30
    db.execute("INSERT INTO pricing_run (id, started_at, status) VALUES (5, 'now', 'completed')")
    db.execute(
        "INSERT INTO price_daily (id, asset_id, date, close, run_id) VALUES (1, 1, '2026-01-01', 150.0, 30)"
    )

    # quant_run: max id = 2; quant_risk_model references quant_run_id = 40
    db.execute(
        "INSERT INTO quant_run (id, command, started_at, status, engine_version) VALUES (2, 'test', 'now', 'completed', 'v1')"
    )
    db.execute(
        "INSERT INTO quant_risk_model (id, as_of, model_version, computed_at, quant_run_id) VALUES (1, '2026-01-01', 'v1', 'now', 40)"
    )
    db.execute(
        "INSERT INTO quant_portfolio (id, as_of, kind, objective, solver, status, n_positions, engine_version, computed_at, quant_run_id) "
        "VALUES (1, '2026-01-01', 'live', 'obj', 'solv', 'ok', 1, 'v1', 'now', 40)"
    )
    db.commit()

    child_tables = [
        "financial_facts",
        "sec_filing_section",
        "filing_cover_shares",
        "fundamental_metrics",
        "score_snapshot",
    ]
    before_children = {
        t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()]  # noqa: S608 - test constant
        for t in child_tables
    }
    before_targets = {
        t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()]  # noqa: S608 - test constant
        for t in TARGET_RUN_TABLES
    }
    views_before = {
        r[0]: r[1]
        for r in db.execute("SELECT name, sql FROM sqlite_master WHERE type='view'").fetchall()
    }
    indexes_before = {
        r[0]: r[1]
        for r in db.execute("SELECT name, sql FROM sqlite_master WHERE type='index'").fetchall()
    }
    triggers_before = {
        r[0]: r[1]
        for r in db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    fk_before = [tuple(r) for r in db.execute("PRAGMA foreign_key_check").fetchall()]
    assert len(fk_before) == 0

    # Apply migration m014
    applied = apply_migrations(db)
    assert 14 in applied

    # 1. Assert all 5 tables now have AUTOINCREMENT in sqlite_master
    for table in TARGET_RUN_TABLES:
        ddl = db.relation_ddl(table)
        assert ddl is not None and "AUTOINCREMENT" in ddl.upper(), (
            f"{table} missing AUTOINCREMENT: {ddl}"
        )

    # 2. Child row counts and contents unchanged (proves foreign_keys really was OFF)
    after_children = {
        t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()]  # noqa: S608 - test constant
        for t in child_tables
    }
    assert after_children == before_children, "Child row contents changed across migration!"

    # 3. Every row of the five tables equal (not only counts)
    after_targets = {
        t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()]  # noqa: S608 - test constant
        for t in TARGET_RUN_TABLES
    }
    assert after_targets == before_targets, "Target tables rows changed across migration!"

    # 4. Triggers, indexes, and views present and identical by name AND sql
    views_after = {
        r[0]: r[1]
        for r in db.execute("SELECT name, sql FROM sqlite_master WHERE type='view'").fetchall()
    }
    indexes_after = {
        r[0]: r[1]
        for r in db.execute("SELECT name, sql FROM sqlite_master WHERE type='index'").fetchall()
    }
    triggers_after = {
        r[0]: r[1]
        for r in db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    assert views_after == views_before, f"Views differ: {set(views_before) ^ set(views_after)}"
    assert indexes_after == indexes_before, (
        f"Indexes differ: {set(indexes_before) ^ set(indexes_after)}"
    )
    assert triggers_after == triggers_before, (
        f"Triggers differ: {set(triggers_before) ^ set(triggers_after)}"
    )

    # 5. Integrity check and foreign key check
    integrity = [tuple(r) for r in db.execute("PRAGMA integrity_check").fetchall()]
    assert integrity == [("ok",)], f"Integrity check failed: {integrity}"
    fk_after = [tuple(r) for r in db.execute("PRAGMA foreign_key_check").fetchall()]
    assert fk_after == fk_before, f"Foreign key check changed: {fk_after}"

    # 6. sqlite_sequence holds expected seq (>= max referencing id)
    seqs: dict[str, int] = {
        r[0]: r[1] for r in db.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
    }
    assert seqs["cycle_run"] == 15
    assert seqs["analysis_run"] == 25
    assert seqs["sec_filings"] == 50
    assert seqs["pricing_run"] == 30
    assert seqs["quant_run"] == 40

    # 7. Next INSERT into each table gets id = seq + 1, not recycled id
    db.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status) VALUES ('ENTITY_RESOLUTION', '2026-01-04', 'now', 'completed')"
    )
    row = db.execute(
        "SELECT id FROM cycle_run WHERE cycle_type = 'ENTITY_RESOLUTION' AND cycle_date = '2026-01-04'"
    ).fetchone()
    assert row[0] == 16, f"expected id 16, got {row[0]}"

    db.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, retrieved_at) VALUES (1, '10-Q', 2026, 'Q1', 'now')"
    )
    row = db.execute(
        "SELECT id FROM sec_filings WHERE form = '10-Q' AND fiscal_period = 'Q1'"
    ).fetchone()
    assert row[0] == 51, f"expected id 51, got {row[0]}"

    # 8. Idempotency: running a second time does nothing
    assert apply_migrations(db) == []
    _m014_autoincrement_run_ids(db)
    seqs_after = {
        r[0]: r[1] for r in db.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
    }
    assert seqs_after["cycle_run"] == 16
    assert seqs_after["sec_filings"] == 51


def test_failure_path_pre_existing_fk_violation_preserved() -> None:
    """A pre-existing FK violation migrates cleanly and keeps exactly that violation."""
    db = _make_v13_db()
    db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'AAPL', '0000320193')")
    db.execute(
        "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status) VALUES (1, 'SELECTION', '2026-01-01', 'now', 'completed')"
    )
    # Inject a pre-existing FK violation
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute(
        "INSERT INTO cycle_ranking (id, cycle_run_id, asset_id, rank, blended_score) VALUES (1, 1, 9999, 1, 0.5)"
    )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")

    fk_before = [tuple(r) for r in db.execute("PRAGMA foreign_key_check").fetchall()]
    assert len(fk_before) == 1
    assert fk_before[0][0] == "cycle_ranking"

    applied = apply_migrations(db)
    assert 14 in applied

    fk_after = [tuple(r) for r in db.execute("PRAGMA foreign_key_check").fetchall()]
    assert fk_after == fk_before, "Pre-existing FK violation was not preserved!"


def test_failure_path_mid_rebuild_failure_rolls_back() -> None:
    """A statement made to fail mid-rebuild (monkeypatch) leaves the five tables,

    views and triggers as before and schema_version 13.
    """
    db = _make_v13_db()
    views_before = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()
    }
    triggers_before = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    }

    orig_execute = db.execute

    def fail_execute(sql: str, *args: Any, **kwargs: Any) -> Any:
        if "CREATE TABLE sec_filings__new" in str(sql):
            raise RuntimeError("simulated mid-rebuild failure")
        return orig_execute(sql, *args, **kwargs)

    db.execute = fail_execute  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="simulated mid-rebuild failure"):
        apply_migrations(db)

    db.execute = orig_execute  # type: ignore[assignment]
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 13

    views_after = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()
    }
    triggers_after = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    assert views_after == views_before
    assert triggers_after == triggers_before
    for t in TARGET_RUN_TABLES:
        ddl = db.relation_ddl(t)
        assert ddl is not None
        assert "AUTOINCREMENT" not in ddl.upper()


def test_column_mismatch_refusal() -> None:
    """Pre-flight check refuses migration before any changes if table columns differ."""
    db = kg_schema.connect(":memory:", create_parents=False)
    db.create_schema(
        """
        CREATE TABLE cycle_run (
            id INTEGER PRIMARY KEY,
            cycle_type TEXT,
            cycle_date TEXT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            params_json TEXT,
            code_version TEXT,
            extra_unexpected_col TEXT
        );
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT, description TEXT);
        INSERT INTO schema_version VALUES (13, 'now', 'v13');
        """
    )
    with pytest.raises(
        RuntimeError, match=r"m014: table cycle_run columns .* differ from expected"
    ):
        apply_migrations(db)


def test_negative_deleted_highest_id_not_reused() -> None:
    db = _full_schema_db()
    # Insert 3 runs
    db.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status) VALUES ('SELECTION', '2026-01-01', 'now', 'completed')"
    )
    db.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status) VALUES ('SELECTION', '2026-01-02', 'now', 'completed')"
    )
    db.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status) VALUES ('SELECTION', '2026-01-03', 'now', 'completed')"
    )
    r3 = db.execute("SELECT id FROM cycle_run WHERE cycle_date = '2026-01-03'").fetchone()[0]

    # Delete the highest-id row
    db.execute("DELETE FROM cycle_run WHERE id = ?", (r3,))

    # Insert a new run
    db.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status) VALUES ('SELECTION', '2026-01-04', 'now', 'completed')"
    )
    r4 = db.execute("SELECT id FROM cycle_run WHERE cycle_date = '2026-01-04'").fetchone()[0]

    assert r4 > r3, f"deleted id {r3} was reused! New row got {r4}"


def test_negative_emptied_cycle_run_starts_at_seeded_seq() -> None:
    db = _full_schema_db()
    # Simulate production having emptied cycle_run completely
    db.execute("DELETE FROM cycle_checkpoint")
    db.execute("DELETE FROM cycle_ranking")
    db.execute("DELETE FROM cycle_run")

    # Seed sqlite_sequence with 500
    db.execute(SQLITE_SEQUENCE_QUERIES["delete"], ("cycle_run",))
    db.execute(SQLITE_SEQUENCE_QUERIES["insert"], ("cycle_run", 500))

    # Insert a new run
    db.execute(
        "INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status) VALUES ('SELECTION', '2026-01-01', 'now', 'completed')"
    )
    new_id = db.execute("SELECT id FROM cycle_run").fetchone()[0]
    assert new_id == 501, f"expected id 501, got {new_id}"


def test_unresolved_run_references_check() -> None:
    db = _full_schema_db()
    # Clean DB: no unresolved references
    assert unresolved_run_references(db) == []

    # Inject an orphaned reference in cycle_ranking (disable FKs to allow inserting orphan)
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("INSERT INTO assets (id, ticker, company_name) VALUES (999, 'XYZ', 'XYZ Corp')")
    db.execute(
        "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score) VALUES (8888, 999, 1, 0.5)"
    )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")

    violations = unresolved_run_references(db)
    assert len(violations) == 1
    v = violations[0]
    assert v["table"] == "cycle_ranking"
    assert v["column"] == "cycle_run_id"
    assert v["reason"] == "missing"
    assert v["unresolved_count"] == 1
    assert v["max_unresolved_id"] == 8888
    assert v["sample_ids"] == [8888]

    # Clean it up
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("DELETE FROM cycle_ranking WHERE cycle_run_id = 8888")
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
    assert unresolved_run_references(db) == []


def test_unresolved_run_references_wrong_cycle_type_and_unmapped_kind() -> None:
    """Wrong cycle_type targets (edge -> SELECTION) and unmapped run_kinds are flagged;

    also verifies execution on raw sqlite3 tuple connections.
    """
    conn = sqlite3.connect(":memory:")
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE cycle_run (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_type TEXT);
        CREATE TABLE shared_executive_edge (id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES cycle_run(id));
        CREATE TABLE score_snapshot (id INTEGER PRIMARY KEY, run_id INTEGER, run_kind TEXT);
        INSERT INTO cycle_run VALUES (1, 'SELECTION');
        INSERT INTO shared_executive_edge VALUES (1, 1);
        INSERT INTO score_snapshot VALUES (1, 10, 'bogus_kind');
        """
    )
    conn.commit()

    raw_db = Database(conn)
    violations = unresolved_run_references(raw_db)
    assert len(violations) == 2

    reasons = {v["reason"] for v in violations}
    assert reasons == {"wrong_cycle_type", "unmapped_run_kind"}

    edge_v = next(v for v in violations if v["table"] == "shared_executive_edge")
    assert edge_v["reason"] == "wrong_cycle_type"
    assert edge_v["unresolved_count"] == 1
    assert edge_v["sample_ids"] == [1]
    assert edge_v["cycle_type"] == "SELECTION"

    kind_v = next(v for v in violations if v["table"] == "score_snapshot")
    assert kind_v["reason"] == "unmapped_run_kind"
    assert kind_v["unresolved_count"] == 1
    assert kind_v["sample_ids"] == [10]
    assert kind_v["run_kind"] == "bogus_kind"


def test_invalid_sec_filings_accessions_check() -> None:
    db = _full_schema_db()
    db.execute(
        "INSERT INTO assets (id, ticker, company_name, cik) VALUES (1, 'AAPL', 'Apple Inc', '0000320193')"
    )
    # Clean filing
    db.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, retrieved_at, accession_number) "
        "VALUES (1, '10-K', 2025, 'FY', 'now', '0000320193-25-000100')"
    )
    assert invalid_sec_filings_accessions(db) == []

    # Insert a row with NULL accession
    db.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, retrieved_at, accession_number) "
        "VALUES (1, '10-Q', 2026, 'Q1', 'now', NULL)"
    )
    bad = invalid_sec_filings_accessions(db)
    assert len(bad) == 1
    assert bad[0]["issue"] == "null_accession"
    assert bad[0]["form"] == "10-Q"


def test_invalid_sec_filings_accessions_dual_class_and_duplicates() -> None:
    """One asset with the same accession twice is flagged; two assets sharing one accession

    (dual-class) is not.
    """
    db = kg_schema.connect(":memory:", create_parents=False)
    db.create_schema(
        """
        CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT, cik TEXT);
        CREATE TABLE sec_filings (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER REFERENCES assets(id),
            form TEXT,
            fiscal_year INT,
            fiscal_period TEXT,
            filing_date TEXT,
            accession_number TEXT,
            retrieved_at TEXT
        );
        """
    )
    db.execute(
        "INSERT INTO assets VALUES (1, 'GOOG', '0001652044'), (2, 'GOOGL', '0001652044'), (3, 'AAPL', '0000320193')"
    )

    # Dual-class: asset 1 and asset 2 share accession '0001652044-25-000010'
    db.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, retrieved_at, accession_number) "
        "VALUES (1, 1, '10-K', 2025, 'FY', 'now', '0001652044-25-000010')"
    )
    db.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, retrieved_at, accession_number) "
        "VALUES (2, 2, '10-K', 2025, 'FY', 'now', '0001652044-25-000010')"
    )

    # Should NOT be flagged (dual class sharing accession is valid)
    assert invalid_sec_filings_accessions(db) == []

    # One asset with duplicate accession: asset 3 has two rows with same accession
    db.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, retrieved_at, accession_number) "
        "VALUES (3, 3, '10-K', 2025, 'FY', 'now', '0000320193-25-000100')"
    )
    db.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, retrieved_at, accession_number) "
        "VALUES (4, 3, '10-Q', 2025, 'Q1', 'now', '0000320193-25-000100')"
    )

    bad = invalid_sec_filings_accessions(db)
    assert len(bad) == 2
    assert all(b["asset_id"] == 3 for b in bad)
    assert all(b["issue"] == "duplicate_per_asset" for b in bad)


def test_verify_pilot_t145_raw_connection_does_not_crash(tmp_path: Path) -> None:
    """Verify verify_pilot.py t145 path executes on raw databases without crashing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE schema_version (version INT, applied_at TEXT, description TEXT)")
    conn.execute("CREATE TABLE cycle_run (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_type TEXT)")
    conn.execute("CREATE TABLE veto (id INTEGER PRIMARY KEY, run_id INTEGER)")
    conn.execute("INSERT INTO veto (id, run_id) VALUES (1, 999)")
    conn.commit()
    conn.close()

    result = subprocess.run(  # noqa: S603
        [sys.executable, "scripts/verify_pilot.py", "--db", str(db_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    t145_lines = [line for line in result.stdout.splitlines() if "T-145" in line]
    assert len(t145_lines) == 1, f"Expected 1 T-145 line in stdout: {result.stdout} {result.stderr}"
    assert "[FAIL] T-145" in t145_lines[0]
    assert "veto.run_id=1 (missing)" in t145_lines[0]


# -- T-012: the orchestrator provenance tables ----------------------------------------------


def test_m015_is_a_marker_that_replays_over_orchestrator_rows() -> None:
    """m015 (T-012) rebuilds no table: a fresh ``ensure()`` database with orchestrator rows
    keeps them (and its DDL) when migrated, and the floor reaches 15."""
    db = kg_schema.connect(":memory:", create_parents=False)
    kg_schema.ensure(db)  # additive only: orchestrator_run / orchestrator_step + their views
    assert db.relation_exists("orchestrator_run") and db.relation_exists("orchestrator_step")
    db.execute(
        "INSERT INTO orchestrator_run "
        "(handle, kind, as_of, surface, status, started_at) "
        "VALUES ('h', 'pipeline', '2026-10-02', 'cli', 'completed', 'now')"
    )
    run_id = db.execute("SELECT id FROM orchestrator_run WHERE handle = 'h'").fetchone()[0]
    db.execute(
        "INSERT INTO orchestrator_step "
        "(orchestrator_run_id, ordinal, step, argv_json, status) "
        "VALUES (?, 1, 'pricing', '[]', 'completed')",
        (run_id,),
    )
    db.commit()
    before_ddl = {t: db.relation_ddl(t) for t in ("orchestrator_run", "orchestrator_step")}

    applied = apply_migrations(db)

    assert 15 in applied
    assert db.execute("SELECT COUNT(*) FROM orchestrator_run").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM orchestrator_step").fetchone()[0] == 1
    after_ddl = {t: db.relation_ddl(t) for t in ("orchestrator_run", "orchestrator_step")}
    assert after_ddl == before_ddl
    assert current_version(db) == 15


def test_unresolved_run_references_flags_orchestrator_steps() -> None:
    """An orchestrator step's run_id is checked like any other: a missing target, a wrong
    cycle_type, and a NULL/unexpected run_kind are all flagged (T-012)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE cycle_run (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_type TEXT);
        CREATE TABLE pricing_run (id INTEGER PRIMARY KEY);
        CREATE TABLE orchestrator_run (id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT);
        CREATE TABLE orchestrator_step (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orchestrator_run_id INTEGER REFERENCES orchestrator_run(id),
            run_kind TEXT, run_id INTEGER);
        INSERT INTO orchestrator_run VALUES (1, 'h');
        INSERT INTO orchestrator_step VALUES (1, 1, 'pricing', 999);
        INSERT INTO cycle_run VALUES (7, 'REPLAY');
        INSERT INTO orchestrator_step VALUES (2, 1, 'cycle', 7);
        INSERT INTO orchestrator_step VALUES (3, 1, 'bogus', 5);
        INSERT INTO orchestrator_step VALUES (4, 1, NULL, 6);
        """
    )
    conn.commit()

    violations = unresolved_run_references(Database(conn))
    by_reason = {v["reason"] for v in violations if v["table"] == "orchestrator_step"}
    assert by_reason == {"missing", "wrong_cycle_type", "unmapped_run_kind"}
    missing = next(v for v in violations if v["reason"] == "missing")
    assert missing["column"] == "run_id" and missing["sample_ids"] == [999]
    wrong = next(v for v in violations if v["reason"] == "wrong_cycle_type")
    assert wrong["cycle_type"] == "REPLAY" and wrong["sample_ids"] == [7]
