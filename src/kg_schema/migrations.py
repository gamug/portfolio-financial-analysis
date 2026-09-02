"""Non-additive schema changes -- table rebuilds SQLite cannot do with ``ALTER``.

These NEVER run from :func:`kg_schema.ensure`'s automatic path. They run only via
``python -m fundamental_agent migrate`` / ``python -m pricing_agent migrate`` so the
shared ``KG_FINANCIAL_DB`` is only reshaped deliberately, with the other repos quiesced.

Each migration runs inside one transaction; on success its version is recorded in
``schema_version``. Re-running is a no-op once the version is recorded.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from kg_schema import version as _version

Migration = Callable[[sqlite3.Connection], None]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _is_view(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# -- m001 --------------------------------------------------------------------


def _m001_bootstrap(conn: sqlite3.Connection) -> None:
    """No structural change -- just establish the version floor at 1."""
    _version.ensure(conn)


# -- m002: financial_facts append-only, versioned ---------------------------


def _m002_financial_facts(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "financial_facts"):
        return
    cols = _columns(conn, "financial_facts")
    if "filing_version" not in cols or "event_time" not in cols:
        return  # additive columns not present yet; run kg_schema.ensure first
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE financial_facts__new (
            id               INTEGER PRIMARY KEY,
            filing_id        INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
            statement        TEXT NOT NULL,
            concept          TEXT NOT NULL,
            standard_concept TEXT,
            label            TEXT,
            period_key       TEXT NOT NULL,
            value            REAL,
            filing_version   TEXT NOT NULL DEFAULT 'pre-v1',
            event_time       TEXT NOT NULL DEFAULT '',
            ingested_at      TEXT,
            run_id           INTEGER,
            UNIQUE (filing_id, statement, concept, period_key, filing_version)
        );
        INSERT INTO financial_facts__new
            (id, filing_id, statement, concept, standard_concept, label, period_key, value,
             filing_version, event_time, ingested_at, run_id)
        SELECT ff.id, ff.filing_id, ff.statement, ff.concept, ff.standard_concept, ff.label,
               ff.period_key, ff.value,
               COALESCE(NULLIF(ff.filing_version, ''), f.accession_number, 'pre-v1'),
               COALESCE(NULLIF(ff.event_time, ''), f.period_end, substr(ff.period_key, 1, 10),
                        f.retrieved_at),
               COALESCE(ff.ingested_at, f.retrieved_at),
               ff.run_id
        FROM financial_facts ff
        LEFT JOIN sec_filings f ON f.id = ff.filing_id;
        DROP TABLE financial_facts;
        ALTER TABLE financial_facts__new RENAME TO financial_facts;
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


# -- m003: fundamental_metrics append-only, versioned ----------------------


def _m003_fundamental_metrics(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "fundamental_metrics"):
        return
    cols = _columns(conn, "fundamental_metrics")
    if "engine_version" not in cols or "event_time" not in cols:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE fundamental_metrics__new (
            id           INTEGER PRIMARY KEY,
            filing_id    INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
            metric_group TEXT NOT NULL,
            metric_name  TEXT NOT NULL,
            value        REAL,
            unit         TEXT,
            inputs_json  TEXT,
            computed_at  TEXT NOT NULL,
            engine_version TEXT NOT NULL DEFAULT 'pre-v1',
            event_time   TEXT NOT NULL DEFAULT '',
            run_id       INTEGER,
            UNIQUE (filing_id, metric_group, metric_name, engine_version)
        );
        INSERT INTO fundamental_metrics__new
            (id, filing_id, metric_group, metric_name, value, unit, inputs_json, computed_at,
             engine_version, event_time, run_id)
        SELECT m.id, m.filing_id, m.metric_group, m.metric_name, m.value, m.unit, m.inputs_json,
               m.computed_at,
               COALESCE(NULLIF(m.engine_version, ''), 'pre-v1'),
               COALESCE(NULLIF(m.event_time, ''), f.period_end, m.computed_at),
               m.run_id
        FROM fundamental_metrics m
        LEFT JOIN sec_filings f ON f.id = m.filing_id;
        DROP TABLE fundamental_metrics;
        ALTER TABLE fundamental_metrics__new RENAME TO fundamental_metrics;
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


# -- m004: fundamental_snapshot -> score_snapshot + compat view -----------


def _m004_score_snapshot(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "score_snapshot"):
        return  # additive DDL must have run first
    if _is_view(conn, "fundamental_snapshot") or not _table_exists(conn, "fundamental_snapshot"):
        return  # already migrated (view exists) or nothing to migrate
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        INSERT OR IGNORE INTO score_snapshot
            (asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, filing_id, rating, narrative, strengths_json, risks_json)
        SELECT s.asset_id, 'FUNDAMENTAL', s.score, s.score,
               COALESCE((SELECT f.period_end FROM sec_filings f WHERE f.id = s.filing_id),
                        s.created_at),
               s.created_at, s.model, s.metrics_json, s.filing_id, s.rating, s.narrative,
               s.strengths_json, s.risks_json
        FROM fundamental_snapshot s
        """
    )
    conn.executescript(
        """
        ALTER TABLE fundamental_snapshot RENAME TO fundamental_snapshot_legacy;
        CREATE VIEW fundamental_snapshot AS
        SELECT s.id, s.asset_id, s.filing_id, f.form, f.fiscal_period,
               s.raw_value AS score, s.rating, s.narrative, s.strengths_json, s.risks_json,
               s.model, s.inputs_json AS metrics_json, s.computed_at AS created_at
        FROM score_snapshot s
        JOIN sec_filings f ON f.id = s.filing_id
        WHERE s.score_type = 'FUNDAMENTAL';
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


# -- m005: widen score_snapshot.score_type CHECK to admit 'SECTOR' -----------


def _m005_score_type_sector(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "score_snapshot"):
        return
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'score_snapshot'"
    ).fetchone()
    if sql_row is None or "'SECTOR'" in (sql_row[0] or ""):
        return  # fresh DB already has the widened CHECK
    had_compat_view = _is_view(conn, "fundamental_snapshot")
    conn.execute("PRAGMA foreign_keys = OFF")
    # Views that read score_snapshot must go before the table rebuild; ensure()'s
    # post-migration ensure_views() call puts the read-contract one back.
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_score_snapshot;
        DROP VIEW IF EXISTS fundamental_snapshot;
        CREATE TABLE score_snapshot__new (
            id               INTEGER PRIMARY KEY,
            asset_id         INTEGER NOT NULL REFERENCES assets(id),
            score_type       TEXT NOT NULL CHECK (score_type IN
                                ('FUNDAMENTAL', 'QUANTITATIVE', 'TECHNICAL', 'SEMANTIC', 'SECTOR')),
            raw_value        REAL,
            normalized_score REAL,
            event_time       TEXT NOT NULL,
            computed_at      TEXT NOT NULL,
            model            TEXT,
            inputs_json      TEXT,
            run_id           INTEGER,
            run_kind         TEXT,
            filing_id        INTEGER REFERENCES sec_filings(id),
            rating           TEXT,
            narrative        TEXT,
            strengths_json   TEXT,
            risks_json       TEXT,
            UNIQUE (asset_id, score_type, event_time)
        );
        INSERT INTO score_snapshot__new
            (id, asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, run_id, run_kind, filing_id, rating, narrative, strengths_json,
             risks_json)
        SELECT id, asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
               model, inputs_json, run_id, run_kind, filing_id, rating, narrative, strengths_json,
               risks_json
        FROM score_snapshot;
        DROP TABLE score_snapshot;
        ALTER TABLE score_snapshot__new RENAME TO score_snapshot;
        CREATE INDEX IF NOT EXISTS ix_score_snapshot_type_time
            ON score_snapshot (score_type, event_time);
        """
    )
    if had_compat_view:
        conn.executescript(
            """
            CREATE VIEW fundamental_snapshot AS
            SELECT s.id, s.asset_id, s.filing_id, f.form, f.fiscal_period,
                   s.raw_value AS score, s.rating, s.narrative, s.strengths_json, s.risks_json,
                   s.model, s.inputs_json AS metrics_json, s.computed_at AS created_at
            FROM score_snapshot s
            JOIN sec_filings f ON f.id = s.filing_id
            WHERE s.score_type = 'FUNDAMENTAL';
            """
        )
    conn.execute("PRAGMA foreign_keys = ON")


