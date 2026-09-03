"""``/runs`` -- the per-agent run log (``v_analysis_run`` / ``v_pricing_run`` /
``v_quant_run`` / ``v_cycle_run``), so callers can trace an old run by ``as_of`` +
``code_version``."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from api.dependencies import Page, get_db, page_params
from api.models import RunKind, RunRow

router = APIRouter(tags=["runs"])

_VIEW: dict[RunKind, str] = {
    "analysis": "v_analysis_run",
    "pricing": "v_pricing_run",
    "quant": "v_quant_run",
    "cycle": "v_cycle_run",
}


@router.get("/runs", response_model=list[RunRow])
def list_runs(
    kind: RunKind | None = Query(None, description="one agent, or all when omitted"),
    status: str | None = Query(None, description="e.g. completed / running / failed"),
    page: Page = Depends(page_params),
    db: sqlite3.Connection = Depends(get_db),
) -> list[RunRow]:
    kinds = [kind] if kind else list(_VIEW)
    out: list[RunRow] = []
    for k in kinds:
        sql = f"SELECT run_id, as_of, code_version, status, started_at, finished_at FROM {_VIEW[k]}"  # noqa: S608
        params: list[object] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        try:
            cur = db.execute(sql, params)
        except sqlite3.OperationalError:
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
