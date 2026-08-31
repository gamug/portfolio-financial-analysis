"""Shared ``migrate`` implementation for both agents' command lines.

Runs the additive schema *and* the non-additive rebuilds in
:mod:`kg_schema.migrations`. This is the only entrypoint that advances
``schema_version``; quiesce the other repos before running it against the shared DB.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import kg_schema


def resolve_db_path(explicit: str | None) -> Path:
    path = explicit or os.environ.get("KG_FINANTIAL_DB")
    if not path:
        raise RuntimeError("no database: pass --db or set KG_FINANTIAL_DB")
    return Path(path).expanduser()


def run_migrate(db_path: str | None) -> int:
    """Apply pending migrations to *db_path*; print the resulting version table."""
    path = resolve_db_path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        applied = kg_schema.ensure(conn, run_migrations=True)
        rows = conn.execute(
            "SELECT version, applied_at, description FROM schema_version ORDER BY version"
        ).fetchall()
    finally:
        conn.close()

    if applied:
        print(f"applied migrations: {', '.join(map(str, applied))}")
    else:
        print("schema already up to date")
    for r in rows:
        print(f"  v{r['version']}  {r['applied_at']}  {r['description']}")
    return 0
