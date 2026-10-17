"""Non-additive schema changes -- table rebuilds ``ALTER`` cannot express.

These NEVER run from :func:`kg_schema.ensure`'s automatic path. They run only via
``python -m fundamental_agent migrate`` / ``python -m pricing_agent migrate`` so the
shared ``KG_FINANCIAL_DB`` is only reshaped deliberately, with the other repos quiesced.

Each migration runs inside one transaction; on success its version is recorded in
``schema_version``. Re-running is a no-op once the version is recorded.

Catalog lookups and multi-statement DDL go through
``portfolio_common.db.Database`` (``relation_exists`` / ``relation_kind`` /
``relation_ddl`` / ``create_schema``). The rebuild *bodies* below -- the
``CREATE <t>__new`` / copy / drop / rename dance and the ``PRAGMA
foreign_keys`` toggles that bracket them -- are SQLite's own workaround for
its limited ``ALTER``; a different engine's migration path would be written
fresh rather than translated, so those stay as-is.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime

from portfolio_common.db import Database

from . import availability as _availability
from . import identity as _identity
from . import queries as _queries
from .ddl import (
    M014_REBUILD_COLUMNS,
    M014_TABLE_REBUILD_STATEMENTS,
    MAX_ID_QUERIES,
    MAX_REFERENCE_QUERIES,
    REQUIRED_COLUMNS,
    RUN_REFERENCE_MAP,
    SQLITE_SEQUENCE_QUERIES,
    TARGET_RUN_TABLES,
)

Migration = Callable[[Database], None]


def _table_exists(db: Database, name: str) -> bool:
    return db.relation_exists(name)


def _is_view(db: Database, name: str) -> bool:
    return db.relation_kind(name) == "view"


def _columns(db: Database, table: str) -> set[str]:
    return set(db.table_columns(table))


# -- T-154: the target-shape guards ------------------------------------------
#
# A rebuilding migration copies only the columns it itself knows. Replaying one against a table that a
# later release widened (``preferred_sign``/``correction_rule`` on ``financial_facts`` (T-148/T-118),
# ``available_at`` on ``fundamental_metrics``/``score_snapshot`` (T-107), ``prompt_hash`` on
# ``score_snapshot`` (T-113), ``expires_on``/``expiry_history_json`` on ``veto`` (T-070)) would bring
# those columns back NULL. Each rebuilding migration below therefore returns early -- changing nothing
# -- when its own target shape is already there; :func:`apply_migrations` still records the version, so
# the floor ends up correct. The same checks answer :func:`at_current_shape`, which ``ensure`` uses to
# stamp a database that was born at the current shape (D2).
#
# The rebuilds also carry the columns the current code adds, exactly as m005/m006 already carried
# ``forensic_flags_json`` (T-041): the column is added to the old table first (a no-op when present),
# then copied, so a rebuild that legitimately runs on an older shape cannot drop it either.

_UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    # m002 / m003: the append-only key each rebuild widens with its own version column.
    "financial_facts": ("filing_id", "statement", "concept", "period_key", "filing_version"),
    "fundamental_metrics": ("filing_id", "metric_group", "metric_name", "engine_version"),
}


def _has_unique_key(db: Database, table: str) -> bool:
    """True when *table* carries a UNIQUE index over exactly :data:`_UNIQUE_KEYS`\\ [table].

    Read through the ``pragma_index_list`` / ``pragma_index_info`` table-valued functions: the
    table and index names are bound parameters, never interpolated into SQL."""
    wanted = _UNIQUE_KEYS[table]
    for row in db.execute('SELECT name, "unique" AS u FROM pragma_index_list(?)', (table,)):
        if not int(row["u"]):
            continue
        columns = tuple(
            str(r["name"])
            for r in db.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (str(row["name"]),)
            )
        )
        if columns == wanted:
            return True
    return False


def target_unique_key(table: str) -> tuple[str, ...] | None:
    """The append-only ``UNIQUE`` key *table* is rebuilt to (m002 / m003; SPEC §5), or ``None`` when
    no rebuilding migration owns it.

    The public reader the agents' own DDL and write paths assert against (T-157), so a fresh
    database's key and the migrations' target cannot drift apart."""
    return _UNIQUE_KEYS.get(table)


def at_target_unique_key(db: Database, table: str) -> bool:
    """Whether *table* carries :func:`target_unique_key`'s key -- T-154's guard, read for one table.

    ``True`` when the table does not exist, and when no rebuilding migration owns it (there is no
    target to miss). ``False`` is the state T-157 refuses to write to: the table exists with an
    older, narrower key, where ``INSERT OR IGNORE`` would drop a new version's row in silence."""
    if table not in _UNIQUE_KEYS or not _table_exists(db, table):
        return True
    return _has_unique_key(db, table)


def _m002_at_target_shape(db: Database) -> bool:
    """m002's target: ``financial_facts`` keyed by ``filing_version``."""
    if not _table_exists(db, "financial_facts"):
        return True  # nothing to rebuild
    return _has_unique_key(db, "financial_facts")


def _m003_at_target_shape(db: Database) -> bool:
    """m003's target: ``fundamental_metrics`` keyed by ``engine_version``."""
    if not _table_exists(db, "fundamental_metrics"):
        return True
    return _has_unique_key(db, "fundamental_metrics")


