"""T-157: a fresh database is born at the append-only key SPEC §5 declares.

``fundamental_agent run`` creates its tables through :func:`fundamental_agent.db.ensure_schema`
without a manual ``migrate``, so the agent's own DDL must declare m002's / m003's target key --
otherwise a new engine version's (or a new filing version's) row is dropped by ``INSERT OR IGNORE``
in silence, and a database that never ran ``migrate`` can hold only one version of a filing's
metrics or facts. These tests pin the key against ``kg_schema``'s own target (so the two cannot
drift), the coexistence of two versions on a fresh database, and the loud refusal of a table that
still carries the pre-migration key.
"""

from __future__ import annotations

import pytest
from conftest import filed_after
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.db import (
    FACTS_ENGINE_VERSION,
    METRICS_ENGINE_VERSION,
    FilingKey,
    FilingMeta,
    LegacyAppendOnlyKey,
)
from fundamental_agent.metrics.base import MetricResult
from kg_schema import migrations


def _filing(conn: Database) -> int:
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    return db.upsert_filing(
        conn,
        FilingKey(1, "10-K", 2025, "FY2025"),
        FilingMeta(filing_date=filed_after("2025-12-31"), period_end="2025-12-31"),
    )


def _unique_columns(conn: Database, table: str) -> tuple[str, ...]:
    """The columns of *table*'s UNIQUE index (its first one), read through the pragma functions."""
    for row in conn.execute('SELECT name, "unique" AS u FROM pragma_index_list(?)', (table,)):
        if not int(row["u"]):
            continue
        return tuple(
            str(r["name"])
            for r in conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (str(row["name"]),)
            )
        )
    return ()


def _legacy_metrics_table(conn: Database) -> None:
    """The pre-m003 shape: ``engine_version`` nullable and outside the key."""
    conn.execute("DROP TABLE fundamental_metrics")
    conn.execute(
        """
        CREATE TABLE fundamental_metrics (
            id INTEGER PRIMARY KEY,
            filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
            metric_group TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            value REAL,
            unit TEXT,
            inputs_json TEXT,
            computed_at TEXT NOT NULL,
            engine_version TEXT,
            event_time TEXT,
            run_id INTEGER,
            available_at TEXT,
            UNIQUE (filing_id, metric_group, metric_name)
        )
        """
    )
    conn.commit()


def _legacy_facts_table(conn: Database) -> None:
    """The pre-m002 shape: ``filing_version`` nullable and outside the key."""
    conn.execute("DROP TABLE financial_facts")
    conn.execute(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
            statement TEXT NOT NULL,
            concept TEXT NOT NULL,
            standard_concept TEXT,
            label TEXT,
            period_key TEXT NOT NULL,
            value REAL,
            filing_version TEXT,
            event_time TEXT,
            ingested_at TEXT,
            run_id INTEGER,
            correction_rule TEXT,
            preferred_sign REAL,
            UNIQUE (filing_id, statement, concept, period_key)
        )
        """
    )
    conn.commit()


# -- the key a fresh database is born with --------------------------------------------------------


def test_a_fresh_database_declares_the_migrations_target_keys(memory_db: Database) -> None:
    """The agent's own DDL equals SPEC §5's declared key, read from ``kg_schema`` itself, so the
    fresh shape and m002/m003's target cannot drift apart."""
    for table in ("financial_facts", "fundamental_metrics"):
        target = migrations.target_unique_key(table)
        assert target is not None
        assert _unique_columns(memory_db, table) == target
        assert migrations.at_target_unique_key(memory_db, table)


def test_a_fresh_database_holds_two_engine_versions_of_one_filing(memory_db: Database) -> None:
    """The bug T-157 fixes: on a database born by ``ensure_schema`` alone, a second engine version's
    row is kept beside the first, never dropped by the unique key."""
    filing = _filing(memory_db)
    db.record_metrics(
        memory_db, filing, [("profitability", MetricResult("net_margin", 0.2, "ratio"))]
    )
    db.record_metrics(
        memory_db,
        filing,
        [("profitability", MetricResult("net_margin", 0.31, "ratio"))],
        engine_version="metrics-v99",
    )
    rows = memory_db.execute(
        "SELECT engine_version, value FROM fundamental_metrics ORDER BY engine_version"
    ).fetchall()
    assert [(r["engine_version"], r["value"]) for r in rows] == [
        (METRICS_ENGINE_VERSION, 0.2),
        ("metrics-v99", 0.31),
    ]


