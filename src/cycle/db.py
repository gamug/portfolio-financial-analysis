"""Schema for the cycle package (shares KG_FINANCIAL_DB).

The connection factory lives at :func:`portfolio_common.kg_schema.connect` --
import it from there directly rather than through this module."""

from __future__ import annotations

import sqlite3

from portfolio_common import kg_schema


def ensure_schema(conn: sqlite3.Connection) -> None:
    kg_schema.ensure(conn)
