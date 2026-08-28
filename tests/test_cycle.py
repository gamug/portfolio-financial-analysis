"""Selection / monitoring cycle: normalization, scores, rules, ranking, positions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cycle.config import CycleSettings
from cycle.construction import Candidate, target_weights
from cycle.orchestrator import run_monitoring, run_selection
from cycle.rules import RuleContext, enabled_rules, seed_catalog
from cycle.scores import quantitative, technical
from cycle.scores.normalize import cross_sectional_z, rank_pct, z_to_score

# -- normalize ----------------------------------------------------------


def test_cross_sectional_z_and_score() -> None:
    z = cross_sectional_z([1.0, 2.0, 3.0, 4.0, 5.0])
    assert z[2] == pytest.approx(0.0, abs=1e-9)
    assert z[0] < 0 < z[-1]
    assert z_to_score(0.0) == 50.0
    assert z_to_score(10.0) == 100.0
    assert z_to_score(-10.0) == 0.0


def test_rank_pct_handles_none_and_direction() -> None:
    asc = rank_pct([10.0, 20.0, 30.0, None])
    assert asc[0] == 0.0 and asc[2] == 1.0 and asc[3] is None
    desc = rank_pct([10.0, 20.0, 30.0], higher_is_better=False)
    assert desc[0] == 1.0 and desc[2] == 0.0


# -- score modules ------------------------------------------------


def test_technical_prefers_momentum_and_low_vol() -> None:
    obs: dict[int, dict[str, float | None]] = {
        1: {
            "momentum_63d": 0.30,
            "momentum_21d": 0.1,
            "realized_vol_90d": 0.15,
            "atr_14": 1.0,
            "close": 100.0,
            "max_drawdown_90d": -0.05,
        },
        2: {
            "momentum_63d": -0.20,
            "momentum_21d": -0.1,
            "realized_vol_90d": 0.60,
            "atr_14": 5.0,
            "close": 100.0,
            "max_drawdown_90d": -0.40,
        },
    }
    scores = {s.asset_id: s.raw_value for s in technical.compute(obs)}
    assert scores[1] > scores[2]


def test_quantitative_prefers_cheap_and_profitable() -> None:
    rows: dict[int, dict[str, float | None]] = {
        1: {
            "valuation.free_cash_flow_yield": 0.08,
            "profitability.return_on_equity": 0.30,
            "leverage.debt_to_equity": 0.4,
            "earnings_yield": 0.07,
        },
        2: {
            "valuation.free_cash_flow_yield": 0.01,
            "profitability.return_on_equity": 0.02,
            "leverage.debt_to_equity": 4.0,
            "earnings_yield": 0.01,
        },
    }
    scores = {s.asset_id: s.raw_value for s in quantitative.compute(rows)}
    assert scores[1] > scores[2]


# -- construction -----------------------------------------------


def test_target_weights_respects_name_and_sector_caps() -> None:
    cands = [
        Candidate(
            asset_id=i, blended_score=100.0 - i, sector_id=1 if i < 4 else 2, realized_vol_90d=0.2
        )
        for i in range(8)
    ]
    w = target_weights(cands, top_n=6, max_name_weight=0.30, max_sector_weight=0.5)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    # caps are enforced iteratively and converge to within rounding
    assert max(w.values()) <= 0.30 + 1e-3
    sector1 = sum(v for aid, v in w.items() if aid < 4)
    assert sector1 <= 0.5 + 1e-3

    # without a binding sector cap, higher score keeps a higher weight
    flat = [
        Candidate(asset_id=i, blended_score=100.0 - 10 * i, sector_id=1, realized_vol_90d=0.2)
        for i in range(5)
    ]
    fw = target_weights(flat, top_n=5, max_name_weight=0.9, max_sector_weight=1.0)
    assert fw[0] > fw[1] > fw[4]


# -- rules ------------------------------------------------------


def test_threshold_and_drawdown_rules(memory_db: sqlite3.Connection) -> None:
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": 5.0,
                "cashflow.free_cash_flow_margin": 0.2,
                "liquidity.current_ratio": 2.0,
            },
            2: {
                "leverage.debt_to_equity": 1.0,
                "cashflow.free_cash_flow_margin": -0.1,
                "liquidity.current_ratio": 0.7,
            },
        },
        price_obs={2: {"max_drawdown_90d": -0.50}},
        last_fundamental={1: "2026-03-31", 2: None},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert (1, "LEVERAGE_EXTREME", "HARD") in hits
    assert (2, "NEGATIVE_FCF", "HARD") in hits
    assert (2, "LIQUIDITY_DISTRESS", "SOFT") in hits
    assert (2, "PRICE_CRASH", "SOFT") in hits
    assert (2, "EARNINGS_MISSING", "SOFT") in hits


# -- full cycle smoke -----------------------------------------


@pytest.fixture
def cycle_seed(memory_db: sqlite3.Connection) -> sqlite3.Connection:
    conn = memory_db
    conn.execute("INSERT INTO sectors (id, name) VALUES (1, 'S1'), (2, 'S2')")
    for i, tk in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"], start=1):
        conn.execute(
            "INSERT INTO assets (id, ticker, sector_id) VALUES (?, ?, ?)", (i, tk, 1 + i % 2)
        )
        conn.execute(
            "INSERT INTO universe_membership (asset_id, universe, valid_from, detected_at, source) "
            "VALUES (?, 'SP500', '2026-01-01', '2026-01-01T00:00:00Z', 'test')",
            (i,),
        )
        fid = conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
            "retrieved_at) VALUES (?, '10-K', 2025, 'FY2025', '2025-12-31', '2026-02-01T00:00:00Z') "
            "RETURNING id",
            (i,),
        ).fetchone()["id"]
        for grp, name, val in [
            ("leverage", "debt_to_equity", 0.5 + i),  # AAA best, EEE worst
            ("profitability", "return_on_equity", 0.35 - 0.05 * i),
            ("valuation", "free_cash_flow_yield", 0.10 - 0.015 * i),
            ("cashflow", "free_cash_flow_margin", 0.2 - 0.03 * i),
            ("liquidity", "current_ratio", 2.5 - 0.1 * i),
        ]:
            conn.execute(
                "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, "
                "unit, computed_at, engine_version, event_time) "
                "VALUES (?, ?, ?, ?, 'x', '2026-02-01T00:00:00Z', 'metrics-v1', '2025-12-31')",
                (fid, grp, name, val),
            )
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
            "event_time, computed_at, model, run_kind) VALUES (?, 'FUNDAMENTAL', ?, ?, "
            "'2025-12-31', '2026-02-01T00:00:00Z', 'seed', 'analysis')",
            (i, 80 - 8 * i, 80 - 8 * i),
        )
        for d in range(1, 121):
            conn.execute(
                "INSERT INTO price_observation (asset_id, obs_date, close, atr_14, "
                "realized_vol_90d, max_drawdown_90d, momentum_63d, momentum_21d, event_time, "
                "computed_at, engine_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'2026-06-30T00:00:00Z', 'priceobs-v1')",
                (
                    i,
                    f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}",
                    100.0 + d * (6 - i),
                    2.0 * i,
                    0.10 * i,
                    -0.03 * i,
                    0.25 - 0.06 * i,
                    0.05 - 0.01 * i,
                    f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}",
                ),
            )
    conn.commit()
    return conn


def _settings(conn: sqlite3.Connection) -> CycleSettings:
    return CycleSettings(db_path=Path(":memory:"), top_n=3)


def test_selection_cycle_end_to_end(cycle_seed: sqlite3.Connection) -> None:
    conn = cycle_seed
    report = run_selection(_settings(conn), "2026-06-30", conn=conn)

    assert report.cycle_type == "SELECTION"
    assert "positions" in report.steps_run
    run_row = conn.execute("SELECT status FROM cycle_run ORDER BY id DESC LIMIT 1").fetchone()
    assert run_row["status"] == "completed"

    for stype in ("TECHNICAL", "QUANTITATIVE"):
        n = conn.execute(
            "SELECT COUNT(*) FROM score_snapshot WHERE score_type = ? AND event_time = '2026-06-30' "
            "AND normalized_score IS NOT NULL",
            (stype,),
        ).fetchone()[0]
        assert n == 5

    ranked = conn.execute(
        "SELECT ticker, rank, selected FROM v_cycle_ranking ORDER BY rank"
    ).fetchall()
    assert next(r["ticker"] for r in ranked) == "AAA"  # strongest on every factor
    assert sum(r["selected"] for r in ranked) == 3
    assert (
        conn.execute("SELECT COUNT(*) FROM portfolio_position WHERE valid_to IS NULL").fetchone()[0]
        == 3
    )


def test_cycle_resumes_without_duplicating(cycle_seed: sqlite3.Connection) -> None:
    conn = cycle_seed
    run_selection(_settings(conn), "2026-06-30", conn=conn)
    before = conn.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()[0]

    again = run_selection(_settings(conn), "2026-06-30", conn=conn)
    assert set(again.steps_skipped) >= {"technical", "quantitative", "rank", "positions"}
    assert conn.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 3


def test_t_minus_1_hard_veto_excludes_asset(cycle_seed: sqlite3.Connection) -> None:
    conn = cycle_seed
    seed_catalog(conn)
    # AAA (asset 1) carries a HARD veto dated the day before the cycle
    conn.execute(
        "INSERT INTO veto (asset_id, rule_id, severity, detected_at, cycle_date) "
        "VALUES (1, 'LEVERAGE_EXTREME', 'HARD', '2026-06-29T00:00:00Z', '2026-06-29')"
    )
    conn.commit()
    run_selection(_settings(conn), "2026-06-30", conn=conn)
    row = conn.execute(
        "SELECT vetoed, selected FROM v_cycle_ranking WHERE ticker = 'AAA'"
    ).fetchone()
    assert row["vetoed"] == 1
    assert row["selected"] == 0


def test_monitoring_cycle_skips_positions(cycle_seed: sqlite3.Connection) -> None:
    conn = cycle_seed
    r = run_monitoring(_settings(conn), "2026-07-31", conn=conn)
    assert "positions" not in r.steps_run and "positions" not in r.steps_skipped
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 0
