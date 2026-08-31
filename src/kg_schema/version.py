"""The ``schema_version`` table: a monotonic floor other repos can assert against.

Additive DDL (new tables, new nullable columns) never bumps the version -- it is
safe to ship at any time. Only the non-additive rebuilds in
:mod:`kg_schema.migrations` advance it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def ensure(conn: sqlite3.Connection) -> None:
    """Create the ``schema_version`` table if it is missing."""
    conn.executescript(VERSION_DDL)
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    """Highest recorded schema version, or ``0`` when nothing has been applied."""
    ensure(conn)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    value = row["v"] if isinstance(row, sqlite3.Row) else (row[0] if row else None)
    return int(value) if value is not None else 0


def record(conn: sqlite3.Connection, version: int, description: str) -> None:
    """Mark *version* as applied. Idempotent -- a re-recorded version is ignored."""
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
        (version, _now(), description),
    )
    conn.commit()
