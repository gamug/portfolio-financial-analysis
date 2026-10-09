"""The knowledge-graph view contract (T-144): one additive change to ``kg_schema``'s views.

New views ``v_fundamental_metric`` and ``v_cycle_ranking_component``; columns appended to five
existing views; ``v_quant_vs_live`` loses two dead filters; a marker migration (m010) raises
``schema_version`` to 10; ``ensure_views`` leaves a database that is ahead of the running code alone.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import _memory_database, seed_filing
from portfolio_common.db import Database

import kg_schema
from fundamental_agent import db as fundamental_db
from kg_schema import migrations, queries, versions
from kg_schema.views import VIEWS, ensure_views
from pricing_agent import db as pricing_db
from quant import db as quant_db
from quant.db import PortfolioRow, insert_portfolio, sync_positions

# Every column the views had before T-144, in order (a snapshot of master at 2026-10-09). A
# view may only gain columns at the end; the one value change is documented in
# ``test_available_at_is_the_cycle_date_for_cycle_computed_scores``.
_BASELINE_COLUMNS: dict[str, tuple[str, ...]] = {
    "v_score_snapshot": (
        "id",
        "ticker",
        "asset_id",
        "score_type",
        "raw_value",
        "normalized_score",
        "event_time",
        "computed_at",
        "model",
        "inputs_json",
        "filing_id",
        "rating",
        "narrative",
        "strengths_json",
        "risks_json",
        "run_id",
        "run_kind",
        "available_at",
    ),
    "v_universe_membership": (
        "id",
        "ticker",
        "asset_id",
        "universe",
        "valid_from",
        "valid_to",
        "detected_at",
        "source",
        "run_id",
        "run_kind",
    ),
    "v_analysis_run": (
        "run_id",
        "as_of",
        "code_version",
        "status",
        "started_at",
        "finished_at",
        "universe_size",
        "planned_units",
        "completed_units",
        "skipped_units",
        "failed_units",
        "params_json",
    ),
    "v_pricing_run": (
        "run_id",
        "as_of",
        "code_version",
        "status",
        "started_at",
        "finished_at",
        "universe_size",
        "planned_units",
        "completed_units",
        "skipped_units",
        "failed_units",
        "params_json",
    ),
    "v_quant_run": (
        "run_id",
        "command",
        "as_of",
        "code_version",
        "engine_version",
        "status",
        "started_at",
        "finished_at",
        "error",
        "params_json",
    ),
    "v_cycle_run": (
        "run_id",
        "cycle_type",
        "as_of",
        "code_version",
        "status",
        "started_at",
        "finished_at",
        "params_json",
    ),
    "v_universe_coverage": (
        "id",
        "as_of",
        "universe",
        "symbol",
        "asset_id",
        "in_assets",
        "has_fundamental",
        "has_metrics",
        "has_pricing",
        "has_observations",
        "has_returns",
        "covered",
        "missing_json",
        "checked_at",
        "run_id",
    ),
    "v_sector": (
        "sector_id",
        "sector_name",
        "asset_count",
        "sub_industry_count",
    ),
    "v_industry": (
        "industry_name",
        "sector_name",
        "sector_id",
        "asset_count",
    ),
    "v_price_observation": (
        "id",
        "ticker",
        "asset_id",
        "obs_date",
        "close",
        "prev_close",
        "log_return",
        "true_range",
        "atr_14",
        "realized_vol_21d",
        "realized_vol_90d",
        "max_drawdown_90d",
        "momentum_21d",
        "momentum_63d",
        "momentum_252d",
        "dollar_volume",
        "source",
        "event_time",
        "computed_at",
        "engine_version",
    ),
    "v_sec_filing": (
        "id",
        "ticker",
        "asset_id",
        "form",
        "fiscal_year",
        "fiscal_period",
        "filing_date",
        "accession_number",
        "period_end",
        "retrieved_at",
        "available_at",
    ),
    "v_sec_filing_section": (
        "id",
        "ticker",
        "asset_id",
        "filing_id",
        "form",
        "fiscal_period",
        "section_type",
        "item_number",
        "item_label",
        "heading",
        "ordinal",
        "text",
        "text_sha256",
        "word_count",
        "extraction_method",
        "source_url",
        "event_time",
        "retrieved_at",
        "engine_version",
    ),
    "v_veto": (
        "id",
        "ticker",
        "asset_id",
        "rule_id",
        "severity",
        "raised_on",
        "cleared_on",
        "last_seen_on",
        "detected_at",
        "cleared_at",
        "evidence_json",
        "run_id",
    ),
    "v_data_quality_issue": (
        "id",
        "ticker",
        "asset_id",
        "filing_id",
        "form",
        "fiscal_period",
        "period_end",
        "metric_group",
        "metric_name",
        "metric_engine_version",
        "rule_id",
        "severity",
        "quarantined",
        "value",
        "evidence_json",
        "gate_version",
        "created_at",
        "run_id",
    ),
    "v_rule_catalog": (
        "rule_id",
        "description",
        "severity",
        "enabled",
        "params_json",
        "param_metric",
        "param_operator",
        "param_threshold",
        "created_at",
    ),
    "v_portfolio_position": (
        "id",
        "ticker",
        "asset_id",
        "valid_from",
        "valid_to",
        "weight",
        "cost_basis",
        "opened_by_cycle",
        "run_id",
    ),
    "v_shared_executive_edge": (
        "asset_id_a",
        "ticker_a",
        "asset_id_b",
        "ticker_b",
        "person_count",
        "total_weight",
        "first_seen",
        "last_seen",
        "method",
    ),
    "v_cycle_ranking": (
        "cycle_run_id",
        "cycle_type",
        "cycle_date",
        "ticker",
        "asset_id",
        "rank",
        "blended_score",
        "components_json",
        "vetoed",
        "veto_rules_json",
        "selected",
        "target_weight",
    ),
    "v_weight_scheme": (
        "cycle_run_id",
        "cycle_type",
        "cycle_date",
        "scheme_id",
        "weights_json",
        "top_n",
        "max_name_weight",
        "max_sector_weight",
        "soft_veto_penalty",
    ),
    "v_weight_component": (
        "cycle_run_id",
        "cycle_type",
        "cycle_date",
        "score_type",
        "weight",
    ),
    "v_sector_aggregate_snapshot": (
        "id",
        "sector_name",
        "sector_id",
        "cycle_date",
        "metric_type",
        "member_count",
        "mean_raw",
        "mean_normalized",
        "computed_at",
        "run_id",
    ),
    "v_corporate_action": (
        "id",
        "ticker",
        "asset_id",
        "action_type",
        "ex_date",
        "value",
        "currency",
        "declared_date",
        "record_date",
        "pay_date",
        "frequency",
        "source",
        "engine_version",
        "ingested_at",
    ),
    "v_quant_return_daily": (
        "id",
        "ticker",
        "asset_id",
        "obs_date",
        "close_split_adj",
        "adj_close",
        "tr_index",
        "cash_dividend",
        "split_factor",
        "price_log_return",
        "tr_log_return",
        "source",
        "engine_version",
        "computed_at",
    ),
    "v_risk_free_rate": (
        "id",
        "curve",
        "rate_date",
        "annualized_rate",
        "source",
        "engine_version",
        "ingested_at",
    ),
    "v_benchmark_series": (
        "id",
        "benchmark",
        "obs_date",
        "level",
        "total_return_level",
        "log_return",
        "source",
        "engine_version",
        "ingested_at",
    ),
    "v_quant_risk_model": (
        "id",
        "as_of",
        "model_version",
        "lookback_days",
        "min_history_days",
        "n_assets",
        "cov_estimator",
        "cov_shrinkage",
        "ret_estimator",
        "periods_per_year",
        "panel_engine_version",
        "panel_spec_json",
        "rf_annual",
        "computed_at",
        "quant_run_id",
        "manifest_json",
    ),
    "v_quant_portfolio": (
        "id",
        "as_of",
        "kind",
        "frontier_k",
        "objective",
        "solver",
        "status",
        "expected_return",
        "expected_vol",
        "sharpe",
        "rf_annual",
        "n_positions",
        "turnover",
        "target_param",
        "model_id",
        "engine_version",
        "computed_at",
        "manifest_json",
    ),
    "v_quant_position": (
        "id",
        "portfolio_id",
        "as_of",
        "kind",
        "ticker",
        "asset_id",
        "weight",
        "valid_from",
        "valid_to",
    ),
    "v_quant_frontier_point": (
        "id",
        "model_id",
        "as_of",
        "k",
        "target_return",
        "expected_return",
        "expected_vol",
        "sharpe",
        "status",
        "weights_json",
        "portfolio_id",
    ),
    "v_quant_benchmark_performance": (
        "id",
        "portfolio_id",
        "as_of",
        "kind",
        "date",
        "realized_return",
        "cumulative_return",
        "benchmark",
        "benchmark_return",
        "active_return",
        "engine_version",
        "computed_at",
    ),
    "v_quant_vs_live": (
        "as_of",
        "kind",
        "ticker",
        "asset_id",
        "benchmark_weight",
        "live_weight",
        "active_weight",
    ),
}

NEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "v_score_snapshot": ("forensic_flags_json", "prompt_hash"),
    "v_shared_executive_edge": ("computed_at", "run_id"),
    "v_cycle_ranking": ("status",),
    "v_quant_vs_live": ("engine_version", "is_current"),
    "v_quant_portfolio": ("is_current",),
}

NEW_VIEWS: dict[str, tuple[str, ...]] = {
    "v_fundamental_metric": (
        "ticker",
        "asset_id",
        "filing_id",
        "metric_group",
        "metric_name",
        "metric_id",
        "unit",
        "value",
        "engine_version",
        "is_current",
        "event_time",
        "available_at",
        "run_id",
    ),
    "v_cycle_ranking_component": (
        "cycle_run_id",
        "asset_id",
        "score_type",
        "component_value",
        "configured_weight",
        "effective_weight",
    ),
}


@pytest.fixture
def conn() -> Database:
    """All three agents' tables plus the shared schema, migrated to the latest version."""
    db = _memory_database()
    fundamental_db.ensure_schema(db)
    pricing_db.ensure_schema(db)
    quant_db.ensure_schema(db)
    kg_schema.ensure(db, run_migrations=True)
    db.execute(
        "INSERT INTO assets (id, ticker, company_name) VALUES (1, 'AA', 'A'), (2, 'BB', 'B'), "
        "(3, 'CC', 'C')"
    )
    db.commit()
    return db


