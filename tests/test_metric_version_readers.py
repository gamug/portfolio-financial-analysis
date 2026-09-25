"""The consumers' metric readers take one version, not all of them (T-090).

Two engine versions of the same metric coexist for the same filing. Before T-090 the readers
joined ``fundamental_metrics`` unfiltered and let row order pick a winner; now each reads only
the versions its run resolved.
"""

from __future__ import annotations

import json

import pytest
from portfolio_common.db import Database

from cycle import data as cycle_data
from kg_schema import apply_migrations
from kg_schema.versions import MetricVersions, resolve_metric_versions
from quant.db import load_market_caps


def _add_filing(conn: Database, filing_id: int, asset_id: int, period_end: str) -> None:
    conn.execute(
        "INSERT INTO sec_filings (id, asset_id, form, fiscal_year, fiscal_period, period_end, "
        "filing_date, retrieved_at) VALUES (?, ?, '10-K', ?, ?, ?, date(?, '+45 days'), "
        "'2026-01-01T00:00:00Z')",
        (filing_id, asset_id, int(period_end[:4]), f"FY{period_end[:4]}", period_end, period_end),
    )


def _add_metric(  # noqa: PLR0913, PLR0917 - one metric row's identity
    conn: Database, filing_id: int, group: str, name: str, version: str, value: float
) -> None:
    inputs = (
        json.dumps({"market_capitalization": value}) if name == "market_capitalization" else "{}"
    )
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "inputs_json, computed_at, engine_version) VALUES (?, ?, ?, ?, 'x', ?, "
        "'2026-01-01T00:00:00Z', ?)",
        (filing_id, group, name, value, inputs, version),
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


def test_cycle_market_caps_follow_the_resolved_version(two_versions: Database) -> None:
    conn = two_versions
    metrics: dict[int, dict[str, float | None]] = {1: {}, 2: {}}
    v2 = cycle_data.market_cap_estimates(conn, "2026-06-30", metrics, resolve_metric_versions(conn))
    v1 = cycle_data.market_cap_estimates(
        conn, "2026-06-30", metrics, resolve_metric_versions(conn, "metrics-v1")
    )
    assert (v2[1], v2[2]) == (300.0, 400.0)
    assert (v1[1], v1[2]) == (100.0, 200.0)


def test_the_most_recent_filing_wins_deterministically(two_versions: Database) -> None:
    """Row order used to decide this. Now ``period_end`` does: a later filing's cap wins even
    though its row was inserted *before* the older filing's."""
    conn = two_versions
    _add_filing(conn, 3, 1, "2026-03-31")  # a newer filing for asset 1, inserted last...
    _add_metric(conn, 3, "valuation", "market_capitalization", "metrics-v2", 999.0)
    _add_filing(conn, 4, 2, "2024-12-31")  # ...and an OLDER filing for asset 2, also inserted last
    _add_metric(conn, 4, "valuation", "market_capitalization", "metrics-v2", 1.0)
    conn.commit()
    versions = resolve_metric_versions(conn)
    caps = cycle_data.market_cap_estimates(conn, "2026-06-30", {1: {}, 2: {}}, versions)
    assert caps[1] == 999.0  # newer period_end wins
    assert caps[2] == 400.0  # not the older filing that happens to be the last row
    assert load_market_caps(conn, [1, 2], versions, as_of="2026-06-30") == {1: 999.0, 2: 400.0}


def test_quant_market_caps_follow_the_resolved_version(two_versions: Database) -> None:
    conn = two_versions
    assert load_market_caps(conn, [1, 2], resolve_metric_versions(conn), as_of="2026-06-30") == {
        1: 300.0,
        2: 400.0,
    }
    assert load_market_caps(
        conn, [1, 2], resolve_metric_versions(conn, "metrics-v1"), as_of="2026-06-30"
    ) == {
        1: 100.0,
        2: 200.0,
    }
    assert load_market_caps(conn, [1], resolve_metric_versions(conn), as_of="2026-06-30") == {
        1: 300.0
    }  # asset filter
    assert load_market_caps(conn, [1, 2], MetricVersions({}), as_of="2026-06-30") == {}


def test_the_readers_require_the_versions_they_can_no_longer_omit(two_versions: Database) -> None:
    """A reader called without versions is a TypeError, not a silent read of every version."""
    with pytest.raises(TypeError):
        cycle_data.latest_metrics(two_versions, "2026-06-30")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        load_market_caps(two_versions, [1])  # type: ignore[call-arg]
