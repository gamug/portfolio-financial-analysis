"""quant.db.ensure_schema is idempotent and adds no schema_version bump."""

from __future__ import annotations

from portfolio_common.db import Database

from kg_schema import queries
from quant import db as quant_db


def test_ensure_schema_idempotent_and_creates_tables(memory_quant_db: Database) -> None:
    conn = memory_quant_db
    quant_db.ensure_schema(conn)  # second call must not raise

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "quant_run" in tables
    assert {
        "corporate_action",
        "quant_return_daily",
        "risk_free_rate",
        "benchmark_series",
        "quant_risk_model",
        "quant_expected_return",
        "quant_covariance",
    } <= tables

    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert {
        "v_corporate_action",
        "v_quant_return_daily",
        "v_risk_free_rate",
        "v_benchmark_series",
    } <= views

    # quant introduces no migration -- the shared schema_version floor is untouched.
    assert queries.current_version(conn) == 0
