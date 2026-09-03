"""FastAPI dependencies: settings + per-request read-only connections + paging."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Query, Request

from api.config import ApiSettings
from api.db import connect_ro


def get_settings(request: Request) -> ApiSettings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_db(settings: ApiSettings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    """A read-only connection to ``KG_FINANCIAL_DB`` for one request."""
    conn = connect_ro(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_universe_db(
    settings: ApiSettings = Depends(get_settings),
) -> Iterator[sqlite3.Connection]:
    """A read-only connection to ``universe.db`` for one request."""
    conn = connect_ro(settings.universe_db_path)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int


def page_params(
    limit: int = Query(100, ge=1, le=1000, description="max rows"),
    offset: int = Query(0, ge=0),
) -> Page:
    return Page(limit=limit, offset=offset)
