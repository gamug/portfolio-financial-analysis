"""Read-only SQLite access for the API. Nothing here ever writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Open *path* strictly read-only (``file:...?mode=ro``)."""
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run *sql* and return a list of plain dicts. On a missing view/table
    (partial DB) returns ``[]`` rather than raising."""
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    except sqlite3.OperationalError:
        return []
