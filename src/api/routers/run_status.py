"""``GET /run/{handle}`` -- the run status (T-011), a *read* router.

It reads the orchestrator's in-memory run state (until T-012 moves it to the
orchestrator's provenance view) and never starts a run, opens no debug connection, and
never imports ``api.routers.run`` -- it may import ``api.orchestrator`` (the state it
reads) and nothing here triggers a run. Unknown handle -> ``404``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.orchestrator import RunState, get_run

router = APIRouter(prefix="/run", tags=["run-status"])


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


def _to_status(run: RunState) -> RunStatus:
    return RunStatus(
        handle=run.handle,
        kind=run.kind,
        analysis_date=run.analysis_date,
        settings=run.settings,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        steps=[
            StepStatus(
                name=step.name,
                argv=step.argv,
                status=step.status,
                started_at=step.started_at,
                finished_at=step.finished_at,
                exit_code=step.exit_code,
                run_id=step.run_id,
                reason=step.reason,
                output_tail=step.output_tail,
            )
            for step in run.steps
        ],
    )


@router.get("/{handle}", response_model=RunStatus)
def get_run_status(handle: str) -> RunStatus:
    run = get_run(handle)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run handle: {handle}")
    return _to_status(run)
