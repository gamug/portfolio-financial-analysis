"""Read-only SQLite access for the API. Nothing here ever writes.

Open connections with :func:`kg_schema.connect_ro` -- import that directly
rather than through this module."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from portfolio_common.db import Database, DatabaseError


def rows(conn: Database, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run *sql* and return a list of plain dicts. On a missing view/table
    (partial DB) returns ``[]`` rather than raising."""
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    except DatabaseError:
        return []
