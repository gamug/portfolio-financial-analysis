"""The ``v_*`` view contract, built from the code (T-152).

The columns of every view in :data:`kg_schema.views.VIEWS` are read from an in-memory
database in which the views have been created -- the way the tests build them -- never from a
second, hand-kept list. The build is a pure function of the code, so it runs once per process.

The agents' base tables come from ``fundamental_agent`` and ``pricing_agent``. ``quant`` is a
leaf package no other package may import (NR-002, ``tests/test_quant_import_isolation.py``), so
the one table of its own that a view reads, ``quant_run``, is declared here;
``tests/test_api_contract.py`` fails if that declaration drifts from ``quant.db.SCHEMA``.
"""

from __future__ import annotations

from functools import cache

import kg_schema
from api.models import ColumnContract, Contract, ViewContract
from fundamental_agent import db as fundamental_db
from kg_schema.migrations import MIGRATIONS
from kg_schema.provenance import code_version
from kg_schema.views import FROZEN_VIEWS, VIEWS
from pricing_agent import db as pricing_db

QUANT_RUN_DDL = """
CREATE TABLE IF NOT EXISTS quant_run (
    id             INTEGER PRIMARY KEY,
    command        TEXT NOT NULL,
    as_of          TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    params_json    TEXT,
    error          TEXT,
    code_version   TEXT
);
"""


def contract_version() -> int:
    """The highest migration version this code knows."""
    return max(ver for ver, _, _ in MIGRATIONS)


def _view_columns() -> dict[str, list[str]]:
    db = kg_schema.connect(":memory:", create_parents=False)
    try:
        fundamental_db.ensure_schema(db)
        pricing_db.ensure_schema(db)
        db.create_schema(QUANT_RUN_DDL)
        kg_schema.ensure(db, run_migrations=True)
        # A view whose base table is absent is dropped by ensure_views, so check for each one
        # rather than publish a contract that silently lacks it.
        absent = [name for name in VIEWS if db.relation_kind(name) != "view"]
        if absent:
            raise RuntimeError(f"the in-memory build did not create: {', '.join(absent)}")
        return {name: db.table_columns(name) for name in VIEWS}
    finally:
        db.close()


@cache
def build_contract() -> Contract:
    """Every view of ``VIEWS`` in order with its ``frozen`` flag and columns in order."""
    columns = _view_columns()
    return Contract(
        contract_version=contract_version(),
        code_version=code_version(),
        views=[
            ViewContract(
                name=name,
                frozen=name in FROZEN_VIEWS,
                columns=[ColumnContract(name=c) for c in columns[name]],
            )
            for name in VIEWS
        ],
    )
