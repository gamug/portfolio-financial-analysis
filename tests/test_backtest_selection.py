"""Final-round measurement functions: hand-computed cases and diagnostic invariants."""

from __future__ import annotations

import math

import numpy as np
import pytest

from cycle.backtest import selection as sel


def _performance(returns: list[float]) -> dict[str, float | int]:
    array = np.asarray(returns, dtype=float)
    zeros = np.zeros_like(array)
    return sel.performance(array, zeros, zeros, zeros)


def test_mdd_includes_the_initial_capital() -> None:
    # The first session loses 10%: the peak is the initial 1.0, not the post-loss NAV.
    first_loss = np.array([-0.10, 0.05, 0.01])
    equity = np.concatenate([[1.0], np.cumprod(1.0 + first_loss)])
    mdd, longest = sel._drawdown(equity)
    assert mdd == pytest.approx(0.10)
    assert longest == 3
    # A first gain has the post-gain NAV as the peak either way.
    first_gain = np.array([0.10, -0.20, 0.01])
    equity = np.concatenate([[1.0], np.cumprod(1.0 + first_gain)])
    assert sel._drawdown(equity)[0] == pytest.approx(0.20)


def test_cagr_sharpe_sortino_and_cvar_hand_computed() -> None:
    returns = np.array([0.01, -0.02, 0.03, -0.01])
    rf = np.array([0.001, 0.001, 0.001, 0.001])
    metrics = sel.performance(returns, rf, returns, returns)
    equity = np.prod(1.0 + returns)
    assert metrics["cagr"] == pytest.approx(equity ** (252 / 4) - 1.0)
    excess = returns - rf
    assert metrics["sharpe"] == pytest.approx(
        np.mean(excess) / np.std(excess, ddof=1) * math.sqrt(252)
    )
    downside = np.minimum(excess, 0.0)
    assert metrics["sortino"] == pytest.approx(
        np.mean(excess) * 252 / (math.sqrt(np.mean(downside**2)) * math.sqrt(252))
    )
    quantile = np.quantile(returns, 0.05)
    assert metrics["cvar95"] == pytest.approx(-np.mean(returns[returns <= quantile]))


def test_identical_series_are_no_evidence_of_a_difference() -> None:
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0005, 0.01, 300)
    rf = np.full(300, 0.0001)
    table = sel.test_table(returns, returns, rf, n_boot=99)
    assert table["hac_t"] == 0.0 and table["hac_p"] == 1.0
    assert table["jk_p"] == 1.0
    assert table["mdd_diff"] == 0.0 and table["bootstrap_p"] == 1.0


def test_expected_max_sharpe_matches_the_published_factors() -> None:
    assert sel.expected_max_sharpe(21, 1.0) == pytest.approx(1.9221, abs=1e-4)
    assert sel.expected_max_sharpe(55, 1.0) == pytest.approx(2.3126, abs=1e-4)


def test_pbo_is_zero_when_one_column_dominates_every_block() -> None:
    rng = np.random.default_rng(2)
    good = rng.normal(0.002, 0.005, 320)
    noise = rng.normal(-0.001, 0.005, (320, 3))
    matrix = np.column_stack([good, noise])
    result = sel.cscv_pbo(matrix, blocks=8)
    assert result["pbo"] == pytest.approx(0.0)
    assert result["splits"] == math.comb(8, 4)


def test_pbo_and_calmar_are_bounded() -> None:
    rng = np.random.default_rng(3)
    matrix = rng.normal(0.0, 0.01, (320, 6))
    assert 0.0 <= sel.cscv_pbo(matrix, blocks=8)["pbo"] <= 1.0
    assert 0.0 <= sel.cscv_pbo(matrix, blocks=8, metric="calmar")["pbo"] <= 1.0


def test_deflated_sharpe_is_a_probability_and_falls_with_more_trials() -> None:
    rng = np.random.default_rng(4)
    returns = rng.normal(0.0008, 0.01, 500)
    sr_std = 0.0004
    few = sel.deflated_sharpe(returns, n_trials=5, sr_std=sr_std)
    many = sel.deflated_sharpe(returns, n_trials=100, sr_std=sr_std)
    assert 0.0 <= few["dsr"] <= 1.0
    assert many["dsr"] <= few["dsr"]


def test_stationary_indices_stay_in_range_and_q_one_is_iid() -> None:
    rng = np.random.default_rng(5)
    indices = sel.stationary_indices(rng, 50, 1.0)
    assert indices.shape == (50,)
    assert indices.min() >= 0 and indices.max() < 50
    # q = 1: every draw is independent, so consecutive values need not follow each other.
    assert not np.array_equal(indices[1:], (indices[:-1] + 1) % 50)


def test_reality_check_on_a_zero_differential_is_not_significant() -> None:
    zeros = np.zeros((100, 3))
    result = sel.reality_check(zeros, n_boot=50)
    assert result["statistic"] == 0.0
    assert result["p_value"] == pytest.approx(1.0)


def test_walk_forward_breaks_a_tie_at_competition_rank_one() -> None:
    windows = {
        "A": [
            {"cumulative_return": 0.10, "max_drawdown": 0.10},
            {"cumulative_return": 0.20, "max_drawdown": 0.10},
            {"cumulative_return": 0.05, "max_drawdown": 0.10},
        ],
        "B": [
            {"cumulative_return": 0.10, "max_drawdown": 0.10},
            {"cumulative_return": 0.20, "max_drawdown": 0.10},
            {"cumulative_return": 0.05, "max_drawdown": 0.10},
        ],
    }
    rows = sel.walk_forward(windows)
    assert rows[0]["chosen"] in {"A", "B"}
    assert rows[0]["next_rank"] == 1