def _columns(db: Database, view: str) -> tuple[str, ...]:
    return tuple(r[1] for r in db.execute(f"PRAGMA table_info({view})"))


# -- the contract's shape -------------------------------------------------------------------


def test_every_view_builds_and_keeps_its_existing_columns_in_order(conn: Database) -> None:
    assert set(_BASELINE_COLUMNS) | set(NEW_VIEWS) == set(VIEWS)  # a view added here is listed
    for name, before in _BASELINE_COLUMNS.items():
        now = _columns(conn, name)
        assert now[: len(before)] == before, f"{name}: an existing column moved or changed"
        assert now[len(before) :] == NEW_COLUMNS.get(name, ()), f"{name}: unexpected new columns"


def test_the_new_views_have_exactly_the_documented_columns(conn: Database) -> None:
    for name, cols in NEW_VIEWS.items():
        assert _columns(conn, name) == cols


def test_the_marker_migration_raises_schema_version_to_10_and_is_idempotent(
    conn: Database,
) -> None:
    assert max(v for v, _, _ in migrations.MIGRATIONS) == 10
    assert queries.current_version(conn) == 10
    assert migrations.apply_migrations(conn) == []  # a second run applies nothing


def test_the_marker_migration_changes_no_table(conn: Database) -> None:
    """m010 is a marker: the views come from ``ensure``, so a database at 9 gets the same
    tables and the same views whether or not it has been migrated."""
    before = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
    conn.execute("DELETE FROM schema_version WHERE version = 10")
    conn.commit()
    assert migrations.apply_migrations(conn) == [10]
    after = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
    assert after == before


