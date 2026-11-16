"""Round-3 experiment parameters, hand-computed (V3_EXPERIMENTS_PLAN §2).

The core-source and partial-exit arithmetic is pure, so every expected number is written out by
hand. The pool's second pass is tested in ``tests/test_quant_topn.py`` on a synthetic identity
covariance (32 names -> the 30 lowest ids, 1/30 each).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cycle.backtest import core, driver

CORE_ID = 100


def _close_series(count: int = 210, value: float = 100.0) -> dict[str, float]:
    return {f"d{index:04d}": value for index in range(count)}


def test_the_base_core_is_c_times_the_base_plus_one_minus_c_times_the_satellite() -> None:
    base = {1: 0.6, 2: 0.4}
    satellite = {2: 0.2, 3: 0.8}
    target = core.base_core_target(0.5, base, satellite)
    # 0.5 x base = {1: 0.3, 2: 0.2}; 0.5 x satellite = {2: 0.1, 3: 0.4}; the overlap on 2 adds.
    assert target == pytest.approx({1: 0.3, 2: 0.3, 3: 0.4}, abs=1e-15)
    assert sum(target.values()) == pytest.approx(1.0, abs=1e-15)
    # c = 0 is the satellite alone
    assert core.base_core_target(0.0, base, satellite) == pytest.approx(satellite, abs=1e-15)
    with pytest.raises(ValueError):
        core.base_core_target(1.0, base, satellite)


def test_the_base_core_target_through_config_targets_hand_computed() -> None:
    base = {1: 0.5, 2: 0.5}
    satellite = {2: 0.5, 3: 0.5}
    close = _close_series()
    config = core.config_targets(
        base,
        satellite,
        core_weight=0.5,
        core_asset_id=None,
        trend_filter=core.FILTER_OFF,
        close=close,
        decision="d0000",
        core_source="base",
    )
    assert config.base == base
    assert config.topn == pytest.approx({1: 0.25, 2: 0.5, 3: 0.25}, abs=1e-15)
    assert config.topn[2] == pytest.approx(0.5, abs=1e-15)  # 0.25 + 0.25


def test_a_partial_exit_keeps_half_the_normal_target_and_the_rest_in_cash() -> None:
    base = {1: 1.0}
    satellite = {1: 0.5, 2: 0.5}
    close = _close_series()
    close["d0209"] = 99.0  # below the 210-session mean: the filter triggers
    normal = core.config_targets(
        base,
        satellite,
        core_weight=0.5,
        core_asset_id=CORE_ID,
        trend_filter=core.FILTER_OFF,
        close=close,
        decision="d0209",
    )
    half = core.config_targets(
        base,
        satellite,
        core_weight=0.5,
        core_asset_id=CORE_ID,
        trend_filter=core.FILTER_ON,
        close=close,
        decision="d0209",
        filter_exit=0.5,
    )
    assert half.trend_state == "cash"
    assert half.topn == pytest.approx(
        {asset_id: 0.5 * value for asset_id, value in normal.topn.items()}, abs=1e-15
    )
    assert sum(half.topn.values()) == pytest.approx(0.5, abs=1e-15)
    full = core.config_targets(
        base,
        satellite,
        core_weight=0.5,
        core_asset_id=CORE_ID,
        trend_filter=core.FILTER_ON,
        close=close,
        decision="d0209",
        filter_exit=1.0,
    )
    assert full.topn == {}


def test_the_defaults_are_the_v2_path_bit_for_bit() -> None:
    base = {1: 0.5, 2: 0.5}
    satellite = {2: 0.5, 3: 0.5}
    close = _close_series()
    close["d0209"] = 99.0
    with_defaults = core.config_targets(
        base,
        satellite,
        core_weight=0.5,
        core_asset_id=CORE_ID,
        trend_filter=core.FILTER_ON,
        close=close,
        decision="d0209",
    )
    explicit = core.config_targets(
        base,
        satellite,
        core_weight=0.5,
        core_asset_id=CORE_ID,
        trend_filter=core.FILTER_ON,
        close=close,
        decision="d0209",
        core_source="spy",
        filter_exit=1.0,
    )
    assert with_defaults == explicit
    assert with_defaults.topn == {}


def test_invalid_round_three_values_are_refused() -> None:
    with pytest.raises(ValueError):
        core.config_targets(
            {1: 1.0},
            {1: 1.0},
            core_weight=0.5,
            core_asset_id=CORE_ID,
            trend_filter=core.FILTER_OFF,
            close=None,
            decision="d0000",
            core_source="bonds",
        )
    with pytest.raises(ValueError):
        core.config_targets(
            {1: 1.0},
            {1: 1.0},
            core_weight=0.5,
            core_asset_id=CORE_ID,
            trend_filter=core.FILTER_OFF,
            close=None,
            decision="d0000",
            filter_exit=1.5,
        )


def test_the_cycle_settings_size_the_book_to_the_pool(tmp_path: Path) -> None:
    """V3 plan §2a: the cycle's `top_n` is the candidate pool, not the 30-name satellite."""
    params = driver.load_params("src/cycle/backtest/params.frozen.toml")
    settings = driver._cycle_settings(
        Path(tmp_path) / "x.db", Path(tmp_path) / "u.db", params, top_n=60
    )
    assert settings.top_n == 60
    assert (
        driver._cycle_settings(Path(tmp_path) / "x.db", Path(tmp_path) / "u.db", params).top_n
        == params.top_n
    )
