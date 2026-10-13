"""``/runs`` -- the per-agent run log (``v_analysis_run`` / ``v_pricing_run`` /
``v_quant_run`` / ``v_cycle_run``) plus the orchestrator's provenance (``v_orchestrator_run``,
T-012), so callers can trace an old run by ``as_of`` + ``code_version``."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from portfolio_common.db import Database, DatabaseError

from api.dependencies import Page, get_db, page_params
from api.models import RunKind, RunRow

router = APIRouter(tags=["runs"])

# Static, literal SQL per kind (constitution Code & Git #10): the view name is never built at
# runtime. The optional ``status`` filter below appends a fixed literal, never an identifier.
_RUN_SQL: dict[RunKind, str] = {
    "analysis": (
        "SELECT run_id, as_of, code_version, status, started_at, finished_at FROM v_analysis_run"
    ),
    "pricing": (
        "SELECT run_id, as_of, code_version, status, started_at, finished_at FROM v_pricing_run"
    ),
    "quant": (
        "SELECT run_id, as_of, code_version, status, started_at, finished_at FROM v_quant_run"
    ),
    "cycle": (
        "SELECT run_id, as_of, code_version, status, started_at, finished_at FROM v_cycle_run"
    ),
    "orchestrator": (
        "SELECT run_id, as_of, code_version, status, started_at, finished_at "
        "FROM v_orchestrator_run"
    ),
}


@router.get("/runs", response_model=list[RunRow])
def list_runs(
    kind: RunKind | None = Query(None, description="one agent, or all when omitted"),
    status: str | None = Query(None, description="e.g. completed / running / failed"),
    page: Page = Depends(page_params),
    db: Database = Depends(get_db),
) -> list[RunRow]:
    kinds = [kind] if kind else list(_RUN_SQL)
    out: list[RunRow] = []
    for k in kinds:
        sql = _RUN_SQL[k]
        params: list[object] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        try:
            cur = db.execute(sql, params)
        except DatabaseError:
            continue  # view absent in this DB
        for r in cur:
            out.append(
                RunRow(
                    run_id=int(r["run_id"]),
                    kind=k,
                    as_of=r["as_of"],
                    code_version=r["code_version"],
                    status=str(r["status"]),
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                )
            )
    out.sort(key=lambda x: (x.started_at or "", x.run_id), reverse=True)
    return out[page.offset : page.offset + page.limit]