def _m004_at_target_shape(db: Database) -> bool:
    """m004's target: ``fundamental_snapshot`` is the compatibility VIEW, not the legacy table.

    A legacy table with no rows holds nothing to migrate, so it counts as already at the target shape:
    a database born at the current shape has that empty table (the agent's own DDL), and the compat
    view only exists for a database that actually carried legacy snapshots."""
    if not _table_exists(db, "fundamental_snapshot") or _is_view(db, "fundamental_snapshot"):
        return True
    return int(db.execute("SELECT COUNT(*) FROM fundamental_snapshot").fetchone()[0]) == 0


def _m005_at_target_shape(db: Database) -> bool:
    """m005's target: the ``score_snapshot.score_type`` CHECK admits ``'SECTOR'``."""
    ddl = db.relation_ddl("score_snapshot")
    return ddl is None or "'SECTOR'" in ddl


def _m006_at_target_shape(db: Database) -> bool:
    """m006's target: the CHECK admits ``'VALORIZATION'`` (and no row says ``'QUANTITATIVE'``)."""
    ddl = db.relation_ddl("score_snapshot")
    return ddl is None or "'VALORIZATION'" in ddl


def _m007_at_target_shape(db: Database) -> bool:
    """m007's target: the NULL-safe unique book key is enforced.

    ``relation_exists`` cannot answer this -- an index is neither a table nor a view -- so the
    index list is read through ``pragma_index_list`` (a literal, with the name bound)."""
    if not _table_exists(db, "quant_portfolio"):
        return True
    row = db.execute(
        "SELECT 1 FROM pragma_index_list('quant_portfolio') WHERE name = ?",
        ("ux_quant_portfolio_book",),
    ).fetchone()
    return row is not None


def _m009_at_target_shape(db: Database) -> bool:
    """m009's target: ``veto`` is the stint table (it has ``raised_on``)."""
    ddl = db.relation_ddl("veto")
    return ddl is None or "raised_on" in ddl


def _m014_at_target_shape(db: Database) -> bool:
    """m014's target: every run table it owns already has AUTOINCREMENT (or does not exist)."""
    return all(
        not _table_exists(db, table) or "AUTOINCREMENT" in (db.relation_ddl(table) or "").upper()
        for table in TARGET_RUN_TABLES
    )


#: The migrations that rebuild a table, each with its "already at the target shape" guard. D2's
#: stamping asks every one of them before it records a database as born at the current shape.
REBUILD_GUARDS: dict[int, Callable[[Database], bool]] = {
    2: _m002_at_target_shape,
    3: _m003_at_target_shape,
    4: _m004_at_target_shape,
    5: _m005_at_target_shape,
    6: _m006_at_target_shape,
    7: _m007_at_target_shape,
    9: _m009_at_target_shape,
    14: _m014_at_target_shape,
}


def at_current_shape(db: Database) -> bool:
    """True when every table a migration would rebuild is already at that migration's target shape.

    Read-only. A database that answers True has nothing a migration would change, so ``ensure`` may
    record it as born at the current shape (D2) instead of letting ``migrate`` replay the rebuilds."""
    return all(check(db) for check in REBUILD_GUARDS.values())


#: The description ``ensure`` records on every version of a database that is already at the current
#: shape (D2) -- distinct from the migrations' own descriptions, so the stamping is visible.
STAMPED_DESCRIPTION = "stamped: created at the current shape"


def stamp_current_shape(db: Database) -> int:
    """Record every migration as applied on a database that is already at the current shape.

    A database created by ``kg_schema.ensure`` alone (no ``run_migrations``) -- the path both agents'
    ``ensure_schema`` take -- has no ``schema_version`` rows, so ``python -m <agent> migrate`` would
    replay m001..m014 on it. When the floor is empty **and** every table a migration would rebuild is
    already at that migration's target shape (:func:`at_current_shape`), every version is recorded here
    and nothing is replayed. A database with an empty floor but an older shape is left alone: ``migrate``
    then runs the real rebuilds, which no longer drop a column the current code adds.

    Returns the number of versions recorded (0 when the floor is already set, or the shape is older)."""
    if _queries.current_version(db) != 0:
        return 0
    if not at_current_shape(db):
        return 0
    for version, _description, _fn in MIGRATIONS:
        _queries.record(db, version, STAMPED_DESCRIPTION)
    return len(MIGRATIONS)


# -- m001 --------------------------------------------------------------------


def _m001_bootstrap(db: Database) -> None:
    """No structural change -- just establish the version floor at 1."""
    _queries.ensure(db)


# -- m002: financial_facts append-only, versioned ---------------------------


