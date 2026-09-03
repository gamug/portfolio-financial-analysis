"""``/portfolio`` -- the live book (``v_portfolio_position``) and the latest cycle
cohort (``v_cycle_ranking``). Views passed through verbatim."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.db import rows
from api.dependencies import get_db

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/positions")
def positions(
    as_of: str | None = Query(None, description="position stints open as of this date"),
    open_only: bool = Query(True, description="only currently-open stints (valid_to IS NULL)"),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM v_portfolio_position"
    params: list[object] = []
    if as_of and not open_only:
        sql += " WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        params += [as_of, as_of]
    elif open_only:
        sql += " WHERE valid_to IS NULL"
    sql += " ORDER BY weight DESC"
    return rows(db, sql, params)


@router.get("/ranking")
def cycle_ranking(
    cycle_type: str = Query("SELECTION", description="SELECTION or MONITORING"),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """The ranked cohort of the most recent cycle of *cycle_type*."""
    sql = (
        "SELECT * FROM v_cycle_ranking WHERE cycle_type = ? "
        "AND cycle_date = (SELECT MAX(cycle_date) FROM v_cycle_ranking WHERE cycle_type = ?) "
        "ORDER BY rank"
    )
    return rows(db, sql, (cycle_type.upper(), cycle_type.upper()))
