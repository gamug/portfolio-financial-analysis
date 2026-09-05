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
"""

from __future__ import annotations

from portfolio_common.db import Database

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

    ``table`` / ``column`` / ``sql_type`` below are interpolated from
    ``REQUIRED_COLUMNS`` -- a fixed dict literal in :mod:`kg_schema.ddl`, never
    from caller-supplied input -- into ``PRAGMA table_info({table})`` /
    ``ALTER TABLE {table} ADD COLUMN {column} {sql_type}``. Neither can be
    bound as a `?` parameter (SQLite only parameterizes values, never
    identifiers/type names), and there is no caller-supplied name reaching
    this SQL, so this is not an injection surface -- no Allowlist needed.
    """
    for table, columns in REQUIRED_COLUMNS.items():
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, sql_type in columns.items():
            if column not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    db.commit()


def ensure(db: Database, *, run_migrations: bool = False) -> list[int]:
    """Bring *db*'s database up to the shared schema. Returns applied migration ids."""
    _queries.ensure(db)
    db.executescript(ADDITIVE_DDL)
    db.commit()
    _add_missing_columns(db)
    ensure_views(db)
    if run_migrations:
        applied = apply_migrations(db)
        ensure_views(db)  # rebuilds may have changed base tables the views read
        return applied
    return []