def test_a_fresh_database_holds_two_filing_versions_of_one_fact(memory_db: Database) -> None:
    """The same for ``financial_facts``: a restatement under a new ``filing_version`` coexists."""
    filing = _filing(memory_db)
    fact = {
        "statement": "income_statement",
        "concept": "us-gaap_Revenues",
        "label": "Revenues",
        "period_key": "2025-12-31 (FY)",
        "value": 100.0,
    }
    db.append_financial_facts(memory_db, filing, [fact])
    db.append_financial_facts(
        memory_db, filing, [{**fact, "value": 120.0}], filing_version="facts-v99"
    )
    rows = memory_db.execute(
        "SELECT filing_version, value FROM financial_facts ORDER BY filing_version"
    ).fetchall()
    assert [(r["filing_version"], r["value"]) for r in rows] == [
        (FACTS_ENGINE_VERSION, 100.0),
        ("facts-v99", 120.0),
    ]


def test_a_re_run_of_the_same_version_is_still_ignored(memory_db: Database) -> None:
    """Append-only is unchanged: the same version's row is not duplicated."""
    filing = _filing(memory_db)
    db.record_metrics(
        memory_db, filing, [("profitability", MetricResult("net_margin", 0.2, "ratio"))]
    )
    db.record_metrics(
        memory_db, filing, [("profitability", MetricResult("net_margin", 0.9, "ratio"))]
    )
    rows = memory_db.execute("SELECT value FROM fundamental_metrics").fetchall()
    assert [r["value"] for r in rows] == [0.2]


# -- a pre-migration table is refused, never silently ignored --------------------------------------


def test_a_pre_migration_metrics_key_is_refused(memory_db: Database) -> None:
    """A table with the pre-m003 key would drop the new version's row; the write fails loudly."""
    filing = _filing(memory_db)
    _legacy_metrics_table(memory_db)
    with pytest.raises(LegacyAppendOnlyKey, match="fundamental_metrics"):
        db.record_metrics(
            memory_db, filing, [("profitability", MetricResult("net_margin", 0.2, "ratio"))]
        )


def test_a_pre_migration_facts_key_is_refused(memory_db: Database) -> None:
    filing = _filing(memory_db)
    _legacy_facts_table(memory_db)
    fact = {
        "statement": "income_statement",
        "concept": "us-gaap_Revenues",
        "period_key": "2025-12-31 (FY)",
        "value": 100.0,
    }
    with pytest.raises(LegacyAppendOnlyKey, match="financial_facts"):
        db.append_financial_facts(memory_db, filing, [fact])


def test_migrate_still_upgrades_a_pre_migration_table(memory_db: Database) -> None:
    """T-154's guard is unchanged: the rebuild still runs on the old key and the write then works."""
    filing = _filing(memory_db)
    _legacy_metrics_table(memory_db)
    _legacy_facts_table(memory_db)
    assert not migrations.at_target_unique_key(memory_db, "fundamental_metrics")
    assert not migrations.at_target_unique_key(memory_db, "financial_facts")
    applied = migrations.apply_migrations(memory_db)
    assert 2 in applied and 3 in applied
    assert migrations.at_target_unique_key(memory_db, "fundamental_metrics")
    assert migrations.at_target_unique_key(memory_db, "financial_facts")
    db.record_metrics(
        memory_db, filing, [("profitability", MetricResult("net_margin", 0.2, "ratio"))]
    )
    db.record_metrics(
        memory_db,
        filing,
        [("profitability", MetricResult("net_margin", 0.31, "ratio"))],
        engine_version="metrics-v99",
    )
    assert len(memory_db.execute("SELECT 1 FROM fundamental_metrics").fetchall()) == 2
