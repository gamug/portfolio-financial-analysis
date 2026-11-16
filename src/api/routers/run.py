"""``/run`` -- the run endpoints (T-011): the orchestrator and each of its steps.

Every route returns ``202`` with a run handle immediately; the run continues in a
background thread and is polled at ``GET /api/v1/run/<handle>`` (``run_status.py``).
This is a *run* router: it may start a run. It opens no database itself -- the
orchestrator records the run's own provenance (T-012) and each step is a subprocess (see
``api.orchestrator``), so the child's own guards and its own write connection stay exactly
as on the CLI.

The request contract (FR-014, T-011): ``analysis_date`` on all; ``top_n`` /
``weight_scheme`` / ``max_name_weight`` / ``max_sector_weight`` only where the step
understands them; ``extra="forbid"`` so a preference flag, an ``--allow-*`` override, or
a database path / universe / ticker field is rejected with ``422``. The database paths
come from ``ApiSettings``, never the request (D7). No route exists for ``migrate``,
``cycle backfill``, ``cycle undo-run`` or ``coverage``.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.orchestrator import (
    WEIGHT_SCHEMES,
    ProvenanceError,
    RunContext,
    RunInProgress,
    RunLocked,
    RunPaths,
    start_run,
)

router = APIRouter(prefix="/run", tags=["run"])

_STATUS_URL = "/api/v1/run/{handle}"


class _AnalysisDate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_date: date


class DateOnlyRequest(_AnalysisDate):
    """Steps whose only input is the analysis date."""


class CapsRequest(_AnalysisDate):
    """Steps that size the book: ``top_n`` and the two weight caps (no scheme)."""

    top_n: int | None = Field(None, ge=1)
    max_name_weight: float | None = Field(None, gt=0, le=1)
    max_sector_weight: float | None = Field(None, gt=0, le=1)


class ConstructionRequest(CapsRequest):
    """The orchestrator and ``cycle select``: the caps plus a weight scheme."""

    weight_scheme: str | None = None

    @field_validator("weight_scheme")
    @classmethod
    def _known_scheme(cls, value: str | None) -> str | None:
        if value is not None and value not in WEIGHT_SCHEMES:
            raise ValueError(f"weight_scheme must be one of {', '.join(WEIGHT_SCHEMES)}")
        return value


def _context(request: Request, body: _AnalysisDate) -> RunContext:
    settings = request.app.state.settings
    return RunContext(
        paths=RunPaths(
            db_path=settings.db_path,
            universe_db_path=settings.universe_db_path,
            news_db_path=settings.news_db_path,
        ),
        analysis_date=body.analysis_date.isoformat(),
        top_n=getattr(body, "top_n", None),
        weight_scheme=getattr(body, "weight_scheme", None),
        max_name_weight=getattr(body, "max_name_weight", None),
        max_sector_weight=getattr(body, "max_sector_weight", None),
    )


def _start(request: Request, kind: str, body: _AnalysisDate) -> JSONResponse:
    try:
        run = start_run(kind, _context(request, body))
    except RunInProgress as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "a run is already in progress",
                "handle": exc.handle,
                "status_url": _STATUS_URL.format(handle=exc.handle),
            },
        )
    except RunLocked as exc:
        # another process, or a step it started that is still running, holds the run lock (T-015)
        return JSONResponse(
            status_code=409,
            content={"detail": RunLocked.MESSAGE, "holder": exc.holder},
        )
    except ProvenanceError as exc:
        # the run's provenance row could not be written, so the run did not start (T-012)
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    return JSONResponse(
        status_code=202,
        content={"handle": run.handle, "status_url": _STATUS_URL.format(handle=run.handle)},
    )


@router.post("/pipeline", status_code=202)
def start_pipeline(request: Request, body: ConstructionRequest) -> JSONResponse:
    return _start(request, "pipeline", body)


@router.post("/pricing-agent", status_code=202)
def start_pricing_agent(request: Request, body: DateOnlyRequest) -> JSONResponse:
    return _start(request, "pricing-agent", body)


@router.post("/fundamental-agent", status_code=202)
def start_fundamental_agent(request: Request, body: DateOnlyRequest) -> JSONResponse:
    return _start(request, "fundamental-agent", body)


@router.post("/entity-resolution", status_code=202)
def start_entity_resolution(request: Request, body: DateOnlyRequest) -> JSONResponse:
    return _start(request, "entity-resolution", body)


@router.post("/cycle/select", status_code=202)
def start_cycle_select(request: Request, body: ConstructionRequest) -> JSONResponse:
    return _start(request, "cycle-select", body)


@router.post("/cycle/monitor", status_code=202)
def start_cycle_monitor(request: Request, body: DateOnlyRequest) -> JSONResponse:
    return _start(request, "cycle-monitor", body)


@router.post("/quant/backfill-actions", status_code=202)
def start_quant_backfill_actions(request: Request, body: DateOnlyRequest) -> JSONResponse:
    return _start(request, "quant-backfill-actions", body)


@router.post("/quant/build-returns", status_code=202)
def start_quant_build_returns(request: Request, body: DateOnlyRequest) -> JSONResponse:
    return _start(request, "quant-build-returns", body)


@router.post("/quant/build-risk-model", status_code=202)
def start_quant_build_risk_model(request: Request, body: CapsRequest) -> JSONResponse:
    return _start(request, "quant-build-risk-model", body)


@router.post("/quant/optimize", status_code=202)
def start_quant_optimize(request: Request, body: CapsRequest) -> JSONResponse:
    return _start(request, "quant-optimize", body)


@router.post("/quant/evaluate", status_code=202)
def start_quant_evaluate(request: Request, body: DateOnlyRequest) -> JSONResponse:
    return _start(request, "quant-evaluate", body)
