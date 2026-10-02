"""The consumers' metric readers take one version, not all of them (T-090).

Two engine versions of the same metric coexist for the same filing. Before T-090 the readers
joined ``fundamental_metrics`` unfiltered and let row order pick a winner; now each reads only
the versions its run resolved.
"""

from __future__ import annotations

import json

import pytest
from conftest import filed_after
from portfolio_common.db import Database

from cycle import data as cycle_data
from kg_schema import apply_migrations
from kg_schema.trading_calendar import available_from
from kg_schema.versions import MetricVersions, resolve_metric_versions


def _add_filing(conn: Database, filing_id: int, asset_id: int, period_end: str) -> None:
    conn.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, period_end, "
        "filing_date, available_at, retrieved_at) VALUES (?, ?, '10-K', ?, ?, ?, ?, ?, "
        "'2026-01-01T00:00:00Z')",
        (
            filing_id,
            asset_id,
            int(period_end[:4]),
            f"FY{period_end[:4]}",
            period_end,
            filed_after(period_end),
            available_from(filed_after(period_end)),
        ),
    )


def _add_metric(  # noqa: PLR0913, PLR0917 - one metric row's identity
    conn: Database, filing_id: int, group: str, name: str, version: str, value: float
) -> None:
    inputs = (
        json.dumps({"market_capitalization": value}) if name == "market_capitalization" else "{}"
    )
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "inputs_json, computed_at, engine_version, available_at) VALUES (?, ?, ?, ?, 'x', ?, "
        "'2026-01-01T00:00:00Z', ?, (SELECT available_at FROM sec_filings WHERE id = ?))",
        (filing_id, group, name, value, inputs, version, filing_id),
    )


@pytest.fixture
def two_versions(memory_db: Database) -> Database:
    """Asset 1: one filing carrying v1 *and* v2 of a profitability and a valuation metric."""
    apply_migrations(memory_db)  # the migrated key lets parallel versions coexist, as in production
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA'), (2, 'BBB')")
    _add_filing(memory_db, 1, 1, "2025-12-31")
    _add_filing(memory_db, 2, 2, "2025-12-31")
    for filing, cap_v1, cap_v2 in ((1, 100.0, 300.0), (2, 200.0, 400.0)):
        _add_metric(memory_db, filing, "valuation", "market_capitalization", "metrics-v1", cap_v1)
        _add_metric(memory_db, filing, "valuation", "market_capitalization", "metrics-v2", cap_v2)
        _add_metric(memory_db, filing, "profitability", "net_margin", "metrics-v1", 0.10)
        _add_metric(memory_db, filing, "profitability", "net_margin", "metrics-v2", 0.20)
    memory_db.commit()
    return memory_db


def test_latest_metrics_reads_only_the_resolved_version(two_versions: Database) -> None:
    conn = two_versions
    newest = cycle_data.latest_metrics(conn, "2026-06-30", resolve_metric_versions(conn))
    assert newest[1]["profitability.net_margin"] == 0.20  # v2 -- and one value, not two rows

    v1 = cycle_data.latest_metrics(conn, "2026-06-30", resolve_metric_versions(conn, "metrics-v1"))
    assert v1[1]["profitability.net_margin"] == 0.10

    mixed = cycle_data.latest_metrics(
        conn, "2026-06-30", resolve_metric_versions(conn, {"profitability": "metrics-v1"})
    )
    assert mixed[1]["profitability.net_margin"] == 0.10  # named -> v1
    assert mixed[1]["valuation.market_capitalization"] == 300.0  # not named -> newest, v2


def test_latest_metrics_with_no_resolved_versions_returns_nothing(two_versions: Database) -> None:
    assert cycle_data.latest_metrics(two_versions, "2026-06-30", MetricVersions({})) == {}


def test_the_readers_require_the_versions_they_can_no_longer_omit(two_versions: Database) -> None:
    """A reader called without versions is a TypeError, not a silent read of every version."""
    with pytest.raises(TypeError):
        cycle_data.latest_metrics(two_versions, "2026-06-30")  # type: ignore[call-arg]
