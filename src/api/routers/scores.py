"""``/scores`` -- ``v_score_snapshot`` rows (FUNDAMENTAL / TECHNICAL / VALORIZATION
/ SECTOR / SEMANTIC), filtered by ticker / type / as-of. Passed through verbatim:
the view is the contract."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from portfolio_common.db import Database

from api.db import rows
from api.dependencies import Page, get_db, page_params

router = APIRouter(tags=["scores"])


@router.get("/scores")
def list_scores(
    ticker: str | None = Query(None),
    score_type: str | None = Query(
        None, description="FUNDAMENTAL / TECHNICAL / VALORIZATION / ..."
    ),
    as_of: str | None = Query(None, description="only rows with event_time <= this date"),
    page: Page = Depends(page_params),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM v_score_snapshot"
    clauses: list[str] = []
    params: list[object] = []
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker.upper())
    if score_type:
        clauses.append("score_type = ?")
        params.append(score_type.upper())
    if as_of:
        clauses.append("event_time <= ?")
        params.append(as_of)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY event_time DESC, ticker LIMIT ? OFFSET ?"
    params += [page.limit, page.offset]
    return rows(db, sql, params)
