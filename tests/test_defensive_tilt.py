"""A-2: the CHAMPION grid's blends and defensive tilts (preregistration §3), hand-computed."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from portfolio_common.db import Database
from test_cycle import D1, _ranking, _settings

from cycle.config import CycleSettings
from cycle.orchestrator import run_selection
from cycle.scores import defensive
from cycle.scores.normalize import normalized_scores
from kg_schema import connect as kg_connect


def _market(n: int, first: float, last: float) -> dict[str, float]:
    """A synthetic session series: *n* sessions, the first ones ``first``, the last 63 ``last``."""
    days = [f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]
    values = [first] * (n - 63) + [last] * 63
    return dict(zip(days, values, strict=True))


def test_r63_r126_are_cumulative_and_causal() -> None:
    market = _market(130, 0.001, -0.002)
    day = sorted(market)[-1]
    r63, r126 = defensive.r63_r126(market, day)
    assert r63 == pytest.approx((1 - 0.002) ** 63 - 1.0, abs=1e-15)
    assert r126 == pytest.approx((1.001) ** 63 * (1 - 0.002) ** 63 - 1.0, abs=1e-15)
    # a session after the as-of date never enters the window
    earlier = sorted(market)[-2]
    r63_earlier, _ = defensive.r63_r126(market, earlier)
    assert r63_earlier == pytest.approx((1 - 0.002) ** 62 * (1.001) - 1.0, abs=1e-15)


def test_the_five_triggers_hand_computed() -> None:
    assert not defensive.trigger_fires("T0", -0.5, -0.5)
    assert defensive.trigger_fires("R1", -0.001, 0.10)
    assert not defensive.trigger_fires("R1", 0.0, -0.10)
    assert defensive.trigger_fires("R2", -0.001, 0.10)
    assert defensive.trigger_fires("R3", 0.01, -0.0201)
    assert not defensive.trigger_fires("R3", 0.01, -0.0199)
    assert not defensive.trigger_fires("R3", -0.03, None)
    assert defensive.trigger_fires("R4", None, None)


def test_beta_needs_126_paired_returns() -> None:
    market = [0.01 if i % 2 == 0 else -0.01 for i in range(127)]
    asset = [2.0 * value for value in market]
    assert defensive.beta(asset, market) == pytest.approx(2.0, abs=1e-12)
    # 125 pairs -> no defensive score
    assert defensive.beta(asset[:-2], market[:-2]) is None
    # one non-finite pair is dropped; 126 finite pairs still qualify
    asset_nan = [math.nan, *asset[1:]]
    assert defensive.beta(asset_nan, market) == pytest.approx(2.0, abs=1e-12)


def test_defensive_scores_are_50_plus_10_z_of_minus_beta() -> None:
    scores = defensive.defensive_scores({1: 0.5, 2: 1.5})
    expected = normalized_scores([-0.5, -1.5])
    assert scores[1] == pytest.approx(expected[0], abs=1e-12)
    assert scores[2] == pytest.approx(expected[1], abs=1e-12)
    assert (scores[1], scores[2]) == pytest.approx((60.0, 40.0), abs=1e-12)


def test_the_tilted_ranking_hand_computed() -> None:
    blended = {1: 50.0, 2: 60.0}
    betas = {1: 0.5, 2: 1.5}
    tilted = defensive.tilted_scores(blended, betas, "R1", fired=True)
    # S_def = [60, 40], w = 0.25 -> [52.5, 55.0]
    assert tilted[1] == pytest.approx(52.5, abs=1e-12)
    assert tilted[2] == pytest.approx(55.0, abs=1e-12)
    # an unfired trigger and T0 are the plain blended scores
    assert defensive.tilted_scores(blended, betas, "R1", fired=False) == blended
    assert defensive.tilted_scores(blended, betas, "T0", fired=True) == blended
    # a name without a beta keeps its blended score
    no_beta = defensive.tilted_scores({1: 50.0, 3: 70.0}, betas, "R1", fired=True)
    assert no_beta[3] == 70.0


def test_b0_weights_are_the_plain_equal_weights() -> None:
    assert defensive.BLENDS["B0"] == {
        "FUNDAMENTAL": 1 / 3,
        "TECHNICAL": 1 / 3,
        "VALORIZATION": 1 / 3,
    }


def test_b0_t0_reproduces_the_plain_ranking(cycle_seed: Database, tmp_path: Path) -> None:
    """The baseline grid cell is the plain ranking: the default run and an explicit B0/T0 run on
    a byte-copy of the same seeded database rank identically."""
    conn = cycle_seed
    second = kg_connect(tmp_path / "second.db")
    conn.raw.backup(second.raw)
    try:
        run_selection(_settings(conn), D1, conn=conn)
        baseline = _ranking(conn)
        explicit = CycleSettings(
            db_path=tmp_path / "second.db",
            top_n=3,
            score_weights=dict(defensive.BLENDS["B0"]),
            defensive_tilt="T0",
        )
        run_selection(explicit, D1, conn=second)
        assert _ranking(second) == baseline
    finally:
        second.close()
