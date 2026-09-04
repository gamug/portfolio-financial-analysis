"""Read-only SQLite access for the API. Nothing here ever writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import kg_schema


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Open *path* strictly read-only (``file:...?mode=ro``).

    The shared read-only factory (:func:`kg_schema.connect_ro`), re-exposed here
    so the API's dependencies keep importing it from ``api.db``."""
    return kg_schema.connect_ro(path)


def rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run *sql* and return a list of plain dicts. On a missing view/table
    (partial DB) returns ``[]`` rather than raising."""
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    except sqlite3.OperationalError:
        return []
