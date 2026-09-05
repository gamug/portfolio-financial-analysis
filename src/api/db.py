"""Read-only SQLite access for the API. Nothing here ever writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from portfolio_common.db import Database

import kg_schema


def connect_ro(path: str | Path) -> Database:
    """Open *path* strictly read-only (``file:...?mode=ro``).

    The shared read-only factory (:func:`kg_schema.connect_ro`), re-exposed here
    so the API's dependencies keep importing it from ``api.db``."""
    return kg_schema.connect_ro(path)


def rows(conn: Database, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run *sql* and return a list of plain dicts. On a missing view/table
    (partial DB) returns ``[]`` rather than raising."""
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    except sqlite3.OperationalError:
        return []