# -- ensure_views' guard --------------------------------------------------------------------


def test_ensure_views_leaves_a_database_that_is_ahead_of_the_code(conn: Database) -> None:
    """Later code may have added columns and views this code does not define: rebuilding would
    remove them."""
    conn.execute("DROP VIEW v_cycle_ranking_component")
    conn.execute("DROP VIEW v_cycle_ranking")
    conn.execute("CREATE VIEW v_cycle_ranking AS SELECT 1 AS from_a_later_contract")
    queries.record(conn, 11, "a later contract")
    ensure_views(conn)
    assert _columns(conn, "v_cycle_ranking") == ("from_a_later_contract",)
    assert not conn.relation_exists("v_cycle_ranking_component")
    kg_schema.ensure(conn)  # the path every agent takes on start-up is guarded too
    assert _columns(conn, "v_cycle_ranking") == ("from_a_later_contract",)


def test_ensure_views_rebuilds_at_the_code_version_and_below(conn: Database) -> None:
    conn.execute("DROP VIEW v_cycle_ranking_component")
    ensure_views(conn)  # schema_version == the highest migration the code knows
    assert conn.relation_exists("v_cycle_ranking_component")
    conn.execute("DROP VIEW v_cycle_ranking_component")
    conn.execute("DELETE FROM schema_version WHERE version > 9")
    ensure_views(conn)  # a database still at 9 is not ahead
    assert conn.relation_exists("v_cycle_ranking_component")


