"""``/health`` -- liveness + a read probe of both databases."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from portfolio_common.db import Database, DatabaseError

from api import __version__
from api.config import ApiSettings
from api.dependencies import get_db, get_settings, get_universe_db
from api.models import DbHealth, Health

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", version=__version__)


@router.get("/health/db", response_model=DbHealth)
def health_db(
    settings: ApiSettings = Depends(get_settings),
    db: Database = Depends(get_db),
    udb: Database = Depends(get_universe_db),
) -> DbHealth:
    schema_version: int | None = None
    ok = False
    try:
        row = db.execute("SELECT MAX(version) FROM schema_version").fetchone()
        schema_version = int(row[0]) if row and row[0] is not None else None
        ok = True
    except DatabaseError:
        ok = False
    try:
        udb.execute("SELECT 1 FROM universe_membership LIMIT 1")
        universe_ok = True
    except DatabaseError:
        universe_ok = False
    return DbHealth(
        ok=ok,
        db_path=str(settings.db_path),
        schema_version=schema_version,
        universe_db_ok=universe_ok,
    )
