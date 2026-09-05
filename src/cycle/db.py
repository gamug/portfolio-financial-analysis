"""Schema for the cycle package (shares KG_FINANCIAL_DB).

The connection factory lives at :func:`kg_schema.connect` -- import it from
there directly rather than through this module."""

from __future__ import annotations

from portfolio_common.db import Database

import kg_schema


def ensure_schema(conn: Database) -> None:
    kg_schema.ensure(conn)