def _m002_financial_facts(db: Database) -> None:
    if not _table_exists(db, "financial_facts"):
        return
    cols = _columns(db, "financial_facts")
    if "filing_version" not in cols or "event_time" not in cols:
        return  # additive columns not present yet; run kg_schema.ensure first
    if _m002_at_target_shape(db):
        return  # T-154: already keyed by filing_version -- never replay the rebuild
    # T-154: the rebuild copies only m002's own column list, so a column a later release added
    # (T-118's correction_rule, T-148's preferred_sign) is added to the old table first -- a no-op
    # when it is already there -- exactly as m005/m006 do for forensic_flags_json (T-041).
    db.ensure_columns("financial_facts", REQUIRED_COLUMNS["financial_facts"])
    db.execute("PRAGMA foreign_keys = OFF")
    db.create_schema(
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
            correction_rule  TEXT,
            preferred_sign   REAL,
            UNIQUE (filing_id, statement, concept, period_key, filing_version)
        );
        INSERT INTO financial_facts__new
            (id, filing_id, statement, concept, standard_concept, label, period_key, value,
             filing_version, event_time, ingested_at, run_id, correction_rule, preferred_sign)
        SELECT ff.id, ff.filing_id, ff.statement, ff.concept, ff.standard_concept, ff.label,
               ff.period_key, ff.value,
               COALESCE(NULLIF(ff.filing_version, ''), f.accession_number, 'pre-v1'),
               COALESCE(NULLIF(ff.event_time, ''), f.period_end, substr(ff.period_key, 1, 10),
                        f.retrieved_at),
               COALESCE(ff.ingested_at, f.retrieved_at),
               ff.run_id, ff.correction_rule, ff.preferred_sign
        FROM financial_facts ff
        LEFT JOIN sec_filings f ON f.id = ff.filing_id;
        DROP TABLE financial_facts;
        ALTER TABLE financial_facts__new RENAME TO financial_facts;
        """
    )
    db.execute("PRAGMA foreign_keys = ON")


# -- m003: fundamental_metrics append-only, versioned ----------------------


def _m003_fundamental_metrics(db: Database) -> None:
    if not _table_exists(db, "fundamental_metrics"):
        return
    cols = _columns(db, "fundamental_metrics")
    if "engine_version" not in cols or "event_time" not in cols:
        return
    if _m003_at_target_shape(db):
        return  # T-154: already keyed by engine_version -- never replay the rebuild
    # T-154: carry the column a later release added (T-107's available_at), as m002 does above.
    db.ensure_columns("fundamental_metrics", REQUIRED_COLUMNS["fundamental_metrics"])
    db.execute("PRAGMA foreign_keys = OFF")
    db.create_schema(
        """
        DROP VIEW IF EXISTS v_fundamental_metric;
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
            available_at TEXT,
            UNIQUE (filing_id, metric_group, metric_name, engine_version)
        );
        INSERT INTO fundamental_metrics__new
            (id, filing_id, metric_group, metric_name, value, unit, inputs_json, computed_at,
             engine_version, event_time, run_id, available_at)
        SELECT m.id, m.filing_id, m.metric_group, m.metric_name, m.value, m.unit, m.inputs_json,
               m.computed_at,
               COALESCE(NULLIF(m.engine_version, ''), 'pre-v1'),
               COALESCE(NULLIF(m.event_time, ''), f.period_end, m.computed_at),
               m.run_id, m.available_at
        FROM fundamental_metrics m
        LEFT JOIN sec_filings f ON f.id = m.filing_id;
        DROP TABLE fundamental_metrics;
        ALTER TABLE fundamental_metrics__new RENAME TO fundamental_metrics;
        """
    )
    db.execute("PRAGMA foreign_keys = ON")


# -- m004: fundamental_snapshot -> score_snapshot + compat view -----------


def _m004_score_snapshot(db: Database) -> None:
    if not _table_exists(db, "score_snapshot"):
        return  # additive DDL must have run first
    if _m004_at_target_shape(db):
        return  # T-154: the compat view is already there (or nothing to migrate)
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute(
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
    db.create_schema(
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
    db.execute("PRAGMA foreign_keys = ON")


def _ensure_forensic_flags_column(db: Database) -> None:
    """The m005/m006 rebuilds copy ``forensic_flags_json`` (T-041), so a database that is
    migrated before T-074 populates it keeps its flags. ``ensure()`` adds the column before
    migrating; this covers a direct :func:`apply_migrations` call on an older table."""
    db.ensure_columns("score_snapshot", REQUIRED_COLUMNS["score_snapshot"])


# -- m005: widen score_snapshot.score_type CHECK to admit 'SECTOR' -----------


def _m005_score_type_sector(db: Database) -> None:
    if _m005_at_target_shape(db):
        return  # T-154: the CHECK already admits 'SECTOR' (a fresh DB) -- never replay the rebuild
    _ensure_forensic_flags_column(db)
    had_compat_view = _is_view(db, "fundamental_snapshot")
    db.execute("PRAGMA foreign_keys = OFF")
    # Views that read score_snapshot must go before the table rebuild; ensure()'s
    # post-migration ensure_views() call puts the read-contract one back.
    db.create_schema(
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
            forensic_flags_json TEXT,
            available_at     TEXT,
            prompt_hash      TEXT,
            UNIQUE (asset_id, score_type, event_time)
        );
        INSERT INTO score_snapshot__new
            (id, asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, run_id, run_kind, filing_id, rating, narrative, strengths_json,
             risks_json, forensic_flags_json, available_at, prompt_hash)
        SELECT id, asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
               model, inputs_json, run_id, run_kind, filing_id, rating, narrative, strengths_json,
               risks_json, forensic_flags_json, available_at, prompt_hash
        FROM score_snapshot;
        DROP TABLE score_snapshot;
        ALTER TABLE score_snapshot__new RENAME TO score_snapshot;
        CREATE INDEX IF NOT EXISTS ix_score_snapshot_type_time
            ON score_snapshot (score_type, event_time);
        """
    )
    if had_compat_view:
        db.create_schema(
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
    db.execute("PRAGMA foreign_keys = ON")


# -- m006: rename score_type 'QUANTITATIVE' -> 'VALORIZATION' -----------------


def _m006_quantitative_to_valorization(db: Database) -> None:
    """The cycle's value/quality/size factor blend was mislabelled 'QUANTITATIVE'.
    Rename it to 'VALORIZATION' everywhere it is persisted: the score_type CHECK,
    the stored rows, and the score-type keys inside the recorded blend JSON.
    """
    if _m006_at_target_shape(db):
        return  # T-154: the CHECK already says 'VALORIZATION' (a fresh DB)
    _ensure_forensic_flags_column(db)
    had_compat_view = _is_view(db, "fundamental_snapshot")
    db.execute("PRAGMA foreign_keys = OFF")
    db.create_schema(
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
            forensic_flags_json TEXT,
            available_at     TEXT,
            prompt_hash      TEXT,
            UNIQUE (asset_id, score_type, event_time)
        );
        INSERT INTO score_snapshot__new
            (id, asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, run_id, run_kind, filing_id, rating, narrative, strengths_json,
             risks_json, forensic_flags_json, available_at, prompt_hash)
        SELECT id, asset_id,
               CASE score_type WHEN 'QUANTITATIVE' THEN 'VALORIZATION' ELSE score_type END,
               raw_value, normalized_score, event_time, computed_at,
               model, inputs_json, run_id, run_kind, filing_id, rating, narrative, strengths_json,
               risks_json, forensic_flags_json, available_at, prompt_hash
        FROM score_snapshot;
        DROP TABLE score_snapshot;
        ALTER TABLE score_snapshot__new RENAME TO score_snapshot;
        CREATE INDEX IF NOT EXISTS ix_score_snapshot_type_time
            ON score_snapshot (score_type, event_time);
        """
    )
    # The recorded blend carries score_type as JSON object keys / component labels.
    if _table_exists(db, "cycle_run"):
        db.execute(
            "UPDATE cycle_run SET params_json = "
            "REPLACE(params_json, '\"QUANTITATIVE\"', '\"VALORIZATION\"') "
            "WHERE params_json LIKE '%\"QUANTITATIVE\"%'"
        )
    if _table_exists(db, "cycle_ranking"):
        db.execute(
            "UPDATE cycle_ranking SET components_json = "
            "REPLACE(components_json, '\"QUANTITATIVE\"', '\"VALORIZATION\"') "
            "WHERE components_json LIKE '%\"QUANTITATIVE\"%'"
        )
    if had_compat_view:
        db.create_schema(
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
    db.execute("PRAGMA foreign_keys = ON")


# -- m007: quant_portfolio's book key made NULL-safe (T-101) ----------------

# Refresh the kept book with the newest copy's metadata: every column but the key and the id.
# Fully literal SQL, never assembled from a column list (constitution Code & Git #10).
_COPY_NEWEST_SQL = """
UPDATE quant_portfolio SET
    (quant_run_id, model_id, objective, solver, status, expected_return, expected_vol, sharpe,
     rf_annual, n_positions, turnover, target_param, computed_at, params_json, manifest_json)
  = (SELECT quant_run_id, model_id, objective, solver, status, expected_return, expected_vol,
            sharpe, rf_annual, n_positions, turnover, target_param, computed_at, params_json,
            manifest_json
     FROM quant_portfolio WHERE id = ?)
WHERE id = ?
"""


def _m007_quant_portfolio_null_safe_key(db: Database) -> None:
    """Merge duplicate books, then enforce the book key with NULL treated as a value.

    ``UNIQUE (as_of, kind, frontier_k, engine_version)`` never matched a non-frontier book,
    whose ``frontier_k`` is NULL, so every identical ``optimize`` / ``evaluate`` re-run inserted
    another copy of each book. The old read-back then returned the **oldest** copy, so
    ``sync_positions`` kept writing the latest weights into it while each newer copy got none.
    Per duplicate group this keeps the oldest id (it has the positions, and it is the id every
    earlier reference used), refreshes it with the newest copy's metadata, moves frontier-point
    references onto it, and drops the newer copies with their positions and forward
    performance rows (both derived; ``quant evaluate`` recomputes performance). Then a unique
    index on ``IFNULL(frontier_k, -1)`` makes the database refuse a duplicate book outright."""
    if _m007_at_target_shape(db):
        return  # T-154: the NULL-safe book key is already enforced -- nothing to merge
    groups = db.execute(
        "SELECT MIN(id) AS keep, MAX(id) AS newest, GROUP_CONCAT(id) AS ids "
        "FROM quant_portfolio GROUP BY as_of, kind, IFNULL(frontier_k, -1), engine_version "
        "HAVING COUNT(*) > 1"
    ).fetchall()
    for group in groups:
        keep, newest = int(group["keep"]), int(group["newest"])
        drop = [int(i) for i in str(group["ids"]).split(",") if int(i) != keep]
        db.execute(_COPY_NEWEST_SQL, (newest, keep))
        for pid in drop:
            if _table_exists(db, "quant_frontier_point"):
                db.execute(
                    "UPDATE quant_frontier_point SET portfolio_id = ? WHERE portfolio_id = ?",
                    (keep, pid),
                )
            if _table_exists(db, "quant_benchmark_performance"):
                db.execute("DELETE FROM quant_benchmark_performance WHERE portfolio_id = ?", (pid,))
            if _table_exists(db, "quant_position"):
                db.execute("DELETE FROM quant_position WHERE portfolio_id = ?", (pid,))
            db.execute("DELETE FROM quant_portfolio WHERE id = ?", (pid,))
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_quant_portfolio_book ON quant_portfolio "
        "(as_of, kind, IFNULL(frontier_k, -1), engine_version)"
    )


# -- m008: available_at backfilled (T-107) ------------------------------------


def _m008_available_at(db: Database) -> None:
    """Give every dated filing, and every metric and FUNDAMENTAL score, its ``available_at``
    (the first NYSE trading day after the filing date), and put the guards in place.

    The columns and triggers are (re)created first: ``ensure()`` adds them before migrating,
    but the m002/m003/m005/m006 rebuilds of an older database drop both. Refuses -- the
    migration rolls back -- if a metric or FUNDAMENTAL score is left without one (its filing
    has no date), since as-of readers would skip it silently."""
    if not _table_exists(db, "sec_filings"):
        return
    for table in ("sec_filings", "fundamental_metrics", "score_snapshot"):
        # a no-op for a table that does not exist
        db.ensure_columns(table, {"available_at": REQUIRED_COLUMNS[table]["available_at"]})
    _availability.ensure_triggers(db)
    _availability.backfill(db)
    gaps = _availability.missing(db)
    if gaps:
        db.rollback()
        detail = ", ".join(f"{t}: {n}" for t, n in gaps.items())
        raise _availability.AvailabilityMissing(
            f"m008: rows left without available_at ({detail}) -- their filing has no "
            "filing_date; fix or remove them first"
        )


# -- m009: veto per-date hit rows collapsed into stints (T-125) ------------------


def _m009_veto_stints(db: Database) -> None:
    """Collapse ``veto``'s per-(asset, rule, cycle_date) hit-event rows into stints
    (``raised_on``/``cleared_on``/``last_seen_on``), so a HARD veto clears the first cycle
    its condition stops holding on an evaluated asset, and a SOFT rule penalizes once per
    open stint rather than once per per-date row it happened to be hit on (the bug this
    migration exists to retroactively undo: a HARD veto was permanent once raised, and a
    SOFT rule held across N cycles counted N times).

    The old table only ever recorded *hits* -- a row exists for a (asset, rule, cycle_date)
    only where the rule actually fired that date; there is no stored record of a date it
    was evaluated and found clean. What *is* recorded, system-wide, is which cycle dates
    the "veto" checkpoint step actually completed on (any ``cycle_run``, any
    ``cycle_type`` -- REPLAY runs write the same shared table). For each (asset_id,
    rule_id) pair this walks its own hit dates union those global evaluation dates in
    order: a stint opens on the first hit date reached with none already open, extends
    (``last_seen_on``) across consecutive hit dates, and closes (``cleared_on``) at the
    first evaluation date reached with no hit row for that pair -- reopening a new, later
    stint if a hit row appears again after that. A pair's last hit, if no evaluation date
    after it shows a miss, stays open, exactly like a currently-active veto should.
    """
    if _m009_at_target_shape(db):
        return  # T-154: veto already has the stint shape -- never replay the rebuild
    # T-154: carry T-070's columns (expires_on / expiry_history_json) through the rebuild; a no-op
    # when they are already there.
    db.ensure_columns("veto", REQUIRED_COLUMNS["veto"])
    migrated_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
    eval_dates = sorted(
        {
            str(r["cycle_date"])
            for r in db.execute(
                "SELECT DISTINCT cr.cycle_date FROM cycle_checkpoint cc "
                "JOIN cycle_run cr ON cr.id = cc.cycle_run_id "
                "WHERE cc.step = 'veto' AND cc.status = 'done'"
            )
        }
    )
    # cleared_at IS NULL: the old writer set cleared_at on a row when a same-date re-run no
    # longer hit that (asset, rule) pair (ON CONFLICT ... cleared_at = NULL on a fresh hit,
    # else left set by the clearing UPDATE) -- such a row's cycle_date was NOT a hit in that
    # date's final verdict, and must not be read as one here (PR #103 review). The date still
    # counts as an evaluation date through cycle_checkpoint regardless, so the pair still
    # closes correctly on it.
    old_rows = db.execute(
        "SELECT asset_id, rule_id, severity, detected_at, cycle_date, evidence_json, run_id, "
        "expires_on, expiry_history_json "
        "FROM veto WHERE cleared_at IS NULL ORDER BY asset_id, rule_id, cycle_date"
    ).fetchall()
    by_pair: dict[tuple[int, str], dict[str, dict]] = {}
    for r in old_rows:
        key = (int(r["asset_id"]), str(r["rule_id"]))
        by_pair.setdefault(key, {})[str(r["cycle_date"])] = dict(r)
    stints: list[dict] = []
    for (asset_id, rule_id), by_date in by_pair.items():
        open_stint: dict[str, object] | None = None
        # A hit before the earliest global eval date (a legacy/manually-inserted row) still
        # opens a stint of its own.
        dates = sorted(set(eval_dates) | set(by_date))
        for d in dates:
            row = by_date.get(d)
            if row is not None:
                if open_stint is None:
                    open_stint = {
                        "asset_id": asset_id,
                        "rule_id": rule_id,
                        "severity": row["severity"],
                        "raised_on": d,
                        "cleared_on": None,
                        "last_seen_on": d,
                        "detected_at": row["detected_at"],
                        "cleared_at": None,
                        "evidence_json": row["evidence_json"],
                        "run_id": row["run_id"],
                        "expires_on": row["expires_on"],
                        "expiry_history_json": row["expiry_history_json"],
                    }
                else:
                    open_stint["last_seen_on"] = d
                    open_stint["severity"] = row["severity"]
                    open_stint["evidence_json"] = row["evidence_json"]
                    open_stint["run_id"] = row["run_id"]
                    open_stint["expires_on"] = row["expires_on"]
                    open_stint["expiry_history_json"] = row["expiry_history_json"]
            elif open_stint is not None and d in eval_dates:
                open_stint["cleared_on"] = d
                open_stint["cleared_at"] = migrated_at
                stints.append(open_stint)
                open_stint = None
        if open_stint is not None:
            stints.append(open_stint)
    db.execute("PRAGMA foreign_keys = OFF")
    db.create_schema(
        """
        DROP VIEW IF EXISTS v_veto;
        CREATE TABLE veto__new (
            id            INTEGER PRIMARY KEY,
            asset_id      INTEGER NOT NULL REFERENCES assets(id),
            rule_id       TEXT NOT NULL REFERENCES rule_catalog(rule_id),
            severity      TEXT NOT NULL,
            raised_on     TEXT NOT NULL,
            cleared_on    TEXT,
            last_seen_on  TEXT NOT NULL,
            detected_at   TEXT NOT NULL,
            cleared_at    TEXT,
            evidence_json TEXT,
            run_id        INTEGER,
            expires_on    TEXT,
            expiry_history_json TEXT,
            UNIQUE (asset_id, rule_id, raised_on)
        );
        DROP TABLE veto;
        ALTER TABLE veto__new RENAME TO veto;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_veto_open ON veto (asset_id, rule_id)
            WHERE cleared_on IS NULL;
        CREATE INDEX IF NOT EXISTS ix_veto_active ON veto (raised_on) WHERE cleared_on IS NULL;
        """
    )
    db.executemany(
        """
        INSERT INTO veto (asset_id, rule_id, severity, raised_on, cleared_on, last_seen_on,
                          detected_at, cleared_at, evidence_json, run_id, expires_on,
                          expiry_history_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                s["asset_id"],
                s["rule_id"],
                s["severity"],
                s["raised_on"],
                s["cleared_on"],
                s["last_seen_on"],
                s["detected_at"],
                s["cleared_at"],
                s["evidence_json"],
                s["run_id"],
                s["expires_on"],
                s["expiry_history_json"],
            )
            for s in stints
        ],
    )
    db.execute("PRAGMA foreign_keys = ON")