# -- m006: rename score_type 'QUANTITATIVE' -> 'VALORIZATION' -----------------


def _m006_quantitative_to_valorization(conn: sqlite3.Connection) -> None:
    """The cycle's value/quality/size factor blend was mislabelled 'QUANTITATIVE'.
    Rename it to 'VALORIZATION' everywhere it is persisted: the score_type CHECK,
    the stored rows, and the score-type keys inside the recorded blend JSON.
    """
    if not _table_exists(conn, "score_snapshot"):
        return
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'score_snapshot'"
    ).fetchone()
    if sql_row is None or "'VALORIZATION'" in (sql_row[0] or ""):
        return  # fresh DB already has the renamed CHECK
    had_compat_view = _is_view(conn, "fundamental_snapshot")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_score_snapshot;
        DROP VIEW IF EXISTS fundamental_snapshot;
        CREATE TABLE score_snapshot__new (
            id               INTEGER PRIMARY KEY,
            asset_id         INTEGER NOT NULL REFERENCES assets(id),
            score_type       TEXT NOT NULL CHECK (score_type IN
                                ('FUNDAMENTAL', 'VALORIZATION', 'TECHNICAL', 'SEMANTIC', 'SECTOR')),
            raw_value        REAL,
            normalized_score REAL,
            event_time       TEXT NOT NULL,
            computed_at      TEXT NOT NULL,
            model            TEXT,
            inputs_json      TEXT,
            run_id           INTEGER,
            run_kind         TEXT,
            filing_id        INTEGER REFERENCES sec_filings(id),
            rating           TEXT,
            narrative        TEXT,
            strengths_json   TEXT,
            risks_json       TEXT,
            UNIQUE (asset_id, score_type, event_time)
        );
        INSERT INTO score_snapshot__new
            (id, asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, run_id, run_kind, filing_id, rating, narrative, strengths_json,
             risks_json)
        SELECT id, asset_id,
               CASE score_type WHEN 'QUANTITATIVE' THEN 'VALORIZATION' ELSE score_type END,
               raw_value, normalized_score, event_time, computed_at,
               model, inputs_json, run_id, run_kind, filing_id, rating, narrative, strengths_json,
               risks_json
        FROM score_snapshot;
        DROP TABLE score_snapshot;
        ALTER TABLE score_snapshot__new RENAME TO score_snapshot;
        CREATE INDEX IF NOT EXISTS ix_score_snapshot_type_time
            ON score_snapshot (score_type, event_time);
        """
    )
    # The recorded blend carries score_type as JSON object keys / component labels.
    if _table_exists(conn, "cycle_run"):
        conn.execute(
            "UPDATE cycle_run SET params_json = "
            "REPLACE(params_json, '\"QUANTITATIVE\"', '\"VALORIZATION\"') "
            "WHERE params_json LIKE '%\"QUANTITATIVE\"%'"
        )
    if _table_exists(conn, "cycle_ranking"):
        conn.execute(
            "UPDATE cycle_ranking SET components_json = "
            "REPLACE(components_json, '\"QUANTITATIVE\"', '\"VALORIZATION\"') "
            "WHERE components_json LIKE '%\"QUANTITATIVE\"%'"
        )
    if had_compat_view:
        conn.executescript(
            """
            CREATE VIEW fundamental_snapshot AS
            SELECT s.id, s.asset_id, s.filing_id, f.form, f.fiscal_period,
                   s.raw_value AS score, s.rating, s.narrative, s.strengths_json, s.risks_json,
                   s.model, s.inputs_json AS metrics_json, s.computed_at AS created_at
            FROM score_snapshot s
            JOIN sec_filings f ON f.id = s.filing_id
            WHERE s.score_type = 'FUNDAMENTAL';
            """
        )
    conn.execute("PRAGMA foreign_keys = ON")


MIGRATIONS: list[tuple[int, str, Migration]] = [
    (1, "bootstrap schema_version", _m001_bootstrap),
    (2, "financial_facts: append-only, filing_version in key, event_time", _m002_financial_facts),
    (
        3,
        "fundamental_metrics: append-only, engine_version in key, event_time",
        _m003_fundamental_metrics,
    ),
    (4, "fundamental_snapshot -> score_snapshot (+ compatibility view)", _m004_score_snapshot),
    (5, "score_snapshot.score_type CHECK widened to admit 'SECTOR'", _m005_score_type_sector),
    (
        6,
        "score_snapshot.score_type 'QUANTITATIVE' renamed to 'VALORIZATION' (rows + CHECK + blend JSON)",
        _m006_quantitative_to_valorization,
    ),
]


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Run every migration whose version exceeds the recorded floor. Returns applied ids."""
    _version.ensure(conn)
    at = _version.current_version(conn)
    applied: list[int] = []
    for ver, desc, fn in MIGRATIONS:
        if ver <= at:
            continue
        fn(conn)
        _version.record(conn, ver, desc)
        conn.commit()
        applied.append(ver)
    return applied