def test_ensure_views_on_a_database_without_schema_version_does_not_create_it() -> None:
    db = _memory_database()
    db.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT, sub_industry TEXT)")
    ensure_views(db)  # must not raise, and must not write a table of its own
    assert not db.relation_exists("schema_version")


# -- v_score_snapshot -----------------------------------------------------------------------


def _score(db: Database, asset_id: int, score_type: str, event_time: str, **extra: Any) -> None:
    cols = {
        "asset_id": asset_id,
        "score_type": score_type,
        "raw_value": 1.0,
        "event_time": event_time,
        "computed_at": "now",
        **extra,
    }
    db.execute(
        f"INSERT INTO score_snapshot ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",  # noqa: S608
        tuple(cols.values()),
    )


def test_available_at_is_the_cycle_date_for_cycle_computed_scores(conn: Database) -> None:
    fid = seed_filing(conn, 1, period_end="2025-12-31", filing_date="2026-02-10")
    stored = conn.execute("SELECT available_at FROM sec_filings WHERE id = ?", (fid,)).fetchone()[0]
    _score(conn, 1, "FUNDAMENTAL", "2025-12-31", filing_id=fid, available_at=stored)
    for t in ("TECHNICAL", "VALORIZATION", "SECTOR"):
        _score(conn, 1, t, "2026-03-02")
    _score(conn, 1, "SEMANTIC", "2026-03-02")
    conn.commit()
    got = {
        r["score_type"]: (r["event_time"], r["available_at"])
        for r in conn.execute("SELECT * FROM v_score_snapshot")
    }
    assert got["FUNDAMENTAL"] == ("2025-12-31", stored)  # the stored column, unchanged
    for t in ("TECHNICAL", "VALORIZATION", "SECTOR"):
        assert got[t] == ("2026-03-02", "2026-03-02")  # NULL -> the cycle date
    assert got["SEMANTIC"] == ("2026-03-02", None)  # not written until Work item 4
    stored_null = conn.execute(
        "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'TECHNICAL' "
        "AND available_at IS NULL"
    ).fetchone()[0]
    assert stored_null == 1  # the view computes it; no row was rewritten


def test_forensic_flags_and_prompt_hash_are_projected(conn: Database) -> None:
    flags = json.dumps({"data_error_suspected": False, "severe_sbc_dilution": True})
    fid = seed_filing(conn, 1, period_end="2025-12-31", filing_date="2026-02-10")
    avail = conn.execute("SELECT available_at FROM sec_filings WHERE id = ?", (fid,)).fetchone()[0]
    _score(
        conn,
        1,
        "FUNDAMENTAL",
        "2025-12-31",
        filing_id=fid,
        available_at=avail,
        forensic_flags_json=flags,
        prompt_hash="abc123",
    )
    _score(conn, 1, "TECHNICAL", "2026-03-02")
    conn.commit()
    rows = {r["score_type"]: r for r in conn.execute("SELECT * FROM v_score_snapshot")}
    assert rows["FUNDAMENTAL"]["forensic_flags_json"] == flags
    assert rows["FUNDAMENTAL"]["prompt_hash"] == "abc123"
    assert rows["TECHNICAL"]["forensic_flags_json"] is None
    assert rows["TECHNICAL"]["prompt_hash"] is None


# -- v_fundamental_metric -------------------------------------------------------------------


def _metric(  # noqa: PLR0913, PLR0917 - one metric row, every column a test may pin
    db: Database,
    filing_id: int,
    group: str,
    name: str,
    version: str,
    value: float = 1.0,
    unit: str = "ratio",
) -> None:
    avail = db.execute(
        "SELECT available_at FROM sec_filings WHERE id = ?", (filing_id,)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "computed_at, engine_version, event_time, run_id, available_at) "
        "VALUES (?, ?, ?, ?, ?, 'now', ?, '2025-12-31', 7, ?)",
        (filing_id, group, name, value, unit, version, avail),
    )


