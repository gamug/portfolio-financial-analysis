"""``/contract`` -- the ``v_*`` view contract, as metadata (never rows): what the running code
defines, and what the connected database has of it."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from portfolio_common.db import Database

from api.contract import build_contract
from api.dependencies import get_db
from api.models import Contract, DatabaseContract
from kg_schema.views import VIEWS, schema_version

router = APIRouter(tags=["contract"])


@router.get("/contract", response_model=Contract)
def contract() -> Contract:
    return build_contract()


@router.get("/contract/database", response_model=DatabaseContract)
def contract_database(db: Database = Depends(get_db)) -> DatabaseContract:
    present = [name for name in VIEWS if db.relation_kind(name) == "view"]
    return DatabaseContract(
        schema_version=schema_version(db),
        views_present=present,
        views_missing=[name for name in VIEWS if name not in present],
    )
