"""N2 (T-149, MET-05): the cycle's earnings yield is the trailing-year net income over the as-of
market cap, the positive part, with the negative-earnings dummy kept apart.

Before this, ``orchestrator._valorization`` read ``profitability.net_income`` -- not a stored metric
(0 of 34 metric names) -- so every real cycle ranked VALORIZATION's value factor without it (the
factor was empty in 35 of 40 stored rows). The trailing-year net income is now an audit input of
the profitability metric rows (``net_income_ttm``): a 10-K's fiscal year, a 10-Q's real TTM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from portfolio_common.db import Database

from cycle import data
from cycle.config import CycleSettings
from cycle.orchestrator import run_selection
from fundamental_agent.metrics.profitability import compute as profitability_compute
from fundamental_agent.statements import Statements
from kg_schema.versions import DATA_QUALITY_GATE_VERSION, MetricVersions
from pricing_agent import db as pricing_db

CYCLE_DATE = "2026-06-30"
V1 = MetricVersions({"profitability": "metrics-v1"})
SHARES, CLOSE = 1_000.0, 10.0  # a market cap of 10,000 for every name


def _roa_row(
    conn: Database, asset_id: int, inputs: dict[str, float], version: str = "metrics-v1"
) -> None:
    fid = conn.execute("SELECT id FROM sec_filings WHERE asset_id = ?", (asset_id,)).fetchone()[
        "id"
    ]
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "inputs_json, computed_at, engine_version, event_time, available_at) VALUES "
        "(?, 'profitability', 'return_on_assets', 0.05, 'ratio', ?, '2026-02-01T00:00:00Z', ?, "
        "'2025-12-31', '2026-02-02')",
        (fid, json.dumps(inputs), version),
    )


# -- the input ---------------------------------------------------------------------------------


def test_the_profitability_inputs_carry_the_trailing_year_net_income(aapl_10k: Statements) -> None:
    key = aapl_10k.latest_fy().key  # type: ignore[union-attr]
    (roa, *_) = profitability_compute(aapl_10k, key)
    assert roa.inputs["net_income_ttm"] == roa.inputs["net_income"] == 96_995_000_000.0


def test_a_10q_s_trailing_year_replaces_its_quarter(msft_10q: Statements) -> None:
    key = max(msft_10q.quarter_periods(), key=lambda p: p.date).key
    quarter = msft_10q.get("net_income", key)
    assert quarter is not None
    (roa, *_) = profitability_compute(msft_10q, key, None, {"net_income": 4.0 * quarter + 7.0})
    assert roa.inputs["net_income"] == quarter  # the raw quarter stays for a later TTM
    assert roa.inputs["net_income_ttm"] == 4.0 * quarter + 7.0


# -- the reader --------------------------------------------------------------------------------


def test_the_reader_takes_the_newest_usable_filing_in_the_resolved_version(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    _roa_row(conn, 1, {"net_income": 5.0, "net_income_ttm": 800.0})
    _roa_row(conn, 2, {"net_income": 5.0, "net_income_ttm": 999.0}, version="metrics-v9")
    assert data.latest_trailing_net_income(conn, CYCLE_DATE, V1) == {1: 800.0}


def test_a_quarter_times_four_is_not_a_trailing_year(cycle_seed: Database) -> None:
    conn = cycle_seed
    _roa_row(conn, 1, {"net_income_ttm": 800.0, "annualized_x4": 1.0})
    _roa_row(conn, 2, {"net_income_ttm": 500.0, "annualized_ttm": 1.0})
    assert data.latest_trailing_net_income(conn, CYCLE_DATE, V1) == {2: 500.0}


def test_a_filing_recorded_before_the_input_existed_has_none(cycle_seed: Database) -> None:
    _roa_row(cycle_seed, 1, {"net_income": 800.0, "total_assets": 1.0})
    assert data.latest_trailing_net_income(cycle_seed, CYCLE_DATE, V1) == {}


def test_a_filing_not_yet_usable_is_not_read(cycle_seed: Database) -> None:
    """Point in time: the seeded filings are usable from 2026-02-02."""
    _roa_row(cycle_seed, 1, {"net_income_ttm": 800.0})
    assert data.latest_trailing_net_income(cycle_seed, "2026-02-01", V1) == {}
    assert data.latest_trailing_net_income(cycle_seed, "2026-02-02", V1) == {1: 800.0}


# -- the cycle ---------------------------------------------------------------------------------


@pytest.fixture
def with_prices(cycle_seed: Database) -> Database:
    pricing_db.ensure_schema(cycle_seed)  # price_daily, for the market cap's close
    return cycle_seed


@pytest.fixture
def with_caps(with_prices: Database) -> Database:
    conn = with_prices
    for aid in range(1, 6):
        fid = conn.execute("SELECT id FROM sec_filings WHERE asset_id = ?", (aid,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO filing_cover_shares (filing_id, class_member, value, as_of_date, "
            "event_time, ingested_at) VALUES (?, '', ?, '2026-01-20', '2026-01-20', 'now')",
            (fid, SHARES),
        )
        conn.execute(
            "INSERT INTO price_daily (asset_id, date, close) VALUES (?, '2026-06-30', ?)",
            (aid, CLOSE),
        )
    conn.commit()
    return conn


def _value_row(conn: Database, asset_id: int, **inputs: float | None) -> None:
    """The D1 VALORIZATION value inputs (T-071 part 2b): v2 reads these, not the metric rows."""
    fid = conn.execute("SELECT id FROM sec_filings WHERE asset_id = ?", (asset_id,)).fetchone()[
        "id"
    ]
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "inputs_json, computed_at, engine_version, event_time, available_at) VALUES "
        "(?, 'rule_input', 'value_inputs', ?, 'usd', ?, '2026-02-01T00:00:00Z', 'metrics-v1', "
        "'2025-12-31', '2026-02-02')",
        (fid, inputs.get("net_income_ttm"), json.dumps(inputs)),
    )


def _all_measures(conn: Database, net_incomes: dict[int, float]) -> None:
    """Every asset's four measures present: the net income by asset, a flat EBITDA / MD-38 /
    book equity and a flat EV bridge, so the coverage rule (min 2 present) is satisfied."""
    for aid in range(1, 6):
        _value_row(
            conn,
            aid,
            net_income_ttm=net_incomes.get(aid),
            ebitda=200.0,
            md38_numerator=80.0,
            book_equity=500.0,
            total_debt=100.0,
            nci=0.0,
            cash=50.0,
        )
    conn.commit()


def _valorization(conn: Database) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    run_selection(CycleSettings(db_path=Path(":memory:"), top_n=3), CYCLE_DATE, conn=conn)
    step = conn.execute(
        "SELECT detail_json FROM cycle_checkpoint WHERE step = 'valorization'"
    ).fetchone()
    scores = {
        int(r["asset_id"]): json.loads(r["inputs_json"])
        for r in conn.execute(
            "SELECT asset_id, inputs_json FROM score_snapshot WHERE score_type = 'VALORIZATION'"
        )
    }
    return json.loads(step["detail_json"]), scores


def test_the_positive_part_is_ranked_and_the_loss_is_counted(with_caps: Database) -> None:
    """E/P = ``max(net income, 0) / market cap on D`` (MD-39, MET-05): AAA..CCC earn 800/500/200 on
    a cap of 10,000; DDD loses 300, so its positive part is 0 -- the lowest, not a negative yield --
    and EEE has no trailing net income (``missing_input``)."""
    conn = with_caps
    _all_measures(conn, {1: 800.0, 2: 500.0, 3: 200.0, 4: -300.0})
    detail, scores = _valorization(conn)
    assert detail["negative_earnings"] == 1 and detail["market_cap_missing"] == 0
    assert detail["scored"] == 5 and detail["empty"] == 0
    ey = {aid: c["measures"]["earnings_yield"] for aid, c in scores.items()}
    assert ey[1]["value"] == pytest.approx(0.08)  # 800 / (1,000 shares x 10)
    assert ey[4]["value"] == 0.0 and ey[4]["status"] == "present"  # the positive part
    assert ey[5]["status"] == "missing_input"
    assert ey[1]["percentile"] > ey[2]["percentile"] > ey[3]["percentile"] > ey[4]["percentile"]


def test_without_a_net_income_the_other_three_measures_still_score(with_caps: Database) -> None:
    """E/P is one of four: with no net income at all it is ``missing_input`` and the score is the
    other three measures' equal-weight mean (the coverage rule needs min(2, 4) = 2)."""
    conn = with_caps
    _all_measures(conn, {})
    detail, scores = _valorization(conn)
    assert detail["negative_earnings"] == 0 and detail["scored"] == 5
    for components in scores.values():
        assert components["measures"]["earnings_yield"]["status"] == "missing_input"
        assert components["measures"]["book_to_market"]["status"] == "present"
        assert components["present"] == 3.0 and components["applicable"] == 4.0


