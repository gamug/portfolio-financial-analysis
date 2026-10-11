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

import pytest
from portfolio_common.db import Database

from cycle import data
from cycle.config import CycleSettings
from cycle.orchestrator import run_selection
from cycle.scores.normalize import rank_pct
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


def _pct(values: list[float]) -> list[float]:
    return [p for p in rank_pct(list(values)) if p is not None]


def _valorization(conn: Database) -> tuple[dict[str, int], dict[int, dict[str, float | None]]]:
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


def test_the_value_factor_ranks_the_positive_part_and_counts_the_loss(with_caps: Database) -> None:
    """AAA..CCC earn 800, 500 and 200 on a cap of 10,000; DDD loses 300 (E(+)/P = 0, the lowest);
    EEE has no trailing net income. The seeded free-cash-flow yields fall AAA > ... > EEE."""
    conn = with_caps
    for aid, ni in ((1, 800.0), (2, 500.0), (3, 200.0), (4, -300.0)):
        _roa_row(conn, aid, {"net_income_ttm": ni})
    detail, scores = _valorization(conn)
    assert detail["earnings_yield"] == 4 and detail["negative_earnings"] == 1
    assert detail["market_cap_missing"] == 0
    fcf = _pct([0.10 - 0.015 * i for i in range(1, 6)])
    ey = rank_pct([0.08, 0.05, 0.02, 0.0, None])
    expected = {i + 1: (fcf[i] + e) / 2 if (e := ey[i]) is not None else fcf[i] for i in range(5)}
    got = {aid: c["value"] for aid, c in scores.items()}
    assert got == pytest.approx(expected)
    # the loss-maker's positive part is 0: the lowest earnings yield, so only half of its
    # free-cash-flow rank survives -- and EEE, with no trailing net income, keeps that rank whole
    assert ey[3] == 0.0 and got[4] == pytest.approx(fcf[3] / 2)
    assert got[5] == pytest.approx(fcf[4])


def test_without_a_trailing_net_income_the_value_factor_is_the_cash_flow_yield_alone(
    with_caps: Database,
) -> None:
    """What every real cycle did before: no earnings yield for anyone."""
    detail, scores = _valorization(with_caps)
    assert detail["earnings_yield"] == 0 and detail["negative_earnings"] == 0
    fcf = _pct([0.10 - 0.015 * i for i in range(1, 6)])
    assert {a: c["value"] for a, c in scores.items()} == pytest.approx(
        {i + 1: fcf[i] for i in range(5)}
    )


def test_a_name_with_no_market_cap_has_no_earnings_yield(with_prices: Database) -> None:
    """No cover count or close: the cap is missing (T-132), so is the yield -- not zero."""
    for aid in range(1, 6):
        _roa_row(with_prices, aid, {"net_income_ttm": 100.0 * aid})
    detail, _ = _valorization(with_prices)
    assert detail["earnings_yield"] == 0 and detail["market_cap_missing"] == 5


def test_a_quarantined_return_on_assets_withholds_the_earnings_yield(with_caps: Database) -> None:
    """The net income is an input of that metric's row: a data-quality gate that quarantines the
    metric (a gate's verdict is not undone by reading its input elsewhere) drops the yield."""
    conn = with_caps
    _roa_row(conn, 1, {"net_income_ttm": 800.0})
    fid = conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO data_quality_issue (filing_id, asset_id, metric_group, metric_name, "
        "metric_engine_version, rule_id, severity, quarantined, value, evidence_json, gate_version, "
        "created_at) VALUES (?, 1, 'profitability', 'return_on_assets', 'metrics-v1', 'DQ_ROA', "
        "'SOFT', 1, 0.05, '{}', ?, 'now')",
        (fid, DATA_QUALITY_GATE_VERSION),
    )
    conn.commit()
    detail, _ = _valorization(conn)
    assert detail["earnings_yield"] == 0
