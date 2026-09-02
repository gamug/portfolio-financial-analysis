"""Passive schema shared across the analysis workstream repos.

This package has no behaviour of its own -- it only owns DDL, migrations, the
``schema_version`` floor, and the read-contract VIEWs consumed by the integration
repo. Both agents call :func:`ensure` from their own ``ensure_schema`` after creating
their private tables.

``ensure(conn)``                 additive only -- new tables, new nullable columns,
                                 refreshed views. Safe to run anytime, concurrently
                                 with the other repos. Never advances schema_version.
``ensure(conn, run_migrations=True)``  additionally runs the non-additive rebuilds in
                                 :mod:`kg_schema.migrations`. Only ``python -m <agent>
                                 migrate`` passes this.
"""

from __future__ import annotations

import sqlite3

from kg_schema import version as _version
from kg_schema.ddl import ADDITIVE_DDL, REQUIRED_COLUMNS
from kg_schema.env import (
    DB_ENV_VAR,
    LEGACY_DB_ENV_VAR,
    UNIVERSE_DB_ENV_VAR,
    database_path,
    universe_database_path,
)
from kg_schema.migrations import apply_migrations
from kg_schema.provenance import code_version
from kg_schema.views import ensure_views

__all__ = [
    "DB_ENV_VAR",
    "LEGACY_DB_ENV_VAR",
    "UNIVERSE_DB_ENV_VAR",
    "apply_migrations",
    "code_version",
    "database_path",
    "ensure",
    "universe_database_path",
]


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in REQUIRED_COLUMNS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, sql_type in columns.items():
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    conn.commit()


def ensure(conn: sqlite3.Connection, *, run_migrations: bool = False) -> list[int]:
    """Bring *conn*'s database up to the shared schema. Returns applied migration ids."""
    _version.ensure(conn)
    conn.executescript(ADDITIVE_DDL)
    conn.commit()
    _add_missing_columns(conn)
    ensure_views(conn)
    if run_migrations:
        applied = apply_migrations(conn)
        ensure_views(conn)  # rebuilds may have changed base tables the views read
        return applied
    return []
