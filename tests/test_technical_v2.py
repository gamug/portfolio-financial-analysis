"""TECHNICAL score version 2 (T-070): 12-1 momentum, 90-day vol and drawdown, sector-Z."""

from __future__ import annotations

import math
import statistics

import pytest
from portfolio_common.db import Database
from test_cycle import _settings

from cycle.orchestrator import run_monitoring
from cycle.scores import technical
from pricing_agent.observations import build_observations
from pricing_agent.pricing_client import Candle


def _row(mom: float | None, vol: float | None, dd: float | None) -> dict[str, float | None]:
    """A price_observation row whose ``mom_12_1`` is *mom* (momentum_21d = 0, so the 12-1 return
    is exactly ``momentum_252d``)."""
    return {
        "momentum_21d": 0.0 if mom is not None else None,
        "momentum_252d": mom,
        "realized_vol_90d": vol,
        "max_drawdown_90d": dd,
    }


def _z(values: list[float]) -> list[float]:
    mean, sd = statistics.fmean(values), statistics.pstdev(values)
    return [(v - mean) / sd for v in values]


def test_mom_12_1_is_close_21_over_close_252_minus_one_and_needs_253_closes() -> None:
    closes = [100.0 + 0.5 * i + 7.0 * math.sin(i / 9.0) for i in range(260)]
    candles = [
        Candle(f"2022-{1 + i // 28:02d}-{1 + i % 28:02d}", c, c + 1, c - 1, c, 1000.0, "t")
        for i, c in enumerate(closes)
    ]
    obs = build_observations(candles, engine_version="priceobs-v2")

    def row(i: int) -> dict[str, float | None]:
        return {"momentum_21d": obs[i].momentum_21d, "momentum_252d": obs[i].momentum_252d}

    assert technical.mom_12_1(row(251)) is None  # 252 closes: no close 252 sessions back
    for i in (252, 255, 259):
        assert technical.mom_12_1(row(i)) == pytest.approx(closes[i - 21] / closes[i - 252] - 1.0)
    # the skipped month matters: it is not the plain 12-month return
    assert technical.mom_12_1(row(259)) != pytest.approx(closes[259] / closes[7] - 1.0)


def test_mom_12_1_is_none_when_either_momentum_is_missing() -> None:
    assert technical.mom_12_1({"momentum_21d": None, "momentum_252d": 0.2}) is None
    assert technical.mom_12_1({"momentum_21d": 0.1, "momentum_252d": None}) is None
    assert technical.mom_12_1({"momentum_21d": -1.0, "momentum_252d": 0.2}) is None


def test_raw_is_the_weighted_sector_z_blend() -> None:
    # six names in one sector: z within the sector (>= 5 names), all three signals present
    moms = [0.30, 0.10, -0.05, 0.20, 0.00, -0.20]
    vols = [0.20, 0.35, 0.50, 0.25, 0.40, 0.60]
    dds = [-0.05, -0.15, -0.30, -0.10, -0.20, -0.40]
    obs = {i: _row(m, v, d) for i, (m, v, d) in enumerate(zip(moms, vols, dds, strict=True))}
    scores = {s.asset_id: s for s in technical.compute(obs, dict.fromkeys(obs, 1))}
    zm, zv, zd = _z(moms), _z(vols), _z(dds)
    for i in obs:
        expected = 0.50 * zm[i] - 0.30 * zv[i] + 0.20 * zd[i]
        assert scores[i].raw_value == pytest.approx(expected)
        assert scores[i].components["z_mom_12_1"] == pytest.approx(zm[i])
        assert scores[i].components["n_mom_12_1"] == 6.0  # the sector, not the cross-section
    best, worst = scores[0], scores[5]
    assert best.raw_value > worst.raw_value  # higher momentum, lower vol, shallower drawdown


def test_each_signal_points_the_documented_way() -> None:
    base = _row(0.1, 0.3, -0.2)
    cohort = {
        1: base,
        2: _row(0.5, 0.3, -0.2),  # more momentum: better
        3: _row(0.1, 0.1, -0.2),  # less volatility: better
        4: _row(0.1, 0.3, -0.05),  # shallower drawdown (less negative): better
    }
    raw = {s.asset_id: s.raw_value for s in technical.compute(cohort)}
    assert raw[2] > raw[1] and raw[3] > raw[1] and raw[4] > raw[1]


def test_a_sector_under_five_names_uses_the_whole_cross_section() -> None:
    # sector 1 has 5 names (its own mean/sd); sector 2 has 3 (the cross-section's)
    moms = {1: 0.0, 2: 0.1, 3: 0.2, 4: 0.3, 5: 0.4, 6: 0.5, 7: 0.6, 8: 0.7}
    obs = {a: _row(m, 0.3, -0.1) for a, m in moms.items()}
    sectors = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 2, 7: 2, 8: 2}
    scores = {s.asset_id: s for s in technical.compute(obs, sectors)}
    in_sector = [moms[a] for a in (1, 2, 3, 4, 5)]
    whole = list(moms.values())
    assert scores[3].components["z_mom_12_1"] == pytest.approx(
        (0.2 - statistics.fmean(in_sector)) / statistics.pstdev(in_sector)
    )
    assert scores[3].components["n_mom_12_1"] == 5.0
    assert scores[7].components["z_mom_12_1"] == pytest.approx(
        (0.6 - statistics.fmean(whole)) / statistics.pstdev(whole)
    )
    assert scores[7].components["n_mom_12_1"] == 8.0  # the fallback group is the cross-section


