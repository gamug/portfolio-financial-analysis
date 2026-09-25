"""The ratio engine writes ``metrics-v3`` rows (T-102; ``metrics-v2`` was T-088), distinguishable
from any older copy."""

from __future__ import annotations

from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.db import METRICS_ENGINE_VERSION, FilingKey, FilingMeta
from fundamental_agent.metrics.base import MetricResult
from kg_schema import apply_migrations
from kg_schema.versions import resolve_metric_versions, version_key


def _filing(conn: Database) -> int:
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    return db.upsert_filing(
        conn, FilingKey(1, "10-K", 2025, "FY2025"), FilingMeta(period_end="2025-12-31")
    )


def _stored(conn: Database) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT engine_version, value FROM fundamental_metrics ORDER BY engine_version"
    )
    return [(r["engine_version"], r["value"]) for r in rows]


def test_the_engine_version_is_metrics_v3_and_outranks_v2() -> None:
    assert METRICS_ENGINE_VERSION == "metrics-v3"
    assert version_key(METRICS_ENGINE_VERSION) > version_key("metrics-v2")  # parses and sorts


def test_a_default_write_is_stamped_with_the_current_version(memory_db: Database) -> None:
    filing = _filing(memory_db)
    db.record_metrics(memory_db, filing, [("profitability", MetricResult("net_margin", 0.2, "r"))])
    assert _stored(memory_db) == [("metrics-v3", 0.2)]


def test_a_new_write_coexists_with_an_old_row_and_is_the_default_read(
    memory_db: Database,
) -> None:
    """The bump is the reason the version exists: the same filing and metric under both engines
    keeps both rows, and a consumer that names no version reads the new one."""
    apply_migrations(memory_db)  # the migrated key is what lets parallel versions coexist
    filing = _filing(memory_db)
    db.record_metrics(
        memory_db,
        filing,
        [("profitability", MetricResult("net_margin", 1.27, "r"))],
        engine_version="metrics-v2",  # an older engine's copy of the same metric
    )
    db.record_metrics(memory_db, filing, [("profitability", MetricResult("net_margin", 0.2, "r"))])
    assert _stored(memory_db) == [("metrics-v2", 1.27), ("metrics-v3", 0.2)]
    assert resolve_metric_versions(memory_db).get("profitability") == "metrics-v3"