def test_v_fundamental_metric_projects_the_metric_with_its_filing_and_asset(
    conn: Database,
) -> None:
    fid = seed_filing(conn, 2, period_end="2025-12-31", filing_date="2026-02-10")
    _metric(conn, fid, "leverage", "debt_to_equity", "metrics-v2", 1.5, "x")
    conn.commit()
    row = conn.execute("SELECT * FROM v_fundamental_metric").fetchone()
    avail = conn.execute("SELECT available_at FROM sec_filings WHERE id = ?", (fid,)).fetchone()[0]
    assert dict(row) == {
        "ticker": "BB",
        "asset_id": 2,
        "filing_id": fid,
        "metric_group": "leverage",
        "metric_name": "debt_to_equity",
        "metric_id": "leverage.debt_to_equity",
        "unit": "x",
        "value": 1.5,
        "engine_version": "metrics-v2",
        "is_current": 1,
        "event_time": "2025-12-31",
        "available_at": avail,
        "run_id": 7,
    }
    assert row["available_at"] >= row["event_time"]


def test_v_fundamental_metric_id_joins_the_rule_catalog(conn: Database) -> None:
    fid = seed_filing(conn, 1, period_end="2025-12-31", filing_date="2026-02-10")
    _metric(conn, fid, "leverage", "debt_to_equity", "metrics-v2")
    conn.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, params_json, enabled, "
        "created_at) VALUES ('LEV', 'd', 'HARD', "
        '\'{"metric": "leverage.debt_to_equity", "op": ">", "threshold": 3.0}\', 1, \'now\')'
    )
    conn.commit()
    joined = conn.execute(
        "SELECT rc.rule_id FROM v_fundamental_metric m "
        "JOIN v_rule_catalog rc ON rc.param_metric = m.metric_id"
    ).fetchall()
    assert [r[0] for r in joined] == ["LEV"]


def test_is_current_is_the_newest_version_per_group_by_explicit_order(conn: Database) -> None:
    """``metrics-v10`` is newer than ``metrics-v2`` (not string order) and ``pre-v1`` is older
    than both; the choice is per metric group; a filing not recomputed under the newest version
    has no current row; an unparseable version is never current."""
    f1 = seed_filing(conn, 1, period_end="2024-12-31", filing_date="2025-02-10")
    f2 = seed_filing(conn, 1, period_end="2025-12-31", filing_date="2026-02-10")
    for version in ("pre-v1", "metrics-v2", "metrics-v10"):
        _metric(conn, f2, "profitability", "roe", version)
    _metric(conn, f1, "profitability", "roe", "metrics-v2")  # f1 was not recomputed under v10
    _metric(conn, f2, "valuation", "pe", "metrics-v2")  # valuation never reached v10
    _metric(conn, f2, "liquidity", "current_ratio", "legacy-v3")  # family not registered
    conn.commit()
    current = {
        (r["filing_id"], r["metric_group"], r["engine_version"])
        for r in conn.execute("SELECT * FROM v_fundamental_metric WHERE is_current = 1")
    }
    assert current == {(f2, "profitability", "metrics-v10"), (f2, "valuation", "metrics-v2")}
    # one current version per group, one current row per (filing, metric)
    per_key = conn.execute(
        "SELECT COUNT(*) FROM v_fundamental_metric WHERE is_current = 1 "
        "GROUP BY filing_id, metric_group, metric_name HAVING COUNT(*) > 1"
    ).fetchall()
    assert per_key == []


def test_is_current_matches_what_resolve_metric_versions_picks(conn: Database) -> None:
    f = seed_filing(conn, 1, period_end="2025-12-31", filing_date="2026-02-10")
    for group, vs in {
        "profitability": ("pre-v1", "metrics-v1", "metrics-v3"),
        "leverage": ("metrics-v2", "metrics-v10"),
        "valuation": ("metrics-v9",),
    }.items():
        for v in vs:
            _metric(conn, f, group, "m", v)
    conn.commit()
    picked = versions.resolve_metric_versions(conn).manifest()
    current = {
        r["metric_group"]: r["engine_version"]
        for r in conn.execute("SELECT * FROM v_fundamental_metric WHERE is_current = 1")
    }
    assert (
        current
        == picked
        == {
            "leverage": "metrics-v10",
            "profitability": "metrics-v3",
            "valuation": "metrics-v9",
        }
    )