def test_the_five_name_threshold_counts_names_that_have_the_signal() -> None:
    # sector 1 has 5 names but only 4 with a momentum: its momentum falls back, its vol does not
    obs = {a: _row(0.1 * a, 0.2 + 0.05 * a, -0.1) for a in range(1, 5)}
    obs[5] = _row(None, 0.5, -0.1)
    obs[6] = _row(0.9, 0.2, -0.1)  # another sector: only contributes to the cross-section
    scores = {s.asset_id: s for s in technical.compute(obs, {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 2})}
    assert scores[1].components["n_mom_12_1"] == 5.0  # 4 sector names + asset 6 = the cross-section
    assert scores[1].components["n_realized_vol_90d"] == 5.0  # the sector's own five


def test_an_asset_without_a_sector_is_standardized_against_the_cross_section() -> None:
    obs = {a: _row(0.1 * a, 0.3, -0.1) for a in range(1, 7)}
    scores = {s.asset_id: s for s in technical.compute(obs, {a: 1 for a in range(1, 6)})}
    assert scores[6].components["n_mom_12_1"] == 6.0
    assert scores[6].components["z_mom_12_1"] == pytest.approx(
        _z([0.1 * a for a in range(1, 7)])[5]
    )


def test_a_signal_with_no_spread_contributes_a_zero_z() -> None:
    obs = {a: _row(0.1 * a, 0.3, -0.1) for a in range(1, 7)}  # vol and drawdown identical
    scores = {s.asset_id: s for s in technical.compute(obs, dict.fromkeys(obs, 1))}
    assert all(s.components["z_realized_vol_90d"] == 0.0 for s in scores.values())
    zm = _z([0.1 * a for a in range(1, 7)])
    assert scores[1].raw_value == pytest.approx(0.50 * zm[0])


def test_the_blend_renormalizes_over_the_signals_present() -> None:
    # no 12-1 momentum (a young listing): volatility and drawdown only, weights 0.30 and 0.20
    obs = {a: _row(None, 0.2 + 0.05 * a, -0.05 * a) for a in range(1, 7)}
    scores = {s.asset_id: s for s in technical.compute(obs, dict.fromkeys(obs, 1))}
    zv, zd = _z([0.2 + 0.05 * a for a in range(1, 7)]), _z([-0.05 * a for a in range(1, 7)])
    for i, a in enumerate(range(1, 7)):
        assert scores[a].raw_value == pytest.approx((-0.30 * zv[i] + 0.20 * zd[i]) / 0.50)
        assert scores[a].components["z_mom_12_1"] is None


def test_the_coverage_floor_drops_an_asset_with_fewer_than_two_signals() -> None:
    obs = {
        1: _row(0.2, 0.3, -0.1),
        2: _row(0.1, 0.4, -0.2),
        3: _row(0.3, None, None),  # one signal: below the floor
        4: _row(None, None, None),  # none
        5: _row(None, 0.2, None),  # one signal (not the momentum one): below the floor
        6: _row(None, 0.5, -0.3),  # two signals: scored
    }
    scored = {s.asset_id for s in technical.compute(obs)}
    assert scored == {1, 2, 6}
    assert technical.MIN_SIGNALS == 2


def test_no_observations_no_scores() -> None:
    assert technical.compute({}) == []


def test_a_cycle_writes_technical_v2_and_skips_an_asset_below_the_floor(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    # asset 5 has neither volatility nor drawdown, and (as seeded) no 252-day momentum: 0 signals
    conn.execute(
        "UPDATE price_observation SET realized_vol_90d = NULL, max_drawdown_90d = NULL "
        "WHERE asset_id = 5"
    )
    conn.commit()
    r = run_monitoring(_settings(conn), "2026-07-31", conn=conn)
    rows = {
        int(x["asset_id"]): x
        for x in conn.execute(
            "SELECT asset_id, raw_value, normalized_score, model, inputs_json FROM score_snapshot "
            "WHERE score_type = 'TECHNICAL' AND event_time = '2026-07-31'"
        )
    }
    assert set(rows) == {1, 2, 3, 4}  # asset 5 is not scored (audit C5), not scored 50
    assert {x["model"] for x in rows.values()} == {"technical-v2"}
    assert all(x["normalized_score"] is not None for x in rows.values())
    # ... and is still ranked, on the components it has (T-141 renormalizes)
    ranked = conn.execute(
        "SELECT components_json FROM cycle_ranking WHERE cycle_run_id = ? AND asset_id = 5",
        (r.cycle_run_id,),
    ).fetchone()
    assert ranked is not None
    assert '"TECHNICAL": null' in ranked["components_json"]
    params = conn.execute(
        "SELECT params_json FROM cycle_run WHERE id = ?", (r.cycle_run_id,)
    ).fetchone()["params_json"]
    assert '"technical_version": "technical-v2"' in params