def test_a_name_with_no_market_cap_has_no_measure(with_prices: Database) -> None:
    """No cover count or close: the market cap on D is missing (T-132), so every measure is
    ``missing_input`` -- not zero -- and the score is empty (never a neutral 50)."""
    conn = with_prices
    _all_measures(conn, {aid: 100.0 * aid for aid in range(1, 6)})
    detail, scores = _valorization(conn)
    assert detail["market_cap_missing"] == 5 and detail["scored"] == 0 and detail["empty"] == 5
    for components in scores.values():
        assert all(m["status"] == "missing_input" for m in components["measures"].values())
        assert "market_cap_missing" in components["measures"]["earnings_yield"]["flags"]


def test_a_quarantined_return_on_assets_withholds_the_earnings_yield(with_caps: Database) -> None:
    """D2's gate map: E/P's carrier is ``profitability.return_on_assets``, so a gate that
    quarantines it drops E/P (status ``quarantined``) while the other measures stay."""
    conn = with_caps
    _all_measures(conn, {1: 800.0})
    fid = conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO data_quality_issue (filing_id, asset_id, metric_group, metric_name, "
        "metric_engine_version, rule_id, severity, quarantined, value, evidence_json, gate_version, "
        "created_at) VALUES (?, 1, 'profitability', 'return_on_assets', 'metrics-v1', 'DQ_ROA', "
        "'SOFT', 1, 0.05, '{}', ?, 'now')",
        (fid, DATA_QUALITY_GATE_VERSION),
    )
    conn.commit()
    _, scores = _valorization(conn)
    assert scores[1]["measures"]["earnings_yield"]["status"] == "quarantined"
    assert scores[1]["measures"]["earnings_yield"]["value"] is None
    assert scores[1]["measures"]["book_to_market"]["status"] == "present"