# -- v_shared_executive_edge / v_cycle_ranking ----------------------------------------------


def test_shared_executive_edge_carries_computed_at_and_run_id(conn: Database) -> None:
    conn.executemany(
        "INSERT INTO shared_executive_edge (asset_id_a, asset_id_b, person_name, "
        "article_count_a, article_count_b, weight, first_seen, last_seen, method, "
        "computed_at, run_id) VALUES (1, 2, ?, 1, 1, 0.5, '2025-01-01', '2025-06-01', 'm', ?, 4)",
        [("P One", "2026-03-01T00:00:00+00:00"), ("P Two", "2026-03-02T00:00:00+00:00")],
    )
    conn.commit()
    row = conn.execute("SELECT * FROM v_shared_executive_edge").fetchone()
    assert row["person_count"] == 2
    assert row["computed_at"] == "2026-03-02T00:00:00+00:00"
    assert row["run_id"] == 4


def _cycle(  # noqa: PLR0913, PLR0917 - one cycle run, every column a test may pin
    db: Database,
    run_id: int,
    date: str,
    status: str,
    weights: dict[str, float] | None = None,
    soft_veto_penalty: float = 15.0,
) -> None:
    params: dict[str, Any] = {"soft_veto_penalty": soft_veto_penalty}
    if weights is not None:
        params["score_weights"] = weights
    db.execute(
        "INSERT INTO cycle_run (id, cycle_type, cycle_date, started_at, status, params_json) "
        "VALUES (?, 'MONITORING', ?, 'now', ?, ?)",
        (run_id, date, status, json.dumps(params)),
    )


def _rank(  # noqa: PLR0913, PLR0917 - one ranking row, every column a test may pin
    db: Database,
    run_id: int,
    asset_id: int,
    rank: int,
    blended: float,
    components: dict[str, float | None],
    veto_rules: list[str] | None = None,
) -> None:
    db.execute(
        "INSERT INTO cycle_ranking (cycle_run_id, asset_id, rank, blended_score, "
        "components_json, vetoed, veto_rules_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            asset_id,
            rank,
            blended,
            json.dumps(components),
            int(bool(veto_rules and "HARD" in veto_rules)),
            json.dumps(veto_rules or []),
        ),
    )


def test_v_cycle_ranking_carries_the_runs_status(conn: Database) -> None:
    _cycle(conn, 1, "2026-03-02", "completed")
    _cycle(conn, 2, "2026-03-03", "failed")
    _rank(conn, 1, 1, 1, 50.0, {"FUNDAMENTAL": 50.0})
    _rank(conn, 2, 1, 1, 50.0, {"FUNDAMENTAL": 50.0})
    conn.commit()
    got = {r["cycle_run_id"]: r["status"] for r in conn.execute("SELECT * FROM v_cycle_ranking")}
    assert got == {1: "completed", 2: "failed"}


# -- v_cycle_ranking_component --------------------------------------------------------------


WEIGHTS = {"FUNDAMENTAL": 0.5, "TECHNICAL": 0.3, "VALORIZATION": 0.2, "SEMANTIC": 0.0}


def test_components_one_row_per_non_null_component_vetoed_or_not(conn: Database) -> None:
    _cycle(conn, 1, "2026-03-02", "completed", WEIGHTS)
    _rank(conn, 1, 1, 1, 60.0, {"FUNDAMENTAL": 60.0, "TECHNICAL": 60.0, "VALORIZATION": 60.0})
    _rank(  # a HARD-vetoed row still has its components
        conn, 1, 2, 2, 40.0, {"FUNDAMENTAL": 40.0, "TECHNICAL": None}, veto_rules=["HARD"]
    )
    _rank(conn, 1, 3, 3, 0.0, {"FUNDAMENTAL": None, "TECHNICAL": None})  # no component at all
    conn.commit()
    rows = conn.execute(
        "SELECT asset_id, score_type FROM v_cycle_ranking_component ORDER BY asset_id, score_type"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (1, "FUNDAMENTAL"),
        (1, "TECHNICAL"),
        (1, "VALORIZATION"),
        (2, "FUNDAMENTAL"),
    ]