# -- m010: the knowledge-graph view contract (T-144) ---------------------------


def _m010_view_contract(db: Database) -> None:
    """No structural change: T-144's contract (new views and appended columns) lives in
    ``kg_schema.views`` and is rebuilt by every ``ensure``. This only raises the floor to 10,
    so the knowledge-graph repo can assert it and ``ensure_views`` can tell a database that
    is ahead of the running code."""


# -- m011: temporal vetoes and the priceobs-v2 columns (T-070) -----------------


def _m011_temporal_vetoes(db: Database) -> None:
    """Additive: ``price_observation``'s five priceobs-v2 columns and ``veto.expires_on`` (plus
    its ``expiry_history_json`` bookkeeping). Spelled out here, not left to ``ensure``, because
    m009 rebuilds ``veto`` without them; a no-op for a table that does not exist. The appended
    ``v_price_observation`` / ``v_veto`` columns come from ``kg_schema.views``, rebuilt by every
    ``ensure``. Every existing row keeps NULL: no veto has an expiry until a temporal rule opens
    one, and no price_observation row has the new fields until pricing_agent writes priceobs-v2."""
    for table in ("price_observation", "veto"):
        db.ensure_columns(table, REQUIRED_COLUMNS[table])


# -- m012: the filing-identity invariant (T-149) -------------------------------


