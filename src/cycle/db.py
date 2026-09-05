"""Connection + schema for the cycle package (shares KG_FINANCIAL_DB)."""

from __future__ import annotations

from pathlib import Path

from portfolio_common.db import Database

import kg_schema


def connect(path: str | Path) -> Database:
    """The shared connection factory (:func:`kg_schema.connect`), re-exposed here
    so ``cycle`` code keeps importing it from ``cycle.db``."""
    return kg_schema.connect(path)


def ensure_schema(conn: Database) -> None:
    kg_schema.ensure(conn)