def test_effective_weights_renormalize_over_the_non_null_components(conn: Database) -> None:
    _cycle(conn, 1, "2026-03-02", "completed", WEIGHTS)
    _rank(conn, 1, 1, 1, 0.0, {"FUNDAMENTAL": 80.0, "TECHNICAL": 40.0, "VALORIZATION": None})
    conn.commit()
    got = {
        r["score_type"]: (r["component_value"], r["configured_weight"], r["effective_weight"])
        for r in conn.execute("SELECT * FROM v_cycle_ranking_component")
    }
    assert got == {
        "FUNDAMENTAL": (80.0, 0.5, pytest.approx(0.5 / 0.8)),
        "TECHNICAL": (40.0, 0.3, pytest.approx(0.3 / 0.8)),
    }


def test_blended_score_identity_holds_with_soft_vetoes(conn: Database) -> None:
    """sum(effective weight * component) - soft_veto_penalty * (SOFT vetoes) = blended_score."""
    _cycle(conn, 1, "2026-03-02", "completed", WEIGHTS, soft_veto_penalty=12.5)
    cases: dict[int, tuple[dict[str, float | None], list[str]]] = {  # asset -> (comps, vetoes)
        1: ({"FUNDAMENTAL": 80.0, "TECHNICAL": 40.0, "VALORIZATION": 55.0}, []),
        2: ({"FUNDAMENTAL": 70.0, "TECHNICAL": None, "VALORIZATION": 20.0}, ["LEV_SOFT"]),
        3: ({"FUNDAMENTAL": 30.0, "TECHNICAL": 90.0}, ["A", "B", "HARD", "UNSCORED"]),
    }
    for rank, (asset, (comps, rules)) in enumerate(cases.items(), start=1):
        num = sum(WEIGHTS[k] * v for k, v in comps.items() if v is not None)
        den = sum(WEIGHTS[k] for k, v in comps.items() if v is not None)
        soft = [r for r in rules if r not in ("HARD", "UNSCORED")]
        _rank(conn, 1, asset, rank, num / den - 12.5 * len(soft), comps, rules)
    conn.commit()
    penalty = conn.execute("SELECT soft_veto_penalty FROM v_weight_scheme").fetchone()[0]
    assert penalty == 12.5
    for r in conn.execute(
        "SELECT asset_id, blended_score, veto_rules_json FROM v_cycle_ranking"
    ).fetchall():
        parts = conn.execute(
            "SELECT component_value, effective_weight FROM v_cycle_ranking_component "
            "WHERE cycle_run_id = 1 AND asset_id = ?",
            (r["asset_id"],),
        ).fetchall()
        assert sum(p["effective_weight"] for p in parts) == pytest.approx(1.0)
        soft = [x for x in json.loads(r["veto_rules_json"]) if x not in ("HARD", "UNSCORED")]
        got = sum(p["effective_weight"] * p["component_value"] for p in parts) - penalty * len(soft)
        assert got == pytest.approx(r["blended_score"], abs=1e-9)


def test_a_run_without_recorded_weights_still_lists_its_components(conn: Database) -> None:
    _cycle(conn, 1, "2026-03-02", "completed", None)
    _rank(conn, 1, 1, 1, 50.0, {"FUNDAMENTAL": 50.0})
    conn.commit()
    row = conn.execute("SELECT * FROM v_cycle_ranking_component").fetchone()
    assert (row["component_value"], row["configured_weight"], row["effective_weight"]) == (
        50.0,
        None,
        None,
    )


def test_all_zero_weights_give_no_effective_weight_rather_than_a_division_error(
    conn: Database,
) -> None:
    _cycle(conn, 1, "2026-03-02", "completed", {"FUNDAMENTAL": 0.0})
    _rank(conn, 1, 1, 1, 0.0, {"FUNDAMENTAL": 50.0})
    conn.commit()
    row = conn.execute("SELECT * FROM v_cycle_ranking_component").fetchone()
    assert row["configured_weight"] == 0.0 and row["effective_weight"] is None


# -- the quant views ------------------------------------------------------------------------


def _book(  # noqa: PLR0913 - one book, every column a test may pin
    db: Database,
    kind: str,
    version: str,
    weights: dict[int, float],
    *,
    as_of: str = "2026-01-02",
    computed_at: str = "2026-01-02T00:00:00+00:00",
    frontier_k: int | None = None,
) -> int:
    pid = insert_portfolio(
        db,
        PortfolioRow(
            as_of=as_of,
            kind=kind,
            objective=kind,
            solver="CLARABEL",
            status="optimal",
            expected_return=0.05,
            expected_vol=0.1,
            sharpe=0.5,
            rf_annual=0.04,
            n_positions=len(weights),
            engine_version=version,
            frontier_k=frontier_k,
        ),
    )
    db.execute("UPDATE quant_portfolio SET computed_at = ? WHERE id = ?", (computed_at, pid))
    sync_positions(db, pid, as_of, weights)
    return pid


