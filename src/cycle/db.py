"""Connection + schema for the cycle package (shares KG_FINANCIAL_DB)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import kg_schema


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    kg_schema.ensure(conn)