def _m012_filing_identity(db: Database) -> None:
    """No table changes: installs ``ddl.IDENTITY_TRIGGERS`` and refuses a database whose stored
    rows already break them (an asset with filings and no CIK, or an accession on assets of
    different CIKs -- a trigger never checks the rows already there), so a database at contract
    12 is one where the invariant holds for every row. Raises the floor to 12, as m010 did for
    T-144's views."""
    _identity.drop_triggers(db)
    _identity.require(db)
    _identity.ensure_triggers(db)


# -- m013: the filed sign of a stored fact (T-148) --------------------------------------------


def _m013_filed_sign(db: Database) -> None:
    """Additive: ``financial_facts.preferred_sign`` (ID-12). A no-op for a database without the
    table. Every existing row keeps NULL -- there is no in-place backfill: ``financial_facts`` is
    append-only on ``(filing_id, statement, concept, period_key, filing_version)``, so a
    same-accession re-run cannot add the sign to a stored row, and a NULL reads as the displayed
    sign, flagged. The fix takes effect on a database rebuilt with the engine that writes it."""
    if _table_exists(db, "financial_facts"):
        db.ensure_columns("financial_facts", REQUIRED_COLUMNS["financial_facts"])


_DROP_VIEWS_M014 = (
    "DROP VIEW IF EXISTS v_analysis_run",
    "DROP VIEW IF EXISTS v_pricing_run",
    "DROP VIEW IF EXISTS v_quant_run",
    "DROP VIEW IF EXISTS v_cycle_run",
    "DROP VIEW IF EXISTS v_cycle_ranking",
    "DROP VIEW IF EXISTS v_weight_scheme",
    "DROP VIEW IF EXISTS v_weight_component",
    "DROP VIEW IF EXISTS v_cycle_ranking_component",
    "DROP VIEW IF EXISTS v_fundamental_metric",
    "DROP VIEW IF EXISTS v_sec_filing",
    "DROP VIEW IF EXISTS v_sec_filing_section",
    "DROP VIEW IF EXISTS v_data_quality_issue",
)


