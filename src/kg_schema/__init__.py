"""Passive schema shared across the analysis workstream repos.

This package has no behaviour of its own -- it only owns DDL, migrations, the
``schema_version`` floor, and the read-contract VIEWs consumed by the integration
repo. Both agents call :func:`ensure` from their own ``ensure_schema`` after creating
their private tables.

``ensure(db)``                 additive only -- new tables, new nullable columns,
                                 refreshed views. Safe to run anytime, concurrently
                                 with the other repos. Never advances schema_version.
``ensure(db, run_migrations=True)``  additionally runs the non-additive rebuilds in
                                 :mod:`kg_schema.migrations`. Only ``python -m <agent>
                                 migrate`` passes this.

Engine coupling: connections, catalog lookups and multi-statement DDL run
through ``portfolio_common.db`` (``connect`` / ``relation_exists`` /
``table_columns`` / ``ensure_columns`` / ``create_schema``); nothing here
``import sqlite3``. What is still SQLite-flavoured -- and held only as SQL
text passed to ``db.execute`` -- is the DDL dialect (``INTEGER PRIMARY KEY``
rowid aliases, partial indexes, ``CHECK ... IN``), the ``INSERT OR IGNORE`` /
``ON CONFLICT ... DO UPDATE`` writes in each agent's ``db.py``, and the
``json_extract`` / ``json_each`` read-contract VIEWs in :mod:`kg_schema.views`.
The seam for a future engine is ``conn.dialect`` (``insert_or_ignore`` /
``upsert(update=/do_nothing=)`` / ``json_extract`` / ``json_each``); the
migration rebuild scripts and the VIEW layer would be rewritten fresh for a
non-SQLite backend rather than translated.
"""

from __future__ import annotations

from portfolio_common.db import Database

from . import availability as _availability
from . import queries as _queries
from .db import connect, connect_ro
from .ddl import ADDITIVE_DDL, REQUIRED_COLUMNS
from .env import (
    DB_ENV_VAR,
    LEGACY_DB_ENV_VAR,
    UNIVERSE_DB_ENV_VAR,
    database_path,
    universe_database_path,
)
from .migrations import apply_migrations
from .provenance import code_version
from .views import ensure_views

__all__ = [
    "DB_ENV_VAR",
    "LEGACY_DB_ENV_VAR",
    "UNIVERSE_DB_ENV_VAR",
    "apply_migrations",
    "code_version",
    "connect",
    "connect_ro",
    "database_path",
    "ensure",
    "universe_database_path",
]


def _add_missing_columns(db: Database) -> None:
    """Add REQUIRED_COLUMNS' nullable columns to tables that already exist.

    Both the "is the table there" check and the per-column ``ADD COLUMN`` go
    through :meth:`Database.ensure_columns` (a no-op when the table is
    absent, adds only the columns not already present). ``REQUIRED_COLUMNS``
    is a fixed dict literal in :mod:`kg_schema.ddl` -- no caller input
    reaches this.
    """
    for table, columns in REQUIRED_COLUMNS.items():
        db.ensure_columns(table, columns)
    db.commit()


def _ensure_veto_indexes(db: Database) -> None:
    """T-125: ``veto``'s two stint indexes (``ux_veto_open``, the partial-unique one open
    index enforces; ``ix_veto_active``) name its new ``raised_on``/``cleared_on`` columns, so
    they cannot live inline in ``ADDITIVE_DDL`` the way a plain new table can -- on a
    pre-T-125 database, ``CREATE TABLE IF NOT EXISTS veto`` safely no-ops against the old,
    still-per-cycle-date shape, but an unconditional ``CREATE INDEX`` naming a column that
    shape doesn't have would abort. Skipped until the columns actually exist (a fresh
    database, or one that has already run ``migrate``'s ``m009``, which creates them itself).
    """
    if "raised_on" not in set(db.table_columns("veto")):
        return
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_veto_open ON veto (asset_id, rule_id) "
        "WHERE cleared_on IS NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS ix_veto_active ON veto (raised_on) WHERE cleared_on IS NULL"
    )
    db.commit()


def ensure(db: Database, *, run_migrations: bool = False) -> list[int]:
    """Bring *db*'s database up to the shared schema. Returns applied migration ids."""
    _queries.ensure(db)
    db.create_schema(ADDITIVE_DDL)
    db.commit()
    _add_missing_columns(db)
    _ensure_veto_indexes(db)
    _availability.ensure_triggers(db)
    ensure_views(db)
    if run_migrations:
        applied = apply_migrations(db)
        _ensure_veto_indexes(db)  # m009 already creates these; idempotent/defensive here too
        ensure_views(db)  # rebuilds may have changed base tables the views read
        return applied
    return []
