"""Connection + schema for the cycle package (shares KG_FINANCIAL_DB)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from portfolio_common import kg_schema


def connect(path: str | Path) -> sqlite3.Connection:
    """The shared connection factory (:func:`kg_schema.connect`), re-exposed here
    so ``cycle`` code keeps importing it from ``cycle.db``."""
    return kg_schema.connect(path)


def ensure_schema(conn: sqlite3.Connection) -> None:
    kg_schema.ensure(conn)
