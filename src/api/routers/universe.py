"""``/universe`` -- the point-in-time S&P 500 roster as of a date (from
``universe.db``) and its core-data coverage."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Query
from portfolio_common.db import Database

from api.dependencies import get_db, get_universe_db
from api.models import CoverageRow, CoverageSummary, UniverseMemberOut
from kg_schema.queries import check_coverage, members_asof

router = APIRouter(prefix="/universe", tags=["universe"])

_AS_OF = Query(..., description="as-of date, YYYY-MM-DD", examples=["2024-06-30"])


@router.get("", response_model=list[UniverseMemberOut])
def universe_as_of(
    as_of: date = _AS_OF,
    universe: str = Query("SP500"),
    udb: Database = Depends(get_universe_db),
) -> list[UniverseMemberOut]:
    members = members_asof(udb, as_of.isoformat(), universe=universe)
    return [
        UniverseMemberOut(
            symbol=m.symbol,
            security=m.security,
            cik=m.cik,
            gics_sector=m.gics_sector,
            gics_sub_industry=m.gics_sub_industry,
            valid_from=m.valid_from,
            valid_to=m.valid_to,
        )
        for m in members
    ]


@router.get("/coverage", response_model=CoverageSummary)
def universe_coverage(
    as_of: date = _AS_OF,
    universe: str = Query("SP500"),
    db: Database = Depends(get_db),
    udb: Database = Depends(get_universe_db),
) -> CoverageSummary:
    """Persisted ``universe_coverage`` rows for this ``as_of`` if the ``coverage``
    command has been run; otherwise computed live (nothing is written)."""
    d = as_of.isoformat()
    persisted = _persisted_coverage(db, d, universe)
    if persisted is not None:
        return persisted

    report = check_coverage(db, udb, d, universe=universe)
    return CoverageSummary(
        as_of=report.as_of,
        universe=report.universe,
        total=report.total,
        covered=report.covered,
        fraction=report.fraction,
        source="computed",
        rows=[
            CoverageRow(symbol=r.symbol, covered=r.covered, missing=r.missing) for r in report.rows
        ],
    )


def _persisted_coverage(db: Database, as_of: str, universe: str) -> CoverageSummary | None:
    try:
        cur = db.execute(
            "SELECT symbol, covered, missing_json FROM v_universe_coverage "
            "WHERE as_of = ? AND universe = ? ORDER BY symbol",
            (as_of, universe),
        )
    except sqlite3.OperationalError:
        return None
    rows = [
        CoverageRow(
            symbol=str(r["symbol"]),
            covered=bool(r["covered"]),
            missing=list(json.loads(r["missing_json"] or "[]")),
        )
        for r in cur
    ]
    if not rows:
        return None
    covered = sum(1 for r in rows if r.covered)
    return CoverageSummary(
        as_of=as_of,
        universe=universe,
        total=len(rows),
        covered=covered,
        fraction=covered / len(rows),
        source="persisted",
        rows=rows,
    )
