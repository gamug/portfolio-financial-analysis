"""Response models. Endpoints that pass a ``v_*`` view through verbatim return
plain dicts (the view is the contract); the rest get a concrete model here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

RunKind = Literal["analysis", "pricing", "quant", "cycle"]


class Health(BaseModel):
    status: str
    version: str


class DbHealth(BaseModel):
    ok: bool
    db_path: str
    schema_version: int | None
    universe_db_ok: bool


class RunRow(BaseModel):
    run_id: int
    kind: RunKind
    as_of: str | None = None
    code_version: str | None = None
    status: str
    started_at: str | None = None
    finished_at: str | None = None


class UniverseMemberOut(BaseModel):
    symbol: str
    security: str
    cik: str | None = None
    gics_sector: str | None = None
    gics_sub_industry: str | None = None
    valid_from: str
    valid_to: str | None = None


class CoverageRow(BaseModel):
    symbol: str
    covered: bool
    missing: list[str]


class CoverageSummary(BaseModel):
    as_of: str
    universe: str
    total: int
    covered: int
    fraction: float
    source: Literal["persisted", "computed"]
    rows: list[CoverageRow]