def _current_books(db: Database) -> set[tuple[str, int | None, str]]:
    return {
        (r["kind"], r["frontier_k"], r["engine_version"])
        for r in db.execute(
            "SELECT qp.kind, qp.frontier_k, v.engine_version FROM v_quant_portfolio v "
            "JOIN quant_portfolio qp ON qp.id = v.id WHERE v.is_current = 1"
        )
    }


def test_quant_is_current_is_the_newest_opt_version_per_key(conn: Database) -> None:
    _book(conn, "min_var", "opt-v1+aaaaaaaa", {1: 1.0})
    _book(conn, "min_var", "opt-v2+bbbbbbbb", {1: 1.0})
    _book(conn, "min_var", "opt-v10+cccccccc", {1: 1.0})  # numeric, not string, order
    _book(conn, "tangency", "opt-v1", {1: 1.0})  # a bare, untagged version
    _book(conn, "tangency", "opt-v1+dddddddd", {1: 1.0}, computed_at="2026-01-03T00:00:00+00:00")
    _book(conn, "min_var", "opt-v1+aaaaaaaa", {1: 1.0}, as_of="2026-02-02")  # another as_of
    _book(conn, "frontier_k", "opt-v1+eeeeeeee", {1: 1.0}, frontier_k=1)
    _book(conn, "frontier_k", "opt-v1+eeeeeeee", {1: 1.0}, frontier_k=2)  # a book of its own
    _book(conn, "min_var", "ext-v9+ffffffff", {1: 1.0}, as_of="2026-03-02")  # not an opt-v*
    conn.commit()
    assert _current_books(conn) == {
        ("min_var", None, "opt-v10+cccccccc"),
        ("tangency", None, "opt-v1+dddddddd"),  # same N: the later computed_at
        ("min_var", None, "opt-v1+aaaaaaaa"),  # 2026-02-02's only book
        ("frontier_k", 1, "opt-v1+eeeeeeee"),
        ("frontier_k", 2, "opt-v1+eeeeeeee"),
    }


def test_v_quant_vs_live_returns_every_kind_but_the_live_book(conn: Database) -> None:
    """``equal_weight`` and ``cap_weight`` were dead filters (no code writes those kinds); a book of
    any such kind now shows, beside the live book's names."""
    _book(conn, "min_var", "opt-v1+aaaaaaaa", {1: 0.6, 2: 0.4})
    _book(conn, "equal_weight", "opt-v1+aaaaaaaa", {1: 0.5, 2: 0.5})
    _book(conn, "cap_weight", "opt-v1+aaaaaaaa", {1: 0.7, 2: 0.3})
    _book(conn, "live_book", "opt-v1+aaaaaaaa", {1: 1.0})
    conn.commit()
    kinds = {r[0] for r in conn.execute("SELECT kind FROM v_quant_vs_live")}
    assert kinds == {"min_var", "equal_weight", "cap_weight"}


def test_v_quant_vs_live_carries_the_books_engine_version_and_is_current(
    conn: Database,
) -> None:
    _book(conn, "min_var", "opt-v1+aaaaaaaa", {1: 1.0})
    _book(conn, "min_var", "opt-v2+bbbbbbbb", {1: 1.0})
    conn.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, valid_to, weight) "
        "VALUES (3, '2025-12-01', NULL, 0.2)"
    )
    conn.commit()
    got = {
        (r["kind"], r["ticker"], r["engine_version"]): r["is_current"]
        for r in conn.execute("SELECT * FROM v_quant_vs_live")
    }
    assert got == {
        ("min_var", "AA", "opt-v1+aaaaaaaa"): 0,
        ("min_var", "AA", "opt-v2+bbbbbbbb"): 1,
        ("LIVE_ONLY", "CC", None): 1,  # a live-only name belongs to no book version
    }
    per_key = conn.execute(
        "SELECT COUNT(*) FROM v_quant_vs_live WHERE is_current = 1 "
        "GROUP BY as_of, kind, asset_id HAVING COUNT(*) > 1"
    ).fetchall()
    assert per_key == []
