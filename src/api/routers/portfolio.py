"""``/portfolio`` -- the live book (``v_portfolio_position``), the latest cycle
cohort (``v_cycle_ranking``), and optimized vs live weights (``v_quant_vs_live``). Views passed through verbatim."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from portfolio_common.db import Database

from api.db import rows
from api.dependencies import get_db

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/positions")
def positions(
    as_of: str | None = Query(None, description="position stints open as of this date"),
    open_only: bool = Query(
        True, description="only currently-open stints (valid_to IS NULL); ignored with as_of"
    ),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM v_portfolio_position"
    params: list[object] = []
    if as_of:
        sql += " WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        params += [as_of, as_of]
    elif open_only:
        sql += " WHERE valid_to IS NULL"
    sql += " ORDER BY weight DESC"
    return rows(db, sql, params)


@router.get("/ranking")
def cycle_ranking(
    cycle_type: str = Query("SELECTION", description="SELECTION or MONITORING"),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """The ranked cohort of the most recent completed cycle of *cycle_type*."""
    sql = (
        "SELECT * FROM v_cycle_ranking WHERE cycle_type = ? "
        "AND cycle_date = (SELECT MAX(cycle_date) FROM v_cycle_ranking WHERE cycle_type = ? AND status = 'completed') "
        "ORDER BY rank"
    )
    return rows(db, sql, (cycle_type.upper(), cycle_type.upper()))


@router.get("/quant-vs-live")
def quant_vs_live(
    as_of: str | None = Query(
        None,
        description="newest book date on or before this date (omitted -> newest in view)",
    ),
    kind: str | None = Query(
        None,
        description="filter by kind; LIVE_ONLY rows are always included unless kind=LIVE_ONLY",
    ),
    current_only: bool = Query(
        True,
        description="only current books (is_current = 1); false includes variant books",
    ),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Optimized benchmark weights beside live portfolio positions (``v_quant_vs_live``).

    LIVE_ONLY rows represent positions held by no current optimized book on that
    date. If *kind* is provided, returns that kind's rows plus LIVE_ONLY rows (or
    only LIVE_ONLY rows when kind='LIVE_ONLY').
    """
    if as_of:
        date_rows = rows(
            db,
            "SELECT MAX(as_of) AS target_as_of FROM v_quant_vs_live WHERE as_of <= ?",
            (as_of,),
        )
    else:
        date_rows = rows(
            db,
            "SELECT MAX(as_of) AS target_as_of FROM v_quant_vs_live",
        )
    if not date_rows or not date_rows[0]["target_as_of"]:
        return []

    target_as_of = date_rows[0]["target_as_of"]

    sql = "SELECT * FROM v_quant_vs_live WHERE as_of = ?"
    params: list[object] = [target_as_of]

    if kind:
        if kind.upper() == "LIVE_ONLY":
            sql += " AND kind = 'LIVE_ONLY'"
        else:
            sql += " AND (kind = ? OR kind = 'LIVE_ONLY')"
            params.append(kind)

    if current_only:
        sql += " AND is_current = 1"

    sql += " ORDER BY kind, ticker"
    return rows(db, sql, params)
