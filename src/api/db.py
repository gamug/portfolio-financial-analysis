"""Read-only SQLite access for the API. Nothing here ever writes.

Open connections with :func:`portfolio_common.kg_schema.connect_ro` -- import
that directly rather than through this module."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any


def rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run *sql* and return a list of plain dicts. On a missing view/table
    (partial DB) returns ``[]`` rather than raising."""
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    except sqlite3.OperationalError:
        return []
