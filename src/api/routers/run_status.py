"""``GET /run/{handle}`` -- the run status (T-011/T-012), a *read* router.

It reads the orchestrator's recorded provenance (``v_orchestrator_run`` /
``v_orchestrator_step``, T-012) through the read-only ``get_db`` dependency, so a status
still answers after the API restarts. It never starts a run and never imports
``api.orchestrator`` (the read/run split). An unknown handle, or a database without the
provenance views, is ``404``; a missing database file is the ``get_db`` dependency's own
error, the same as every other read route.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from portfolio_common.db import Database, DatabaseError
from pydantic import BaseModel

from api.dependencies import get_db

router = APIRouter(prefix="/run", tags=["run-status"])

_RUN_SQL = (
    "SELECT run_id, kind, as_of, settings_json, status, started_at, finished_at "
    "FROM v_orchestrator_run WHERE handle = ?"
)
_STEPS_SQL = (
    "SELECT step, argv_json, status, started_at, finished_at, exit_code, run_id, reason, "
    "output_tail FROM v_orchestrator_step WHERE orchestrator_run_id = ? ORDER BY ordinal"
)


class StepStatus(BaseModel):
    name: str
    argv: list[str]
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    run_id: int | None = None
    reason: str | None = None
    output_tail: list[str]


class RunStatus(BaseModel):
    handle: str
    kind: str
    analysis_date: str
    settings: dict[str, Any]
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    steps: list[StepStatus]


def _json_dict(text: object) -> dict[str, Any]:
    """Decode a JSON object column (``settings_json``); NULL/empty is an empty mapping."""
    return json.loads(str(text)) if text else {}


def _json_list(text: object) -> list[str]:
    """Decode a JSON array column (``argv_json``, ``output_tail``); NULL is an empty list."""
    return json.loads(str(text)) if text else []


@router.get("/{handle}", response_model=RunStatus)
def get_run_status(handle: str, db: Database = Depends(get_db)) -> RunStatus:
    try:
        row = db.execute(_RUN_SQL, (handle,)).fetchone()
        steps = db.execute(_STEPS_SQL, (row["run_id"],)).fetchall() if row is not None else []
    except DatabaseError:
        # a database with no provenance views (a partial / pre-T-012 DB) has no such run
        raise HTTPException(status_code=404, detail=f"unknown run handle: {handle}") from None
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown run handle: {handle}")
    return RunStatus(
        handle=handle,
        kind=str(row["kind"]),
        analysis_date=str(row["as_of"]),
        settings=_json_dict(row["settings_json"]),
        status=str(row["status"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        steps=[
            StepStatus(
                name=str(step["step"]),
                argv=_json_list(step["argv_json"]),
                status=str(step["status"]),
                started_at=step["started_at"],
                finished_at=step["finished_at"],
                exit_code=step["exit_code"],
                run_id=step["run_id"],
                reason=step["reason"],
                output_tail=_json_list(step["output_tail"]),
            )
            for step in steps
        ],
    )