def _seed_table_sequence(db: Database, table: str) -> None:
    if not _table_exists(db, table):
        return
    max_val = int(db.execute(MAX_ID_QUERIES[table]).fetchone()[0])
    for (src_table, src_col, run_kind), target in RUN_REFERENCE_MAP.items():
        if target != table or not _table_exists(db, src_table):
            continue
        query = MAX_REFERENCE_QUERIES[(src_table, src_col, run_kind)]
        row = db.execute(query).fetchone()
        ref_val = int(row[0]) if row and row[0] is not None else 0
        max_val = max(max_val, ref_val)
    if max_val <= 0:
        return
    row = db.execute(SQLITE_SEQUENCE_QUERIES["select"], (table,)).fetchone()
    if row is None or max_val > int(row[0]):
        db.execute(SQLITE_SEQUENCE_QUERIES["delete"], (table,))
        db.execute(SQLITE_SEQUENCE_QUERIES["insert"], (table, max_val))


def _seed_sqlite_sequence(db: Database) -> None:
    if not _table_exists(db, "sqlite_sequence"):
        return
    for table in TARGET_RUN_TABLES:
        _seed_table_sequence(db, table)


# -- m014: non-reusable IDs with AUTOINCREMENT (T-145) -------------------------


def _rebuild_run_tables(db: Database, tables: list[str]) -> None:  # noqa: C901
    # Pre-flight: verify existing tables have the expected columns before any changes
    for table in tables:
        if table in REQUIRED_COLUMNS:
            db.ensure_columns(table, REQUIRED_COLUMNS[table])
        current_cols = db.table_columns(table)
        expected_cols = M014_REBUILD_COLUMNS[table]
        if set(current_cols) != set(expected_cols):
            raise RuntimeError(
                f"m014: table {table} columns {current_cols} differ from expected {expected_cols}"
            )

    _availability.drop_triggers(db)
    _identity.drop_triggers(db)
    had_compat_view = _is_view(db, "fundamental_snapshot")
    if had_compat_view:
        db.execute("DROP VIEW fundamental_snapshot")
    for stmt in _DROP_VIEWS_M014:
        db.execute(stmt)

    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    fk_status = db.execute("PRAGMA foreign_keys").fetchone()
    fk_val = fk_status[0] if isinstance(fk_status, tuple) else fk_status["foreign_keys"]
    if fk_val != 0:
        raise RuntimeError(f"m014: failed to disable foreign keys (PRAGMA foreign_keys = {fk_val})")

    fk_before_raw = db.execute("PRAGMA foreign_key_check").fetchall()
    fk_before = [tuple(r) for r in fk_before_raw]

    try:
        db.execute("BEGIN TRANSACTION")
        for table in tables:
            for stmt in M014_TABLE_REBUILD_STATEMENTS[table]:
                db.execute(stmt)

        fk_after_raw = db.execute("PRAGMA foreign_key_check").fetchall()
        fk_after = [tuple(r) for r in fk_after_raw]
        if fk_after != fk_before:
            db.execute("ROLLBACK")
            new_violations = [v for v in fk_after if v not in fk_before]
            readable = [f"{v[0]}:{v[1]} (parent {v[2]})" for v in new_violations]
            raise RuntimeError(  # noqa: TRY301
                f"m014: foreign key check failed after rebuild: {readable or fk_after}"
            )
        db.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(Exception):
            db.execute("ROLLBACK")
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")
        if had_compat_view:
            db.create_schema(
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
        from .views import ensure_views  # noqa: PLC0415 - avoid circular import with views.py

        ensure_views(db)
        _availability.ensure_triggers(db)
        _identity.ensure_triggers(db)


def _m014_autoincrement_run_ids(db: Database) -> None:
    """Non-additive: rebuild cycle_run, analysis_run, pricing_run, quant_run, and sec_filings
    with AUTOINCREMENT so deleted IDs are never reused. Seed sqlite_sequence from the maximum
    of each table's own ID and all references pointing to it.
    """
    if not _m014_at_target_shape(db):
        tables_to_rebuild = [
            t
            for t in ("cycle_run", "pricing_run", "quant_run", "analysis_run", "sec_filings")
            if _table_exists(db, t) and "AUTOINCREMENT" not in (db.relation_ddl(t) or "").upper()
        ]
        if tables_to_rebuild:
            _rebuild_run_tables(db, tables_to_rebuild)
    # T-154: the rebuild is skipped when every table already carries AUTOINCREMENT (never replay it
    # on a database that already has the shape); the seed stays -- it changes nothing when there is
    # nothing to seed, and it is what keeps an id from being reused after an explicit-id import.
    _seed_sqlite_sequence(db)


# -- m015: marker for the orchestrator provenance (T-012) ---------------------


def _m015_orchestrator_provenance(db: Database) -> None:
    """No structural change: T-012's tables (``orchestrator_run`` / ``orchestrator_step``) and
    their ``v_*`` views live in ``kg_schema.ddl`` / ``kg_schema.views`` and are created by every
    ``ensure``, so a fresh database has them without migrating. This only raises the floor to 15,
    as m010 did for T-144's views. A marker: replaying it on a database that already has the
    tables changes nothing, so it is safe to run on one that does."""


# -- m016: marker for the as-of ticker history (T-153 part 2) ------------------


def _m016_ticker_history(db: Database) -> None:
    """No structural change: the ``ticker_history`` table (`kg_schema/ddl.py`) and the
    ``v_ticker_history`` view (`kg_schema/views.py`) are created by every ``ensure``, so a fresh
    database has them without migrating. This only raises the floor to 16, as m010/m015 did for
    their views. A marker: replaying it changes nothing, so it is safe on a database that already
    has the table."""


# -- m017: marker for the Tiingo read-contract change (T-153 part 3d) ----------


def _m017_tiingo_read_contract(db: Database) -> None:
    """No structural change: ``quant_return_daily.vendor`` comes from ``REQUIRED_COLUMNS`` on
    every ``ensure``, and the three view filters come from ``views.py`` on every ``ensure``, so a
    fresh database already has them. This only raises the floor to 17 -- the knowledge-graph repo
    asserts it to know the price views drop the private import's rows (m010/m015/m016's pattern).
    The filters and the NULLing are value changes inside the existing column lists; only
    ``vendor`` is appended, at the end, as the contract's versioning rule allows."""
    _ = db


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
    (
        7,
        "quant_portfolio: duplicate books merged, NULL-safe unique book key (T-101)",
        _m007_quant_portfolio_null_safe_key,
    ),
    (
        8,
        "available_at on sec_filings / fundamental_metrics / FUNDAMENTAL scores, backfilled "
        "and guarded (T-107)",
        _m008_available_at,
    ),
    (
        9,
        "veto: per-(asset, rule, cycle_date) hit rows collapsed into raised_on/cleared_on/"
        "last_seen_on stints (T-125)",
        _m009_veto_stints,
    ),
    (
        10,
        "marker: the knowledge-graph view contract -- v_fundamental_metric, "
        "v_cycle_ranking_component and the appended view columns (T-144)",
        _m010_view_contract,
    ),
    (
        11,
        "price_observation: sma_200 / ret_5d / vol_5d / mu_60d_base / vol_60d_base (priceobs-v2); "
        "veto.expires_on (temporal vetoes) (T-070)",
        _m011_temporal_vetoes,
    ),
    (
        12,
        "filing identity: every asset with filings has a CIK, an accession spans only assets "
        "of one CIK -- triggers, checked against the stored rows (T-149)",
        _m012_filing_identity,
    ),
    (
        13,
        "financial_facts.preferred_sign: the filed sign of a stored fact, NULL before it (T-148)",
        _m013_filed_sign,
    ),
    (
        14,
        "non-reusable ids: AUTOINCREMENT on run tables and sec_filings, sqlite_sequence "
        "seeded from references (T-145)",
        _m014_autoincrement_run_ids,
    ),
    (
        15,
        "marker: orchestrator provenance -- orchestrator_run / orchestrator_step and their "
        "v_* views (T-012)",
        _m015_orchestrator_provenance,
    ),
    (
        16,
        "marker: the as-of ticker history -- ticker_history and v_ticker_history (T-153 part 2)",
        _m016_ticker_history,
    ),
    (
        17,
        "marker: the Tiingo read contract -- v_* price views drop the private import's rows, "
        "quant_return_daily.vendor (T-153 part 3d)",
        _m017_tiingo_read_contract,
    ),
]


def apply_migrations(db: Database) -> list[int]:
    """Run every migration whose version exceeds the recorded floor. Returns applied ids."""
    _queries.ensure(db)
    at = _queries.current_version(db)
    pending = [(ver, desc, fn) for ver, desc, fn in MIGRATIONS if ver > at]
    if not pending:
        return []
    # The T-107 guards come off for the rebuilds and go back on afterwards (m008 re-creates
    # them; this covers a later migration too).
    _availability.drop_triggers(db)
    _identity.drop_triggers(db)
    applied: list[int] = []
    try:
        for ver, desc, fn in pending:
            fn(db)
            _queries.record(db, ver, desc)
            db.commit()
            applied.append(ver)
    finally:
        _availability.ensure_triggers(db)
        _identity.ensure_triggers(db)
    return applied
